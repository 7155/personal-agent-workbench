"""Dedicated-origin HTTP gateway for untrusted project preview applications."""
from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
import json
import logging
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .errors import TeamError
from .gateway import TeamHTTPServer
from .previews import TeamPreviewManager


_LOGGER = logging.getLogger(__name__)
_MAX_BODY = 2 * 1024 * 1024
_MAX_RESPONSE = 16 * 1024 * 1024
_METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}
_REQUEST_HEADERS = {'accept', 'accept-language', 'content-type', 'if-none-match', 'if-modified-since', 'range', 'origin', 'referer'}
_RESPONSE_HEADERS = {'content-type', 'content-encoding', 'content-language', 'content-disposition', 'etag', 'last-modified', 'accept-ranges', 'content-range', 'vary', 'allow', 'location', 'set-cookie'}
_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
    "sandbox allow-scripts allow-same-origin allow-forms allow-downloads"
)


def _reserved_cookie(name: str) -> bool:
    return name.lower().startswith(('paw_', '__host-paw_', '__secure-paw_'))


def _cookies(value: str) -> SimpleCookie:
    jar = SimpleCookie()
    try:
        jar.load(value)
    except Exception:
        pass
    return jar


def _app_cookie(value: str) -> str:
    return '; '.join(morsel.OutputString() for name, morsel in _cookies(value).items() if not _reserved_cookie(name))


def _response_cookie(value: str) -> str | None:
    jar = _cookies(value)
    if len(jar) != 1:
        return None
    name, morsel = next(iter(jar.items()))
    if _reserved_cookie(name) or morsel['domain']:
        return None
    return morsel.OutputString()


