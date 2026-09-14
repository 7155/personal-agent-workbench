"""GitHub OAuth and repository operations behind a fixed HTTPS boundary.

The Team connection store owns credentials, membership and grants.  This
module only turns an already-authorized credential bundle into one bounded
GitHub request and projects the response into the small provider contract.
The default transport resolves one public address for each request, connects
to that address directly, and performs TLS verification against the original
GitHub hostname.  Tests can inject a transport with the public
``GitHubRequest -> GitHubResponse`` shape without opening a network socket.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import ipaddress
import json
from http.client import HTTPException, HTTPSConnection
import re
import socket
import ssl
import threading
import time
from typing import Protocol, runtime_checkable
from urllib.parse import quote, urlencode, urlsplit

from .errors import TeamError


GITHUB_OPERATIONS = (
    "repo.read",
    "file.read",
    "issues.list",
    "issue.read",
    "issue.create",
    "issue.comment",
)
GITHUB_READ_OPERATIONS = (
    "repo.read",
    "file.read",
    "issues.list",
    "issue.read",
)

GITHUB_OAUTH_HOST = "github.com"
GITHUB_API_HOST = "api.github.com"
GITHUB_OAUTH_AUTHORIZE_PATH = "/login/oauth/authorize"
GITHUB_OAUTH_TOKEN_PATH = "/login/oauth/access_token"
GITHUB_API_VERSION = "2022-11-28"

_ALLOWED_HOSTS = frozenset({GITHUB_OAUTH_HOST, GITHUB_API_HOST})
_HTTP_METHODS = frozenset({"GET", "POST"})
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "proxy",
        "proxy-authorization",
        "proxy-authenticate",
        "upgrade",
    }
)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
_PKCE_VALUE = re.compile(r"^[A-Za-z0-9._~-]+$")
_STATE_VALUE = re.compile(r"^[A-Za-z0-9._~-]+$")
_REPOSITORY = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,98})/[a-z0-9._-]{1,100}$"
)
_REF = re.compile(r"^[A-Za-z0-9._~/-]+$")
_MAX_PATH_CHARS = 4_096
_MAX_REQUEST_BYTES = 1 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_HEADERS = 128
_MAX_RESPONSE_HEADER_CHARS = 8_192
_MAX_RESULT_TEXT = 16 * 1024
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_ISSUES = 50
_MAX_PAGE = 1_000
_MAX_NUMBER = 2**31 - 1
_MAX_EXPIRY_MS = 10**15


__all__ = [
    "GITHUB_API_HOST",
    "GITHUB_API_VERSION",
    "GITHUB_OAUTH_HOST",
    "GITHUB_OPERATIONS",
    "GITHUB_READ_OPERATIONS",
    "GitHubConnectionClient",
    "GitHubRequest",
    "GitHubResponse",
    "GitHubTransport",
    "GitHubTransportRequest",
    "GitHubTransportResponse",
    "GitHubTransportError",
    "PinnedGitHubTransport",
    "HTTPSGitHubTransport",
    "normalize_repository",
]


def _invalid(message: str) -> TeamError:
    return TeamError(400, "connection_invalid", message)


def _safe_text(
    value: object,
    field: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field} must be text")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise _invalid(f"{field} is required")
    if len(normalized) > maximum:
        raise _invalid(f"{field} is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise _invalid(f"{field} contains an invalid character")
    return normalized


def _safe_secret(value: object, field: str, maximum: int = 16_384) -> str:
    result = _safe_text(value, field, maximum)
    if not _SAFE_TOKEN.fullmatch(result):
        raise _invalid(f"{field} has an invalid format")
    return result


def _safe_redirect_uri(value: object) -> str:
    uri = _safe_text(value, "redirect_uri", 2_048)
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
    except ValueError as exc:
        raise _invalid("redirect_uri is invalid") from exc
    if (
        parsed.scheme not in {"https", "http"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or not parsed.path
    ):
        raise _invalid("redirect_uri must be a fixed HTTPS callback")
    if parsed.scheme == "http" and hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise _invalid("HTTP redirect_uri is allowed only for loopback development")
    return uri


def _pkce(value: object, field: str, *, minimum: int, maximum: int) -> str:
    result = _safe_text(value, field, maximum)
    if not minimum <= len(result) <= maximum or not _PKCE_VALUE.fullmatch(result):
        raise _invalid(f"{field} is not a valid PKCE value")
    return result


def _state(value: object) -> str:
    result = _safe_text(value, "state", 256)
    if len(result) < 16 or not _STATE_VALUE.fullmatch(result):
        raise _invalid("state is not a valid OAuth state")
    return result


def _json_body(value: Mapping[str, object]) -> bytes:
    try:
        body = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _invalid("GitHub request body is not serializable") from exc
    if len(body) > _MAX_REQUEST_BYTES:
        raise _invalid("GitHub request body is too large")
    return body


def normalize_repository(value: object) -> str:
    """Normalize one exact GitHub ``owner/repo`` identifier.

    The provider API is addressed by path segments, so this helper deliberately
    accepts no URL syntax, encoded separators, traversal segments, or platform
    path spelling.  GitHub treats names case-insensitively for this surface;
    the returned identifier is always lower-case.  A leading dot in the repo
    segment is valid (for example ``.github``), while ``.`` and ``..`` are not.
    """

    if not isinstance(value, str) or value != value.strip():
        raise _invalid("repository must be normalized owner/repo text")
    if not value or len(value) > 201:
        raise _invalid("repository must be an exact owner/repo")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _invalid("repository must be an exact owner/repo")
    if value.count("/") != 1:
        raise _invalid("repository must be an exact owner/repo")
    owner, repo = value.split("/", 1)
    normalized = f"{owner.lower()}/{repo.lower()}"
    if owner in {".", ".."} or repo in {".", ".."}:
        raise _invalid("repository must be an exact owner/repo")
    if normalized.endswith(".git") or not _REPOSITORY.fullmatch(normalized):
        raise _invalid("repository must be an exact owner/repo")
    return normalized


@dataclass(frozen=True)
class GitHubRequest:
    """The only request shape accepted by an injected GitHub transport."""

    method: str
    host: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not isinstance(self.host, str) or not isinstance(self.path, str):
            raise ValueError("GitHub transport request fields must be text")
        method = self.method.upper()
        host = self.host
        path = self.path
        if method not in _HTTP_METHODS:
            raise ValueError("GitHub transport method is unsupported")
        if host not in _ALLOWED_HOSTS:
            raise ValueError("GitHub transport host is not approved")
        parsed = urlsplit(path)
        if (
            not path.startswith("/")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or len(path) > _MAX_PATH_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
        ):
            raise ValueError("GitHub transport path is invalid")
        if not isinstance(self.body, bytes) or len(self.body) > _MAX_REQUEST_BYTES:
            raise ValueError("GitHub transport request body is too large")
        raw_headers = self.headers
        if not isinstance(raw_headers, Mapping) or len(raw_headers) > _MAX_RESPONSE_HEADERS:
            raise ValueError("GitHub transport headers are too large")
        headers: dict[str, str] = {}
        total = 0
        for raw_name, raw_value in raw_headers.items():
            name = str(raw_name)
            value = str(raw_value)
            if (
                not name
                or any(ord(char) < 33 or ord(char) == 127 for char in name)
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or name.casefold() in _FORBIDDEN_REQUEST_HEADERS
                or len(name) > _MAX_RESPONSE_HEADER_CHARS
                or len(value) > _MAX_RESPONSE_HEADER_CHARS
            ):
                raise ValueError("GitHub transport header is invalid")
            total += len(name) + len(value)
            headers[name] = value
        if total > 64 * 1024:
            raise ValueError("GitHub transport headers are too large")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "headers", headers)


@dataclass(frozen=True)
class GitHubResponse:
    """Bounded response shape returned by an injected transport."""

    status: int
    headers: Sequence[tuple[str, str]] = ()
    body: bytes = b""

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise ValueError("GitHub response status is invalid")
        if not isinstance(self.body, bytes):
            raise ValueError("GitHub response body must be bytes")
        normalized: list[tuple[str, str]] = []
        for item in self.headers:
            if not isinstance(item, Sequence) or len(item) != 2:
                raise ValueError("GitHub response header is invalid")
            normalized.append((str(item[0]), str(item[1])))
        object.__setattr__(self, "headers", tuple(normalized))


GitHubTransportRequest = GitHubRequest
GitHubTransportResponse = GitHubResponse


class GitHubTransportError(OSError):
    """A transport failure whose provider-side outcome is not known."""


@runtime_checkable
class GitHubTransport(Protocol):
    def request(self, request: GitHubRequest) -> GitHubResponse: ...


def _resolve_public_ip(host: str) -> str:
    """Resolve one public address; callers connect only to this result."""

    try:
        addresses = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise GitHubTransportError("GitHub DNS resolution failed") from exc
    seen: set[str] = set()
    for _family, _socktype, _protocol, _canonname, sockaddr in addresses:
        if not sockaddr:
            continue
        candidate = str(sockaddr[0])
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_global:
            return candidate
    raise GitHubTransportError("GitHub DNS returned no public address")


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, *, timeout: float) -> None:
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip
        self._deadline = time.monotonic() + timeout
        self._cancelled = threading.Event()
        self._owned_socket: socket.socket | ssl.SSLSocket | None = None
        self._detached_socket: socket.socket | ssl.SSLSocket | None = None
        self._response: object | None = None
        self._during_getresponse = False

    def _remaining(self) -> float:
        if self._cancelled.is_set():
            raise TimeoutError("GitHub HTTPS deadline expired")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self.abort()
            raise TimeoutError("GitHub HTTPS deadline expired")
        return remaining

    @staticmethod
    def _shutdown_close(value: object | None) -> None:
        if value is None:
            return
        shutdown = getattr(value, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass
        close = getattr(value, "close", None)
        if callable(close):
            try:
                close()
            except (OSError, ValueError):
                pass

    def abort(self) -> None:
        """Interrupt the socket behind HTTPResponse.fp without a close deadlock.

        ``HTTPResponse.fp`` is a buffered file object and its ``close`` method
        can wait for a concurrent reader.  Shutting down the underlying socket
        first is the operation that interrupts that reader; dropping our
        response reference then lets the normal response cleanup close the
        file after the reader exits.
        """

        self._cancelled.set()
        self._response = None
        targets = (self.sock, self._owned_socket, self._detached_socket)
        seen: set[int] = set()
        for target in targets:
            if target is None or id(target) in seen:
                continue
            seen.add(id(target))
            self._shutdown_close(target)
        self._owned_socket = None
        self._detached_socket = None
        try:
            super().close()
        except OSError:
            pass

    def close(self) -> None:
        # HTTPConnection.getresponse() invokes close() after parsing a
        # Connection: close response.  Detach the socket so HTTPResponse.fp
        # keeps its file descriptor for the body; explicit cancellation uses
        # abort(), which shuts that detached socket down.
        if self._during_getresponse:
            self._detached_socket = self.sock
            self.sock = None
            return
        super().close()

    def connect(self) -> None:
        raw: socket.socket | None = None
        wrapped: ssl.SSLSocket | None = None
        try:
            raw = socket.create_connection(
                (self._pinned_ip, 443),
                timeout=self._remaining(),
            )
            self._owned_socket = raw
            if self._cancelled.is_set():
                raise TimeoutError("GitHub HTTPS deadline expired")
            raw.settimeout(self._remaining())
            wrapped = self._context.wrap_socket(raw, server_hostname=self.host)
            self._owned_socket = wrapped
            self.sock = wrapped
            if self._cancelled.is_set():
                raise TimeoutError("GitHub HTTPS deadline expired")
            wrapped.settimeout(self._remaining())
            self._owned_socket = None
        except Exception:
            self._shutdown_close(wrapped)
            self._shutdown_close(raw)
            self._owned_socket = None
            self.sock = None
            raise

    def getresponse(self):
        self._during_getresponse = True
        try:
            response = super().getresponse()
        finally:
            self._during_getresponse = False
        self._response = response
        if self._cancelled.is_set():
            self.abort()
            raise TimeoutError("GitHub HTTPS deadline expired")
        return response


class PinnedGitHubTransport:
    """Direct HTTPS transport with no proxy or redirect handling.

    ``timeout_seconds`` is a wall-clock deadline for TCP, TLS, request,
    response headers, and response body after the platform DNS lookup has
    returned.  ``getaddrinfo`` is intentionally left to the host resolver;
    its own resolver timeout is an operating-system boundary rather than a
    Python socket inactivity timeout.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        if isinstance(timeout_seconds, bool):
            raise ValueError("GitHub transport timeout is out of bounds")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("GitHub transport timeout is out of bounds") from exc
        if not 1.0 <= timeout <= 120.0:
            raise ValueError("GitHub transport timeout is out of bounds")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("GitHub transport response limit is out of bounds")
        self.timeout_seconds = timeout
        self.max_response_bytes = max_response_bytes

    def request(self, request: GitHubRequest) -> GitHubResponse:
        if not isinstance(request, GitHubRequest):
            raise ValueError("GitHub transport requires a GitHubRequest")
        pinned_ip = _resolve_public_ip(request.host)
        connection = _PinnedHTTPSConnection(
            request.host,
            pinned_ip,
            timeout=self.timeout_seconds,
        )
        deadline_timer = threading.Timer(self.timeout_seconds, connection.abort)
        deadline_timer.daemon = True
        deadline_timer.start()
        try:
            connection.request(
                request.method,
                request.path,
                body=request.body if request.body else None,
                headers=dict(request.headers),
            )
            response = connection.getresponse()
            response_headers = [(str(name), str(value)) for name, value in response.getheaders()]
            if len(response_headers) > _MAX_RESPONSE_HEADERS:
                raise GitHubTransportError("GitHub response headers are too large")
            total_header_chars = 0
            content_lengths: list[int] = []
            for name, value in response_headers:
                if (
                    not name
                    or any(ord(char) < 33 or ord(char) == 127 for char in name)
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)
                    or len(name) > _MAX_RESPONSE_HEADER_CHARS
                    or len(value) > _MAX_RESPONSE_HEADER_CHARS
                ):
                    raise GitHubTransportError("GitHub response header is invalid")
                total_header_chars += len(name) + len(value)
                if name.lower() == "content-length":
                    try:
                        content_lengths.append(int(value))
                    except ValueError as exc:
                        raise GitHubTransportError("GitHub response length is invalid") from exc
            if total_header_chars > 64 * 1024 or len(set(content_lengths)) > 1:
                raise GitHubTransportError("GitHub response headers are too large")
            if content_lengths and (
                content_lengths[0] < 0
                or content_lengths[0] > self.max_response_bytes
            ):
                raise GitHubTransportError("GitHub response body exceeds its limit")
            body = response.read(self.max_response_bytes + 1)
            if len(body) > self.max_response_bytes:
                raise GitHubTransportError("GitHub response body exceeds its limit")
            if content_lengths and len(body) != content_lengths[0]:
                raise GitHubTransportError("GitHub response body is incomplete")
            result = GitHubResponse(response.status, response_headers, body)
            try:
                response.close()
            except (OSError, ValueError):
                pass
            return result
        except GitHubTransportError:
            raise
        except (OSError, HTTPException, TimeoutError, ssl.SSLError, ValueError) as exc:
            raise GitHubTransportError("GitHub HTTPS request failed") from exc
        finally:
            deadline_timer.cancel()
            connection.abort()


