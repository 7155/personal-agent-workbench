from __future__ import annotations

import base64
from collections import deque
import json
from types import SimpleNamespace
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

from rag_ime.team.connection_github import (
    GITHUB_API_HOST,
    GITHUB_OAUTH_HOST,
    GITHUB_OPERATIONS,
    GITHUB_READ_OPERATIONS,
    GitHubConnectionClient,
    GitHubRequest,
    GitHubResponse,
    PinnedGitHubTransport,
    _PinnedHTTPSConnection,
    normalize_repository,
)
from rag_ime.team.errors import TeamError


def _json_response(value: object, *, status: int = 200) -> GitHubResponse:
    return GitHubResponse(status, (("Content-Type", "application/json"),), json.dumps(value).encode())


class _FakeTransport:
    def __init__(self, *responses: object) -> None:
        self.responses = deque(responses)
        self.requests: list[GitHubRequest] = []

    def request(self, request: GitHubRequest) -> GitHubResponse:
        self.requests.append(request)
        value = self.responses.popleft()
        if isinstance(value, BaseException):
            raise value
        if not isinstance(value, GitHubResponse):
            raise AssertionError("fixture response must be GitHubResponse")
        return value


def _credentials(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {"accessToken": "gho_fixture", "tokenType": "bearer"}
    result.update(overrides)
    return result


class GitHubOAuthTests(unittest.TestCase):
    def test_authorization_url_is_fixed_pkce_github_endpoint(self) -> None:
        client = GitHubConnectionClient(client_id="client-123")
        challenge = "c" * 43
        url = client.authorization_url(
            redirect_uri="https://paw.example.test/api/team/connections/github/callback",
            state="state-" + "x" * 20,
            code_challenge=challenge,
        )
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, GITHUB_OAUTH_HOST)
        self.assertEqual(parsed.path, "/login/oauth/authorize")
        query = parse_qs(parsed.query)
        self.assertEqual(query["client_id"], ["client-123"])
        self.assertEqual(query["scope"], ["repo read:user"])
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["prompt"], ["select_account"])
        self.assertNotIn("client_secret", query)

    def test_authorization_url_rejects_weak_pkce_and_non_https_callback(self) -> None:
        client = GitHubConnectionClient(client_id="client-123")
        with self.assertRaisesRegex(TeamError, "PKCE"):
            client.authorization_url(
                redirect_uri="https://paw.example.test/callback",
                state="state-" + "x" * 20,
                code_challenge="short",
            )
        with self.assertRaisesRegex(TeamError, "loopback"):
            client.authorization_url(
                redirect_uri="http://example.test/callback",
                state="state-" + "x" * 20,
                code_challenge="c" * 43,
            )

    def test_exchange_and_refresh_project_rotated_credential_bundle(self) -> None:
        transport = _FakeTransport(
            _json_response(
                {
                    "access_token": "gho_new",
                    "token_type": "bearer",
                    "expires_in": 60,
                    "refresh_token": "ghr_new",
                    "refresh_token_expires_in": 600,
                    "scope": "repo read:user",
                }
            ),
            _json_response(
                {
                    "access_token": "gho_rotated",
                    "token_type": "bearer",
                    "expires_in": 120,
                    "refresh_token": "ghr_rotated",
                    "refresh_token_expires_in": 500,
                }
            ),
        )
        client = GitHubConnectionClient(
            client_id="client-123",
            client_secret="client-secret",
            transport=transport,
            now_ms=lambda: 1_000,
        )
        bundle = client.exchange_code(
            code="oauth-code",
            verifier="v" * 43,
            redirect_uri="https://paw.example.test/callback",
        )
        self.assertEqual(
            bundle,
            {
                "accessToken": "gho_new",
                "tokenType": "bearer",
                "expiresAtMs": 61_000,
                "refreshToken": "ghr_new",
                "refreshExpiresAtMs": 601_000,
            },
        )
        form = parse_qs(transport.requests[0].body.decode("ascii"))
        self.assertEqual(form["code_verifier"], ["v" * 43])
        self.assertEqual(form["client_secret"], ["client-secret"])
        self.assertEqual(transport.requests[0].host, GITHUB_OAUTH_HOST)
        self.assertEqual(transport.requests[0].path, "/login/oauth/access_token")

        rotated = client.refresh_token(bundle)
        self.assertEqual(rotated["accessToken"], "gho_rotated")
        refresh_form = parse_qs(transport.requests[1].body.decode("ascii"))
        self.assertEqual(refresh_form["grant_type"], ["refresh_token"])
        self.assertEqual(refresh_form["refresh_token"], ["ghr_new"])