class PreviewRequestHandler(BaseHTTPRequestHandler):
    previews: TeamPreviewManager
    protocol_version = 'HTTP/1.0'
    server_version = 'PAWPreview'

    def setup(self) -> None:
        self.request.settimeout(20)
        super().setup()

    def log_message(self, _format: str, *args: object) -> None:
        # Entry URLs contain one-use tickets. Never place them in HTTP logs.
        return

    def _send(self, status: int, body: bytes = b'', headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        for name, value in headers or []:
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', _CSP)
        self.end_headers()
        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def _handle(self) -> None:
        try:
            if self.command not in _METHODS:
                raise TeamError(405, 'method_not_allowed', 'Unsupported preview method')
            config = self.previews.origin
            if config is None:
                raise TeamError(503, 'preview_not_configured', 'Preview gateway is not configured')
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1:
                raise TeamError(400, 'invalid_host', 'Use the assigned preview hostname')
            deployment_id = config.deployment_for_host(hosts[0])
            origin = config.origin(deployment_id)
            if (len(self.path) > 8192 or not self.path.startswith('/') or self.path.startswith('//')
                    or re.search(r'[\x00-\x20\x7f\\]', self.path)):
                raise TeamError(400, 'invalid_path', 'Preview request path is invalid')
            parsed = urlsplit(self.path)
            if self.headers.get('Upgrade') or 'upgrade' in self.headers.get('Connection', '').lower():
                raise TeamError(501, 'preview_upgrade_unavailable', 'WebSocket previews are not supported')
            supplied_origin = self.headers.get('Origin')
            if supplied_origin is not None and supplied_origin != origin:
                raise TeamError(403, 'origin_not_allowed', 'Preview requests must come from this deployment')
            fetch_site = self.headers.get('Sec-Fetch-Site', '')
            mode = self.headers.get('Sec-Fetch-Mode', '')
            if fetch_site not in {'', 'same-origin', 'none'} and not (self.command in {'GET', 'HEAD'} and mode == 'navigate'):
                raise TeamError(403, 'origin_not_allowed', 'Other origins cannot read this project preview')
            if parsed.path == '/__paw/enter':
                if self.command != 'GET':
                    raise TeamError(405, 'method_not_allowed', 'Open preview entry by navigation')
                query = parse_qs(parsed.query, max_num_fields=2)
                if set(query) != {'ticket'} or len(query['ticket']) != 1:
                    raise TeamError(400, 'preview_ticket_invalid', 'Open the preview again from the project')
                lease = self.previews.store.consume_ticket(deployment_id, query['ticket'][0])
                cookie = f"{config.cookie_name}={lease['leaseToken']}; Path=/; Max-Age=900; HttpOnly; SameSite=Lax"
                if config.secure:
                    cookie += '; Secure'
                self._send(303, headers=[('Location', '/'), ('Set-Cookie', cookie)])
                return
            if parsed.path.startswith('/__paw/'):
                raise TeamError(404, 'route_not_found', 'Preview route not found')
            cookies = self.headers.get_all('Cookie', [])
            if len(cookies) > 1:
                raise TeamError(400, 'invalid_cookie', 'Preview cookie header is ambiguous')
            cookie_value = cookies[0] if cookies else ''
            lease_cookie = _cookies(cookie_value).get(config.cookie_name)
            if lease_cookie is None:
                raise TeamError(401, 'preview_login_required', 'Open this preview from the PAW project to sign in')
            action = 'read' if self.command in {'GET', 'HEAD', 'OPTIONS'} else 'write'
            self.previews.store.authorize_lease(deployment_id, lease_cookie.value, action=action)
            if action == 'write' and supplied_origin != origin:
                raise TeamError(403, 'origin_required', 'Preview writes require the deployment origin')
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                raise TeamError(400, 'invalid_body', 'Use a bounded preview request body')
            size = self.headers.get('Content-Length', '0')
            if not size.isdigit() or int(size) > _MAX_BODY:
                raise TeamError(413, 'preview_body_too_large', 'Preview request body is too large')
            body = self.rfile.read(int(size))
            if len(body) != int(size):
                raise TeamError(400, 'invalid_body', 'Preview request body was incomplete')
            headers = {name: value for name, value in self.headers.items() if name.lower() in _REQUEST_HEADERS}
            headers['Host'] = hosts[0]
            app_cookie = _app_cookie(cookie_value)
            if app_cookie:
                headers['Cookie'] = app_cookie
            # A slow upload must not retain the authority from its start.
            self.previews.store.authorize_lease(deployment_id, lease_cookie.value, action=action)
            response = self.previews.runtime.request(deployment_id, self.command, self.path, headers, body)
            # Do not reveal a response after logout/removal while it was running.
            self.previews.store.authorize_lease(deployment_id, lease_cookie.value, action=action)
            status, response_headers, response_body = response.status, response.headers, response.body
            if not 200 <= status <= 599 or len(response_body) > _MAX_RESPONSE:
                raise TeamError(502, 'invalid_preview_response', 'Preview returned an invalid response')
            output: list[tuple[str, str]] = []
            for name, value in response_headers:
                if name.lower() not in _RESPONSE_HEADERS:
                    continue
                if re.search(r'[\r\n\x00]', name + value):
                    raise TeamError(502, 'invalid_preview_response', 'Preview returned invalid headers')
                if name.lower() == 'set-cookie':
                    value = _response_cookie(value)
                    if value is None:
                        continue
                if name.lower() == 'location':
                    # Browsers treat a backslash as a slash in HTTP URLs;
                    # urllib deliberately does not. Reject ambiguous raw URLs
                    # before resolving them, including /\\other-host forms.
                    if re.search(r'[\\\x00-\x20\x7f]', value):
                        raise TeamError(502, 'preview_redirect_blocked', 'Preview redirect is invalid')
                    location = urlsplit(urljoin(origin + '/', value))
                    if location.scheme + '://' + location.netloc != origin:
                        raise TeamError(502, 'preview_redirect_blocked', 'Preview redirects must stay within this deployment')
                output.append((name, value))
            self._send(status, response_body, output)
        except TeamError as exc:
            self._send(exc.status, json.dumps({'ok': False, 'error': exc.message, 'errorCode': exc.code}).encode(), [('Content-Type', 'application/json')])
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            _LOGGER.exception('Project preview request failed')
            self._send(502, b'{"ok":false,"error":"Project preview is unavailable"}', [('Content-Type', 'application/json')])

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle
    do_CONNECT = _handle
    do_TRACE = _handle


def make_preview_server(previews: TeamPreviewManager, *, host: str = '127.0.0.1', port: int = 8771):
    if previews.origin is None:
        raise ValueError('Configure a dedicated preview origin before starting the gateway')
    if host not in {'127.0.0.1', 'localhost', '::1'} and not previews.origin.secure:
        raise ValueError('Remote preview access requires HTTPS through a trusted reverse proxy')

    class Handler(PreviewRequestHandler):
        pass

    Handler.previews = previews
    return TeamHTTPServer((host, port), Handler)