HTTPSGitHubTransport = PinnedGitHubTransport


def _now_ms() -> int:
    return int(time.time() * 1000)


class GitHubConnectionClient:
    """GitHub OAuth client and fixed-surface repository API."""

    def __init__(
        self,
        *,
        client_id: str = "",
        client_secret: str = "",
        transport: GitHubTransport | Callable[[GitHubRequest], GitHubResponse] | None = None,
        now_ms: Callable[[], int] | int | None = None,
    ) -> None:
        if not isinstance(client_id, str) or not isinstance(client_secret, str):
            raise ValueError("GitHub OAuth client configuration must be text")
        self.client_id = _safe_text(client_id, "client_id", 256, allow_empty=True)
        self.client_secret = _safe_text(
            client_secret,
            "client_secret",
            4_096,
            allow_empty=True,
        )
        self.transport = transport if transport is not None else PinnedGitHubTransport()
        if callable(now_ms):
            self._clock = now_ms
        elif now_ms is None:
            self._clock = _now_ms
        else:
            fixed_now = int(now_ms)
            self._clock = lambda: fixed_now

    @property
    def oauth_available(self) -> bool:
        return bool(self.client_id)

    def _current_ms(self) -> int:
        try:
            value = int(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise TeamError(503, "connection_not_configured", "GitHub clock is unavailable") from exc
        return value

    def _require_client_id(self) -> str:
        if not self.client_id:
            raise TeamError(503, "connection_not_configured", "GitHub OAuth is not configured")
        return self.client_id

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        callback = _safe_redirect_uri(redirect_uri)
        oauth_state = _state(state)
        challenge = _pkce(code_challenge, "code_challenge", minimum=43, maximum=43)
        query = urlencode(
            {
                "client_id": self._require_client_id(),
                "redirect_uri": callback,
                "state": oauth_state,
                "scope": "repo read:user",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"https://{GITHUB_OAUTH_HOST}{GITHUB_OAUTH_AUTHORIZE_PATH}?{query}"

    def exchange_code(
        self,
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> dict[str, object]:
        callback = _safe_redirect_uri(redirect_uri)
        authorization_code = _safe_text(code, "code", 2_048)
        code_verifier = _pkce(verifier, "code_verifier", minimum=43, maximum=128)
        fields: dict[str, str] = {
            "client_id": self._require_client_id(),
            "code": authorization_code,
            "redirect_uri": callback,
            "code_verifier": code_verifier,
        }
        if self.client_secret:
            fields["client_secret"] = self.client_secret
        request = self._form_request(GITHUB_OAUTH_HOST, GITHUB_OAUTH_TOKEN_PATH, fields)
        payload = self._request_json(request, expected=Mapping, token_exchange=True)
        return self._credential_bundle(payload)

    def refresh_token(self, credentials: Mapping[str, object]) -> dict[str, object]:
        bundle = self._validate_credentials(credentials, require_refresh=True, allow_expired=True)
        fields: dict[str, str] = {
            "client_id": self._require_client_id(),
            "grant_type": "refresh_token",
            "refresh_token": str(bundle["refreshToken"]),
        }
        if self.client_secret:
            fields["client_secret"] = self.client_secret
        request = self._form_request(GITHUB_OAUTH_HOST, GITHUB_OAUTH_TOKEN_PATH, fields)
        payload = self._request_json(
            request,
            expected=Mapping,
            token_exchange=True,
            reconnect_on_unauthorized=True,
        )
        return self._credential_bundle(payload)

    def account(self, credentials: Mapping[str, object]) -> dict[str, str]:
        bundle = self._validate_credentials(credentials)
        payload = self._api_json(bundle, "GET", "/user")
        if not isinstance(payload, Mapping):
            raise self._invalid_provider_response()
        login = self._response_text(payload.get("login"), "account login", 256, required=True)
        identifier = payload.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, (int, str)):
            raise self._invalid_provider_response()
        if isinstance(identifier, int):
            identifier_text = str(identifier)
        else:
            identifier_text = self._response_text(identifier, "account id", 128, required=True)
            if identifier_text is None:
                raise self._invalid_provider_response()
        return {"login": login, "id": identifier_text}

    def execute(
        self,
        credentials: Mapping[str, object],
        operation: str,
        repository: str,
        args: Mapping[str, object],
    ) -> dict[str, object]:
        if operation not in GITHUB_OPERATIONS:
            raise _invalid("operation is not supported")
        normalized_repository = self._repository(repository)
        bundle = self._validate_credentials(credentials)
        owner, repo = normalized_repository.split("/", 1)

        if operation == "repo.read":
            self._arguments(args, set())
            payload = self._api_json(bundle, "GET", f"/repos/{owner}/{repo}")
            return self._project_repository(payload)

        if operation == "file.read":
            values = self._arguments(args, {"path", "ref"}, required={"path"})
            path = self._file_path(values["path"])
            ref = self._ref(values["ref"]) if "ref" in values else None
            endpoint = f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}"
            if ref is not None:
                endpoint += "?" + urlencode({"ref": ref})
            payload = self._api_json(bundle, "GET", endpoint)
            return self._project_file(payload, path=path, ref=ref)

        if operation == "issues.list":
            values = self._arguments(args, {"state", "page"})
            state = values.get("state", "open")
            if state not in {"open", "closed", "all"}:
                raise _invalid("state is invalid")
            page = self._page(values.get("page", 1))
            endpoint = f"/repos/{owner}/{repo}/issues?" + urlencode(
                {"state": state, "per_page": _MAX_ISSUES, "page": page}
            )
            payload = self._api_json(bundle, "GET", endpoint)
            if not isinstance(payload, list):
                raise self._invalid_provider_response()
            items = [self._project_issue(item) for item in payload[:_MAX_ISSUES]]
            return {
                "items": items,
                "state": state,
                "page": page,
                "truncated": len(payload) > _MAX_ISSUES,
            }

        if operation == "issue.read":
            values = self._arguments(args, {"number"}, required={"number"})
            number = self._number(values["number"])
            payload = self._api_json(bundle, "GET", f"/repos/{owner}/{repo}/issues/{number}")
            return self._project_issue(payload)

        if operation == "issue.create":
            values = self._arguments(args, {"title", "body"}, required={"title", "body"})
            title = self._argument_text(values["title"], "title", 1_000)
            body = self._argument_text(
                values["body"],
                "body",
                _MAX_RESULT_TEXT,
                allow_empty=True,
                allow_line_breaks=True,
            )
            payload = self._api_json(
                bundle,
                "POST",
                f"/repos/{owner}/{repo}/issues",
                body=_json_body({"title": title, "body": body}),
                uncertain_on_invalid=True,
            )
            try:
                return self._project_issue(payload)
            except TeamError as exc:
                raise self._invalid_provider_response(uncertain=True) from exc

        values = self._arguments(args, {"number", "body"}, required={"number", "body"})
        number = self._number(values["number"])
        body = self._argument_text(
            values["body"],
            "body",
            _MAX_RESULT_TEXT,
            allow_empty=True,
            allow_line_breaks=True,
        )
        payload = self._api_json(
            bundle,
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            body=_json_body({"body": body}),
            uncertain_on_invalid=True,
        )
        try:
            return self._project_comment(payload)
        except TeamError as exc:
            raise self._invalid_provider_response(uncertain=True) from exc

    @staticmethod
    def _arguments(
        value: Mapping[str, object],
        allowed: set[str],
        *,
        required: set[str] | None = None,
    ) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise _invalid("args must be an object")
        unknown = set(value) - allowed
        if unknown:
            raise _invalid("args contain unsupported fields")
        missing = (required or set()) - set(value)
        if missing:
            raise _invalid("args are missing required fields")
        return dict(value)

    @staticmethod
    def _argument_text(
        value: object,
        field: str,
        maximum: int,
        *,
        allow_empty: bool = False,
        allow_line_breaks: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise _invalid(f"{field} must be text")
        if len(value) > maximum or any(
            ord(char) == 127
            or ord(char) < 32
            and (not allow_line_breaks or ord(char) not in {9, 10, 13})
            for char in value
        ):
            raise _invalid(f"{field} is invalid")
        if not allow_empty and not value.strip():
            raise _invalid(f"{field} is required")
        return value

    @staticmethod
    def _repository(value: object) -> str:
        return normalize_repository(value)

    @staticmethod
    def _file_path(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > _MAX_PATH_CHARS:
            raise _invalid("path is invalid")
        if (
            value.startswith("/")
            or value.endswith("/")
            or "//" in value
            or "\\" in value
            or "%" in value
            or "?" in value
            or "#" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {".", ".."} for part in value.split("/"))
        ):
            raise _invalid("path is invalid")
        return value

    @staticmethod
    def _ref(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 512:
            raise _invalid("ref is invalid")
        if (
            value.startswith("/")
            or value.endswith("/")
            or "//" in value
            or "\\" in value
            or any(part in {".", ".."} for part in value.split("/"))
            or not _REF.fullmatch(value)
        ):
            raise _invalid("ref is invalid")
        return value

    @staticmethod
    def _page(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PAGE:
            raise _invalid("page is invalid")
        return value

    @staticmethod
    def _number(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_NUMBER:
            raise _invalid("issue number is invalid")
        return value

    def _validate_credentials(
        self,
        credentials: Mapping[str, object],
        *,
        require_refresh: bool = False,
        allow_expired: bool = False,
    ) -> dict[str, object]:
        if not isinstance(credentials, Mapping):
            raise TeamError(401, "connection_reconnect_required", "GitHub credentials are unavailable")
        allowed = {
            "accessToken",
            "tokenType",
            "expiresAtMs",
            "refreshToken",
            "refreshExpiresAtMs",
        }
        if set(credentials) - allowed:
            raise TeamError(401, "connection_reconnect_required", "GitHub credentials are invalid")
        token_type = credentials.get("tokenType")
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            raise TeamError(401, "connection_reconnect_required", "GitHub credentials are invalid")
        result: dict[str, object] = {"tokenType": "bearer"}
        access_token = credentials.get("accessToken")
        if access_token is None and not require_refresh:
            raise TeamError(401, "connection_reconnect_required", "GitHub credentials are unavailable")
        if access_token is not None:
            try:
                result["accessToken"] = _safe_secret(access_token, "accessToken")
            except TeamError as exc:
                raise TeamError(401, "connection_reconnect_required", "GitHub credentials are invalid") from exc
        for field_name in ("expiresAtMs", "refreshExpiresAtMs"):
            value = credentials.get(field_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_EXPIRY_MS:
                raise TeamError(401, "connection_reconnect_required", "GitHub credentials are invalid")
            result[field_name] = value
        refresh_token = credentials.get("refreshToken")
        if refresh_token is not None:
            try:
                result["refreshToken"] = _safe_secret(refresh_token, "refreshToken")
            except TeamError as exc:
                raise TeamError(401, "connection_reconnect_required", "GitHub credentials are invalid") from exc
        if require_refresh and "refreshToken" not in result:
            raise TeamError(401, "connection_reconnect_required", "GitHub refresh is unavailable")
        current = self._current_ms()
        expires_at = result.get("expiresAtMs")
        if not allow_expired and isinstance(expires_at, int) and expires_at <= current:
            raise TeamError(401, "connection_reconnect_required", "GitHub access has expired")
        refresh_expires_at = result.get("refreshExpiresAtMs")
        if isinstance(refresh_expires_at, int) and refresh_expires_at <= current:
            raise TeamError(401, "connection_reconnect_required", "GitHub refresh has expired")
        return result

    @staticmethod
    def _form_request(host: str, path: str, fields: Mapping[str, str]) -> GitHubRequest:
        body = urlencode(dict(fields)).encode("ascii")
        if len(body) > _MAX_REQUEST_BYTES:
            raise _invalid("GitHub OAuth request is too large")
        return GitHubRequest(
            "POST",
            host,
            path,
            {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "PAW-Team-GitHub/1",
            },
            body,
        )

    def _api_json(
        self,
        credentials: Mapping[str, object],
        method: str,
        path: str,
        *,
        body: bytes = b"",
        uncertain_on_invalid: bool = False,
    ) -> object:
        access_token = str(credentials["accessToken"])
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "PAW-Team-GitHub/1",
        }
        if body:
            headers["Content-Type"] = "application/json"
        request = GitHubRequest(method, GITHUB_API_HOST, path, headers, body)
        return self._request_json(
            request,
            expected=object,
            reconnect_on_unauthorized=True,
            uncertain_on_invalid=uncertain_on_invalid,
        )

    def _request_json(
        self,
        request: GitHubRequest,
        *,
        expected: object,
        token_exchange: bool = False,
        reconnect_on_unauthorized: bool = False,
        uncertain_on_invalid: bool = False,
    ) -> object:
        response = self._request(request, uncertain_on_invalid=uncertain_on_invalid)
        if not 200 <= response.status < 300:
            self._raise_http_error(
                response.status,
                token_exchange=token_exchange,
                reconnect_on_unauthorized=reconnect_on_unauthorized,
            )
        if not response.body:
            raise self._invalid_provider_response(uncertain=uncertain_on_invalid)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._invalid_provider_response(uncertain=uncertain_on_invalid) from exc
        if expected is Mapping and not isinstance(payload, Mapping):
            raise self._invalid_provider_response(uncertain=uncertain_on_invalid)
        if expected is list and not isinstance(payload, list):
            raise self._invalid_provider_response(uncertain=uncertain_on_invalid)
        return payload

    def _request(
        self,
        request: GitHubRequest,
        *,
        uncertain_on_invalid: bool = False,
    ) -> GitHubResponse:
        try:
            request_method = getattr(self.transport, "request", None)
            if callable(request_method):
                value = request_method(request)
            elif callable(self.transport):
                value = self.transport(request)  # type: ignore[misc]
            else:
                raise GitHubTransportError("GitHub transport is unavailable")
        except TeamError:
            raise
        except (GitHubTransportError, TimeoutError, OSError, HTTPException, ssl.SSLError) as exc:
            raise TeamError(
                503,
                "connection_outcome_unknown",
                "GitHub connection outcome is unknown",
            ) from exc
        except Exception as exc:
            # Alternate transports are still inside the public provider
            # boundary: their implementation details must not escape.
            raise TeamError(
                503,
                "connection_outcome_unknown",
                "GitHub connection outcome is unknown",
            ) from exc
        return self._coerce_response(value, uncertain_on_invalid=uncertain_on_invalid)

    @staticmethod
    def _coerce_response(
        value: object,
        *,
        uncertain_on_invalid: bool = False,
    ) -> GitHubResponse:
        invalid = lambda: GitHubConnectionClient._invalid_provider_response(
            uncertain=uncertain_on_invalid
        )
        if isinstance(value, GitHubResponse):
            response = value
        elif isinstance(value, Mapping):
            try:
                response = GitHubResponse(
                    value.get("status"),  # type: ignore[arg-type]
                    value.get("headers", ()),  # type: ignore[arg-type]
                    value.get("body", b""),  # type: ignore[arg-type]
                )
            except (TypeError, ValueError) as exc:
                raise invalid() from exc
        else:
            try:
                response = GitHubResponse(
                    getattr(value, "status"),
                    getattr(value, "headers", ()),
                    getattr(value, "body"),
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise invalid() from exc
        if not 100 <= response.status <= 599 or len(response.body) > _MAX_RESPONSE_BYTES:
            raise invalid()
        if len(response.headers) > _MAX_RESPONSE_HEADERS:
            raise invalid()
        total = 0
        for name, header_value in response.headers:
            if (
                not name
                or any(ord(char) < 33 or ord(char) == 127 for char in name)
                or any(ord(char) < 32 or ord(char) == 127 for char in header_value)
            ):
                raise invalid()
            total += len(name) + len(header_value)
        if total > 64 * 1024:
            raise invalid()
        return response

    @staticmethod
    def _raise_http_error(
        status: int,
        *,
        token_exchange: bool,
        reconnect_on_unauthorized: bool,
    ) -> None:
        if status >= 500:
            raise TeamError(
                503,
                "connection_outcome_unknown",
                "GitHub connection outcome is unknown",
            )
        if status == 401 and reconnect_on_unauthorized:
            raise TeamError(
                401,
                "connection_reconnect_required",
                "GitHub authorization must be renewed",
            )
        if 300 <= status < 400:
            raise TeamError(
                502,
                "connection_provider_rejected",
                "GitHub redirects are unsupported",
            )
        raise TeamError(
            status,
            "connection_provider_rejected",
            "GitHub rejected the connection request",
        )

    @staticmethod
    def _invalid_provider_response(*, uncertain: bool = False) -> TeamError:
        if uncertain:
            return TeamError(
                503,
                "connection_outcome_unknown",
                "GitHub response outcome is unknown",
            )
        return TeamError(502, "connection_provider_rejected", "GitHub returned an invalid response")

    def _credential_bundle(self, payload: Mapping[str, object]) -> dict[str, object]:
        try:
            access = payload.get("access_token", payload.get("accessToken"))
            token_type = payload.get("token_type", payload.get("tokenType"))
            if not isinstance(access, str) or not isinstance(token_type, str):
                raise ValueError
            if token_type.casefold() != "bearer":
                raise ValueError
            result: dict[str, object] = {
                "accessToken": _safe_secret(access, "accessToken"),
                "tokenType": "bearer",
            }
            expires_in = payload.get("expires_in")
            if expires_in is not None:
                if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not 1 <= expires_in <= 10**9:
                    raise ValueError
            refresh_expires: object | None = None
            refresh = payload.get("refresh_token", payload.get("refreshToken"))
            if refresh is not None:
                result["refreshToken"] = _safe_secret(refresh, "refreshToken")
                refresh_expires = payload.get("refresh_token_expires_in")
                if refresh_expires is not None:
                    if (
                        isinstance(refresh_expires, bool)
                        or not isinstance(refresh_expires, int)
                        or not 1 <= refresh_expires <= 10**9
                    ):
                        raise ValueError
            if expires_in is not None or (
                refresh is not None and payload.get("refresh_token_expires_in") is not None
            ):
                current = self._current_ms()
                if expires_in is not None:
                    result["expiresAtMs"] = current + expires_in * 1_000
                if refresh is not None and refresh_expires is not None:
                    result["refreshExpiresAtMs"] = current + refresh_expires * 1_000
            return result
        except (TeamError, TypeError, ValueError, KeyError) as exc:
            raise self._invalid_provider_response() from exc

    @staticmethod
    def _response_text(
        value: object,
        field: str,
        maximum: int,
        *,
        required: bool = False,
    ) -> str | None:
        if value is None:
            if required:
                raise TeamError(502, "connection_provider_rejected", "GitHub returned an invalid response")
            return None
        if not isinstance(value, str) or len(value) > maximum:
            raise TeamError(502, "connection_provider_rejected", "GitHub returned an invalid response")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise TeamError(502, "connection_provider_rejected", "GitHub returned an invalid response")
        return value

    def _project_repository(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise self._invalid_provider_response()
        identifier = payload.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 0:
            raise self._invalid_provider_response()
        name = self._response_text(payload.get("name"), "name", 256, required=True)
        full_name = self._response_text(payload.get("full_name"), "full_name", 512, required=True)
        private = payload.get("private")
        if not isinstance(private, bool):
            raise self._invalid_provider_response()
        description = self._response_text(payload.get("description"), "description", _MAX_RESULT_TEXT)
        default_branch = self._response_text(payload.get("default_branch"), "default_branch", 256)
        html_url = self._response_url(payload.get("html_url"))
        return {
            "id": identifier,
            "name": name,
            "fullName": full_name,
            "private": private,
            "description": description,
            "defaultBranch": default_branch,
            "htmlUrl": html_url,
        }

    def _project_file(
        self,
        payload: object,
        *,
        path: str,
        ref: str | None,
    ) -> dict[str, object]:
        if not isinstance(payload, Mapping) or payload.get("type") != "file":
            raise self._invalid_provider_response()
        sha = self._response_text(payload.get("sha"), "sha", 128, required=True)
        size = payload.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= _MAX_FILE_BYTES:
            raise self._invalid_provider_response()
        raw_content = payload.get("content")
        if not isinstance(raw_content, str):
            # The contents API can expose a download URL for large files.  A
            # provider URL is never followed by this client.
            raise self._invalid_provider_response()
        encoded = "".join(raw_content.split())
        if len(encoded) > ((_MAX_FILE_BYTES + 2) * 4 // 3 + 4):
            raise self._invalid_provider_response()
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise self._invalid_provider_response() from exc
        if len(content) > _MAX_FILE_BYTES or len(content) != size:
            raise self._invalid_provider_response()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "path": path,
                "ref": ref,
                "sha": sha,
                "size": size,
                "encoding": "base64",
                "contentBase64": encoded,
            }
        return {
            "path": path,
            "ref": ref,
            "sha": sha,
            "size": size,
            "encoding": "utf-8",
            "content": text,
        }

    def _project_issue(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise self._invalid_provider_response()
        number = payload.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= _MAX_NUMBER:
            raise self._invalid_provider_response()
        title = self._response_text(payload.get("title"), "title", 1_000, required=True)
        body = self._response_text(payload.get("body"), "body", _MAX_RESULT_TEXT) or ""
        state = payload.get("state")
        if state not in {"open", "closed"}:
            raise self._invalid_provider_response()
        comments = payload.get("comments", 0)
        if isinstance(comments, bool) or not isinstance(comments, int) or not 0 <= comments <= _MAX_NUMBER:
            raise self._invalid_provider_response()
        return {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "comments": comments,
            "htmlUrl": self._response_url(payload.get("html_url")),
        }

    def _project_comment(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise self._invalid_provider_response()
        identifier = payload.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 0:
            raise self._invalid_provider_response()
        body = self._response_text(payload.get("body"), "body", _MAX_RESULT_TEXT, required=True)
        return {
            "id": identifier,
            "body": body,
            "htmlUrl": self._response_url(payload.get("html_url")),
        }

    @staticmethod
    def _response_url(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 2_048:
            raise TeamError(502, "connection_provider_rejected", "GitHub returned an invalid response")
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise TeamError(502, "connection_provider_rejected", "GitHub returned an invalid response") from exc
        if (
            parsed.scheme != "https"
            or hostname != GITHUB_OAUTH_HOST
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or not parsed.path.startswith("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise TeamError(502, "connection_provider_rejected", "GitHub returned an invalid response")
        return value