class GitHubRepositoryValidationTests(unittest.TestCase):
    def test_normalize_repository_lowercases_and_allows_leading_dot_repo(self) -> None:
        self.assertEqual(normalize_repository("Acme/.github"), "acme/.github")
        self.assertEqual(normalize_repository("OWNER/Repo_Name-1"), "owner/repo_name-1")

    def test_normalize_repository_rejects_path_url_and_traversal_forms(self) -> None:
        for value in (
            "../secret",
            "acme/..",
            "acme/.",
            "acme/foo/bar",
            "acme\\repo",
            "acme/repo%2Fother",
            "https://github.com/acme/repo",
            "acme/repo?ref=main",
            "acme/repo#fragment",
            " acme/repo",
            "acme/repo ",
        ):
            with self.subTest(value=value), self.assertRaises(TeamError):
                normalize_repository(value)


class GitHubOperationTests(unittest.TestCase):
    def test_account_projects_only_login_and_id(self) -> None:
        transport = _FakeTransport(
            _json_response(
                {
                    "login": "octocat",
                    "id": 583231,
                    "email": "private@example.test",
                    "token": "must-not-project",
                }
            )
        )
        client = GitHubConnectionClient(transport=transport, now_ms=1_000)
        self.assertEqual(client.account(_credentials()), {"login": "octocat", "id": "583231"})
        self.assertEqual(transport.requests[0].path, "/user")
        self.assertEqual(transport.requests[0].host, GITHUB_API_HOST)
        self.assertEqual(transport.requests[0].headers["Authorization"], "Bearer gho_fixture")

    def test_execute_projects_bounded_repo_file_issue_results_and_fixed_routes(self) -> None:
        file_content = base64.b64encode(b"hello\n").decode()
        transport = _FakeTransport(
            _json_response(
                {
                    "id": 1,
                    "name": "demo",
                    "full_name": "Acme/Demo",
                    "private": True,
                    "description": "A demo",
                    "default_branch": "main",
                    "html_url": "https://github.com/Acme/Demo",
                    "secrets": "do-not-project",
                }
            ),
            _json_response(
                {
                    "type": "file",
                    "sha": "abc123",
                    "size": 6,
                    "content": file_content,
                    "download_url": "https://evil.example.test/file",
                }
            ),
            _json_response(
                [
                    {
                        "number": 7,
                        "title": "Bug",
                        "body": "Details",
                        "state": "open",
                        "comments": 2,
                        "html_url": "https://github.com/Acme/Demo/issues/7",
                        "labels": ["do-not-project"],
                    }
                ]
            ),
            _json_response(
                {
                    "number": 7,
                    "title": "Bug",
                    "body": "Details",
                    "state": "open",
                    "comments": 2,
                    "html_url": "https://github.com/Acme/Demo/issues/7",
                }
            ),
            _json_response(
                {
                    "number": 8,
                    "title": "New",
                    "body": "Body",
                    "state": "open",
                    "comments": 0,
                    "html_url": "https://github.com/Acme/Demo/issues/8",
                },
                status=201,
            ),
            _json_response(
                {
                    "id": 91,
                    "body": "Thanks",
                    "html_url": "https://github.com/Acme/Demo/issues/7#issuecomment-91",
                },
                status=201,
            ),
        )
        client = GitHubConnectionClient(transport=transport, now_ms=1_000)
        credentials = _credentials()
        repo = client.execute(credentials, "repo.read", "Acme/Demo", {})
        self.assertEqual(repo["fullName"], "Acme/Demo")
        self.assertNotIn("secrets", repo)
        file_result = client.execute(
            credentials,
            "file.read",
            "acme/demo",
            {"path": "src/main.py", "ref": "feature/demo"},
        )
        self.assertEqual(file_result["content"], "hello\n")
        self.assertEqual(file_result["encoding"], "utf-8")
        self.assertNotIn("download_url", file_result)
        issues = client.execute(credentials, "issues.list", "acme/demo", {"state": "open", "page": 2})
        self.assertEqual(issues["items"][0]["number"], 7)
        issue = client.execute(credentials, "issue.read", "acme/demo", {"number": 7})
        self.assertEqual(issue["title"], "Bug")
        created = client.execute(
            credentials,
            "issue.create",
            "acme/demo",
            {"title": "New", "body": "Body"},
        )
        self.assertEqual(created["number"], 8)
        comment = client.execute(
            credentials,
            "issue.comment",
            "acme/demo",
            {"number": 7, "body": "Thanks"},
        )
        self.assertEqual(comment["id"], 91)
        self.assertEqual(
            [(request.method, request.path) for request in transport.requests],
            [
                ("GET", "/repos/acme/demo"),
                ("GET", "/repos/acme/demo/contents/src/main.py?ref=feature%2Fdemo"),
                ("GET", "/repos/acme/demo/issues?state=open&per_page=50&page=2"),
                ("GET", "/repos/acme/demo/issues/7"),
                ("POST", "/repos/acme/demo/issues"),
                ("POST", "/repos/acme/demo/issues/7/comments"),
            ],
        )

    def test_invalid_operation_arguments_and_paths_do_not_call_transport(self) -> None:
        transport = _FakeTransport()
        client = GitHubConnectionClient(transport=transport, now_ms=1_000)
        cases = (
            ("unknown", "acme/demo", {}),
            ("repo.read", "acme/demo.git", {}),
            ("file.read", "acme/demo", {"path": "../secret"}),
            ("file.read", "acme/demo", {"path": "notes%2Fsecret"}),
            ("file.read", "acme/demo", {"path": "notes", "ref": None}),
            ("file.read", "acme/demo", {"path": "note", "extra": True}),
            ("issues.list", "acme/demo", {"state": "pending"}),
            ("issue.read", "acme/demo", {"number": 0}),
            ("issue.create", "acme/demo", {"title": "x", "body": "b", "requestId": "x"}),
        )
        for operation, repository, args in cases:
            with self.subTest(operation=operation, args=args), self.assertRaises(TeamError):
                client.execute(_credentials(), operation, repository, args)
        self.assertEqual(transport.requests, [])

    def test_expired_and_malformed_credentials_require_reconnect(self) -> None:
        client = GitHubConnectionClient(transport=_FakeTransport(), now_ms=2_000)
        with self.assertRaises(TeamError) as expired:
            client.account(_credentials(expiresAtMs=2_000))
        self.assertEqual(expired.exception.code, "connection_reconnect_required")
        with self.assertRaises(TeamError) as malformed:
            client.refresh_token(
                {
                    "tokenType": "bearer",
                    "refreshToken": "r",
                    "refreshExpiresAtMs": 2_000,
                }
            )
        self.assertEqual(malformed.exception.code, "connection_reconnect_required")

    def test_write_arguments_preserve_markdown_spacing_and_allow_empty_body(self) -> None:
        transport = _FakeTransport(
            _json_response(
                {
                    "number": 9,
                    "title": "New",
                    "body": "",
                    "state": "open",
                    "comments": 0,
                },
                status=201,
            )
        )
        client = GitHubConnectionClient(transport=transport, now_ms=1_000)
        client.execute(
            _credentials(),
            "issue.create",
            "acme/demo",
            {"title": "  New  ", "body": "  indented\n"},
        )
        request_body = json.loads(transport.requests[0].body.decode("utf-8"))
        self.assertEqual(request_body, {"title": "  New  ", "body": "  indented\n"})


class GitHubFailureBoundaryTests(unittest.TestCase):
    def test_provider_errors_are_safe_and_writes_are_not_retried(self) -> None:
        transport = _FakeTransport(
            GitHubResponse(500, (), b'{"message":"secret provider detail"}')
        )
        client = GitHubConnectionClient(transport=transport, now_ms=1_000)
        with self.assertRaises(TeamError) as caught:
            client.execute(
                _credentials(),
                "issue.create",
                "acme/demo",
                {"title": "New", "body": "Body"},
            )
        self.assertEqual(caught.exception.code, "connection_outcome_unknown")
        self.assertNotIn("secret provider detail", str(caught.exception))
        self.assertEqual(len(transport.requests), 1)

    def test_accepted_write_with_invalid_response_is_outcome_unknown(self) -> None:
        transport = _FakeTransport(
            GitHubResponse(201, (), b"x" * (8 * 1024 * 1024 + 1)),
            GitHubResponse(201, (), b"not-json"),
            _json_response({"number": "not-an-issue"}, status=201),
        )
        client = GitHubConnectionClient(transport=transport, now_ms=1_000)
        for operation, args in (
            ("issue.create", {"title": "New", "body": "Body"}),
            ("issue.create", {"title": "New", "body": "Body"}),
            ("issue.comment", {"number": 7, "body": "Thanks"}),
        ):
            with self.subTest(operation=operation), self.assertRaises(TeamError) as caught:
                client.execute(_credentials(), operation, "acme/demo", args)
            self.assertEqual(caught.exception.code, "connection_outcome_unknown")
        self.assertEqual(len(transport.requests), 3)

    def test_unauthorized_and_redirect_are_classified_without_following(self) -> None:
        transport = _FakeTransport(
            GitHubResponse(401, (), b'{"message":"token detail"}'),
            GitHubResponse(302, (("Location", "https://evil.example.test"),), b"redirect"),
        )
        client = GitHubConnectionClient(transport=transport, now_ms=1_000)
        with self.assertRaises(TeamError) as unauthorized:
            client.account(_credentials())
        self.assertEqual(unauthorized.exception.code, "connection_reconnect_required")
        with self.assertRaises(TeamError) as redirected:
            client.account(_credentials())
        self.assertEqual(redirected.exception.code, "connection_provider_rejected")
        self.assertEqual(len(transport.requests), 2)

    def test_refresh_unauthorized_requires_reconnect(self) -> None:
        transport = _FakeTransport(GitHubResponse(401, (), b'{"message":"expired"}'))
        client = GitHubConnectionClient(client_id="client-123", transport=transport, now_ms=1_000)
        with self.assertRaises(TeamError) as caught:
            client.refresh_token(
                {
                    "tokenType": "bearer",
                    "refreshToken": "ghr_fixture",
                    "refreshExpiresAtMs": 2_000,
                }
            )
        self.assertEqual(caught.exception.code, "connection_reconnect_required")

    def test_transport_exception_is_mapped_without_provider_details(self) -> None:
        def broken(_request: GitHubRequest) -> GitHubResponse:
            raise RuntimeError("secret transport implementation detail")

        client = GitHubConnectionClient(transport=broken, now_ms=1_000)
        with self.assertRaises(TeamError) as caught:
            client.account(_credentials())
        self.assertEqual(caught.exception.code, "connection_outcome_unknown")
        self.assertNotIn("secret transport implementation detail", str(caught.exception))

    def test_deadline_abort_shuts_down_response_file_and_socketpair(self) -> None:
        reader_socket, peer_socket = socket.socketpair()
        response_file = reader_socket.makefile("rb")
        connection = _PinnedHTTPSConnection("api.github.com", "127.0.0.1", timeout=5)
        connection.sock = reader_socket
        connection._owned_socket = reader_socket
        connection._response = SimpleNamespace(fp=response_file)
        started = threading.Event()
        finished = threading.Event()

        def blocked_read() -> None:
            started.set()
            try:
                response_file.read(1)
            except (OSError, ValueError):
                pass
            finally:
                finished.set()

        reader = threading.Thread(target=blocked_read, daemon=True)
        reader.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.01)
        connection.abort()
        self.assertTrue(finished.wait(1))
        self.assertTrue(connection._cancelled.is_set())
        response_file.close()
        reader_socket.close()
        peer_socket.close()

    def test_connection_close_response_body_survives_internal_close(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        local_port = listener.getsockname()[1]
        server_errors: list[BaseException] = []

        def serve_response() -> None:
            try:
                peer, _address = listener.accept()
                try:
                    peer.recv(4096)
                    peer.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Connection: close\r\n"
                        b"Content-Length: 5\r\n"
                        b"Content-Type: application/json\r\n"
                        b"\r\nhello"
                    )
                finally:
                    peer.close()
            except BaseException as exc:  # pragma: no cover - only reports server fixture failures.
                server_errors.append(exc)

        server = threading.Thread(target=serve_response, daemon=True)
        server.start()
        real_create_connection = socket.create_connection

        def connect_to_fixture(_address, timeout=None, source_address=None):
            return real_create_connection(("127.0.0.1", local_port), timeout, source_address)

        class _PlainContext:
            def wrap_socket(self, raw, *, server_hostname):
                del server_hostname
                return raw

        try:
            with patch(
                "rag_ime.team.connection_github._resolve_public_ip",
                return_value="127.0.0.1",
            ), patch(
                "rag_ime.team.connection_github.socket.create_connection",
                side_effect=connect_to_fixture,
            ), patch(
                "rag_ime.team.connection_github.ssl.create_default_context",
                return_value=_PlainContext(),
            ):
                result = PinnedGitHubTransport(timeout_seconds=5).request(
                    GitHubRequest("GET", GITHUB_API_HOST, "/user")
                )
        finally:
            listener.close()
            server.join(1)
        self.assertEqual(result.body, b"hello")
        self.assertEqual(server_errors, [])

    def test_oversized_or_invalid_json_response_fails_without_raw_body(self) -> None:
        oversized = GitHubResponse(200, (), b"x" * (8 * 1024 * 1024 + 1))
        transport = _FakeTransport(oversized, GitHubResponse(200, (), b"not-json"))
        client = GitHubConnectionClient(transport=transport, now_ms=1_000)
        for _ in range(2):
            with self.assertRaisesRegex(TeamError, "GitHub"):
                client.account(_credentials())
        self.assertEqual(len(transport.requests), 2)

    def test_public_dns_filter_rejects_private_addresses(self) -> None:
        with patch(
            "rag_ime.team.connection_github.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(OSError):
                from rag_ime.team.connection_github import _resolve_public_ip

                _resolve_public_ip(GITHUB_API_HOST)

    def test_transport_configuration_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            PinnedGitHubTransport(timeout_seconds=0.1)
        with self.assertRaises(ValueError):
            PinnedGitHubTransport(max_response_bytes=9 * 1024 * 1024)
        with self.assertRaises(ValueError):
            PinnedGitHubTransport(max_response_bytes=1.5)  # type: ignore[arg-type]

    def test_transport_request_cannot_override_host_or_proxy_headers(self) -> None:
        for header in ("Host", "Content-Length", "Proxy-Authorization", "Transfer-Encoding"):
            with self.subTest(header=header), self.assertRaises(ValueError):
                GitHubRequest("GET", GITHUB_API_HOST, "/user", {header: "fixture"})


if __name__ == "__main__":
    unittest.main()
