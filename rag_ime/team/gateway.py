"""Cookie-authenticated team HTTP entry over the existing PAW handlers."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from http import HTTPStatus
from http.cookies import SimpleCookie
import hmac
import json
import logging
import mimetypes
from pathlib import Path
import re
from threading import BoundedSemaphore, RLock
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..control_api.errors import ControlApiError
from ..debug_server import DebugRequestHandler, QuietThreadingHTTPServer
from ..secure_files import regular_reader
from .access import AuthorizedTeamRequest, TeamAccessPolicy
from .errors import TeamError
from .grants import TeamGrantStore, execution_session_key
from .identity import TeamIdentityStore
from .origins import canonical_http_origin


_SPACE_ROUTE = re.compile(r'^/team/spaces/([A-Za-z0-9_-]{1,128})(/api/.*)$')
_KNOWLEDGE_IMPORT = re.compile(r'^/api/knowledge-bases/[^/]{1,128}/documents/import$')
_MEMBER_ROUTE = re.compile(r'^/api/team/members/([A-Za-z0-9_-]{1,128})/status$')
_PROJECT_MEMBERS = re.compile(r'^/api/team/projects/([A-Za-z0-9_-]{1,128})/members(?:/([A-Za-z0-9_-]{1,128}))?$')
_PROJECT_DRAFTS = re.compile(r'^/api/team/projects/([A-Za-z0-9_-]{1,128})/drafts(?:/([A-Za-z0-9_-]{1,128})(/(?:integrate|adopt|diff))?)?$')
_PROJECT_CONTEXT = re.compile(r'^/api/team/projects/([A-Za-z0-9_-]{1,128})/(overview|brief)$')
_PROJECT_PREVIEW = re.compile(r'^/api/team/projects/([A-Za-z0-9_-]{1,128})/preview(/(?:open|stop))?$')
_PROJECT_SESSION_REQUIREMENTS = re.compile(r'^/api/team/projects/([A-Za-z0-9_-]{1,128})/sessions/([^/]{1,768})/requirements$')
_CONNECTIONS = re.compile(r'^/api/team/spaces/([A-Za-z0-9_-]{1,128})/connections(?P<operation>/token|/oauth/start|/grants|/[A-Za-z0-9_-]{1,128}/revoke|/grants/[A-Za-z0-9_-]{1,128}/revoke)?$')
_RESOURCE_STATUS = re.compile(r'^/api/team/resources/([A-Za-z0-9_-]{1,128})/status$')
_SPACE_RESOURCES = re.compile(r'^/api/team/spaces/([A-Za-z0-9_-]{1,128})/resources$')
_SESSION_RESOURCES = re.compile(r'^/api/team/spaces/([A-Za-z0-9_-]{1,128})/sessions/([^/]{1,768})/resources$')
_LOGGER = logging.getLogger(__name__)
_KNOWLEDGE_UPLOAD_SECONDS = 30.0


class TeamApplication:
    def __init__(
        self, data_root: Path, static_dir: Path, *,
        public_origin: str = '', service_factory: Callable[[Mapping[str, object]], Any] | None = None,
        execution: Any | None = None,
        preview_runtime: Any | None = None, preview_origin: str = '',
        connections_config: Mapping[str, object] | None = None, connection_client: Any | None = None,
        package_source_root: Path | None = None, package_catalog: Path | None = None,
    ) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.static_dir = static_dir.resolve()
        self.public_origin = canonical_http_origin(public_origin) if public_origin else ''
        self.identity = TeamIdentityStore(self.data_root / 'team.sqlite')
        self.identity.initialize()
        self.grants = TeamGrantStore(self.identity.db_path, self.identity)
        self.access = TeamAccessPolicy(self.identity, self.grants)
        from .shared_resources import TeamSharedResources
        self.shared_resources = TeamSharedResources(self, source_root=package_source_root, catalog_path=package_catalog)
        self.execution = execution
        if execution is not None:
            execution.bind_application(self)
        self._workspaces: Any | None = None
        self._service_factory = service_factory
        self._services: dict[str, Any] = {}
        self._lock = RLock()
        from .previews import TeamPreviewManager
        self.previews = TeamPreviewManager(self, runtime=preview_runtime, origin_template=preview_origin)
        from .connections import TeamConnections
        self.connections = TeamConnections(self, config=connections_config, client=connection_client)

    @property
    def workspaces(self):
        with self._lock:
            if self._workspaces is None:
                from .workspaces import TeamWorkspaceService
                self._workspaces = TeamWorkspaceService(
                    self.data_root / 'projects', self.identity, self.grants,
                    self.quiesce_session, self.verify_candidate,
                )
            return self._workspaces

    @contextmanager
    def quiesce_session(self, session_id: str):
        binding = self.grants.binding(session_id)
        if self.execution is not None:
            with self.execution.quiesce(session_id):
                yield
        else:
            # A disabled runtime is not proof that workers from a prior server
            # process disappeared. Retained receipts require controller cleanup.
            execution_root = self.data_root / 'execution'
            roots = [execution_root / execution_session_key(session_id)]
            operations = execution_root / 'operations'
            if operations.is_dir() and not operations.is_symlink():
                roots.extend(operations.iterdir())
            for root in roots:
                record = root / 'active-container.json'
                if record.exists():
                    with regular_reader(record) as handle:
                        active = json.loads(handle.read(8192))
                    if active.get('sessionId') == session_id:
                        raise TeamError(503, 'worker_cleanup_pending', 'Configure the execution controller to stop the previous worker before publishing')
            self.grants.binding(str(binding['sessionId']))
            yield

    def verify_candidate(self, path: Path, context: Mapping[str, object]):
        if self.execution is None:
            raise TeamError(503, 'team_worker_unavailable', 'Configure isolated validation before integrating a draft')
        return self.execution.verify_candidate(path, context)

    def service(self, space: Mapping[str, object]) -> Any:
        space_id = str(space['id'])
        with self._lock:
            if space_id not in self._services:
                if self._service_factory is None:
                    from .services import create_space_service
                    self._services[space_id] = create_space_service(self, space)
                else:
                    self._services[space_id] = self._service_factory(space)
            return self._services[space_id]

    def close(self) -> None:
        self.connections.close()
        self.previews.begin_close()
        with self._lock:
            services, self._services = self._services, {}
        try:
            for service in services.values():
                close = getattr(service, 'close', None)
                if callable(close):
                    close()
        finally:
            try:
                # Cancel any candidate verifier before waiting for preview
                # startup jobs that may be awaiting its result.
                if self.execution is not None:
                    self.execution.close()
            finally:
                self.previews.close()

    def revoke_user(self, user_id: str, *, space_id: str | None = None) -> None:
        sessions = self.grants.revoke_user(user_id, space_id=space_id)
        if self.execution is not None:
            try:
                self.execution.revoke_user(user_id, space_id=space_id)
            except Exception:
                _LOGGER.exception('Workspace worker stop is pending after grant revocation')
        for session_id in sessions:
            binding = self.grants.binding(session_id, check_current=False)
            with self._lock:
                service = self._services.get(str(binding['spaceId']))
            if self.execution is not None:
                try:
                    self.execution.revoke_session(session_id)
                except Exception:
                    _LOGGER.exception('Container stop is pending after grant revocation')
            if service is not None:
                try:
                    service.agent.abort(session_id)
                except Exception:
                    # The persistent grant is already revoked. Failure to stop
                    # is recoverable; it must never restore API/tool authority.
                    _LOGGER.exception('Could not stop a revoked team Session')


class TeamRequestHandler(DebugRequestHandler):
    team: TeamApplication

    def log_message(self, fmt: str, *args: object) -> None:
        if urlsplit(self.path).path == '/api/team/connections/github/callback':
            return
        super().log_message(fmt, *args)

    def setup(self) -> None:
        self.request.settimeout(15)
        super().setup()

    def do_GET(self) -> None:  # noqa: N802
        self._handle_team('GET')

    def do_POST(self) -> None:  # noqa: N802
        self._handle_team('POST')

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_team('PATCH')

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_team('DELETE')

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(405)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _handle_team(self, method: str) -> None:
        self._team_request: AuthorizedTeamRequest | None = None
        self._team_login: dict[str, object] | None = None
        self._request_body_bytes = None
        parsed = urlsplit(self.path)
        try:
            if parsed.path.startswith('/api/team/'):
                self._team_api(method, parsed.path)
                return
            match = _SPACE_ROUTE.fullmatch(parsed.path)
            if match is None:
                if method == 'GET' and not parsed.path.startswith(('/api/', '/team/')):
                    self._static(parsed.path)
                    return
                raise TeamError(404, 'route_not_found', 'Team route not found')
            login = self._authenticate()
            space_id, path = match.groups()
            raw_import = method == 'POST' and _KNOWLEDGE_IMPORT.fullmatch(path) is not None
            body = self._checked_body(method, raw_import=raw_import)
            query = self._control_query(parsed.query)
            request = self.team.access.authorize_http(
                str(login['user']['id']), space_id, method=method, path=path, query=query, body=body,
            )
            request = replace(request, actor_display_name=str(login['user']['displayName'] or login['user']['username']))
            self._team_request = request
            self.service = self.team.service(request.space)
            self.path = path + ('?' + parsed.query if parsed.query else '')
            if path == '/api/health':
                self._write_json(HTTPStatus.OK, {'ok': True, 'service': 'PAW Team', 'spaceId': space_id})
                return
            prepare = getattr(self.service, 'prepare_team_request', None)
            if callable(prepare):
                body = prepare(request)
                if not raw_import:
                    self._request_body_bytes = json.dumps(body).encode()
            begin = getattr(self.service, 'team_request_context', None)
            with begin(request) if callable(begin) else nullcontext():
                getattr(super(), 'do_' + method)()
        except TeamError as exc:
            self._team_error(exc)
        except ControlApiError as exc:
            self._team_error(TeamError(exc.status, exc.code.value, exc.message))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._team_error(TeamError(400, 'invalid_request', str(exc)))
        except Exception:
            _LOGGER.exception('Team request failed')
            self._team_error(TeamError(500, 'team_request_failed', 'The request could not be completed'))

    def _origin(self) -> str:
        if self.team.public_origin:
            return self.team.public_origin
        host, port = self.server.server_address[:2]
        if host not in {'127.0.0.1', '::1', 'localhost'}:
            raise TeamError(503, 'public_origin_required', 'Configure the public HTTPS origin for this team server')
        return f'http://[{host}]:{port}' if ':' in host else f'http://{host}:{port}'

    def _cookie_name(self) -> str:
        return '__Host-paw_team' if self._origin().startswith('https:') else 'paw_team_dev'

    def _cookie_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception as exc:
            raise TeamError(401, 'login_required', 'Sign in to PAW Team') from exc
        item = cookie.get(self._cookie_name())
        return item.value if item is not None else ''

    def _authenticate(self) -> dict[str, object]:
        token = self._cookie_token()
        if not token:
            raise TeamError(401, 'login_required', 'Sign in to PAW Team')
        self._team_login = self.team.identity.authenticate(token)
        return self._team_login

    def _checked_body(self, method: str, *, login: bool = False, raw_import: bool = False) -> dict[str, object]:
        if method == 'GET':
            return {}
        if self.headers.get('Origin', '').rstrip('/') != self._origin():
            raise TeamError(403, 'origin_not_allowed', 'Request origin is not allowed')
        empty_delete = method == 'DELETE' and self.headers.get('Content-Length', '0') == '0'
        if not empty_delete and not raw_import and self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise TeamError(415, 'json_required', 'Team writes require JSON')
        if not login:
            current = self._team_login or self._authenticate()
            supplied = self.headers.get('X-CSRF-Token', '')
            if not supplied or not hmac.compare_digest(supplied, str(current['csrfToken'])):
                raise TeamError(403, 'csrf_required', 'Refresh the page before submitting this change')
        if self.headers.get_all('Transfer-Encoding', []):
            raise TeamError(400, 'invalid_request', 'Use a bounded JSON request body')
        if raw_import:
            # This exception is only for the canonical, authorized document
            # import route. The ordinary handler retains ownership of reading
            # bytes and the Knowledge facade of intake, storage and indexing.
            # Reject framing before reading or allocating the declared body.
            from .knowledge import TEAM_KNOWLEDGE_MAX_SOURCE_BYTES
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or re.fullmatch(r'[0-9]{1,9}', lengths[0]) is None or int(lengths[0]) <= 0:
                raise TeamError(400, 'invalid_request', 'Provide one non-empty Content-Length for this document')
            if int(lengths[0]) > TEAM_KNOWLEDGE_MAX_SOURCE_BYTES:
                raise TeamError(413, 'knowledge_source_too_large', '单个团队资料不能超过 8 MiB。')
            if any(value.strip() for value in self.headers.get_all('Content-Encoding', [])):
                raise TeamError(415, 'knowledge_encoding_not_allowed', 'Upload the original document without HTTP compression')
            return {}
        return {} if empty_delete else self._read_json()

    def _shared_resources_api(self, actor: str, method: str, path: str, body: dict[str, object]) -> tuple[dict[str, object], int]:
        resources = self.team.shared_resources
        if path == '/api/team/resources' and method == 'GET':
            return {'items': resources.store.list_published(actor)}, 200
        if path == '/api/team/resources/catalog' and method == 'GET':
            return {'items': resources.catalog_items(actor)}, 200
        if path == '/api/team/resources/publish' and method == 'POST':
            return {'resource': resources.publish(actor, body)}, 201
        if status := _RESOURCE_STATUS.fullmatch(path):
            if method != 'POST' or set(body) != {'status'}:
                raise TeamError(400, 'invalid_request', 'Use POST with only the distribution status')
            return {'resource': resources.store.set_status(actor, status[1], body['status'])}, 200
        if space := _SPACE_RESOURCES.fullmatch(path):
            if method == 'GET':
                result = resources.store.selection(actor, space[1])
            elif method == 'POST' and set(body) == {'baseRevision', 'publicationIds'}:
                result = resources.store.select(actor, space[1], body['baseRevision'], body['publicationIds'])
            else:
                raise TeamError(400, 'invalid_request', 'Use GET or POST with baseRevision and publicationIds')
            self.team.identity.require_space(actor, space[1])
            return result, 200
        if session := _SESSION_RESOURCES.fullmatch(path):
            if method != 'GET':
                raise TeamError(405, 'method_not_allowed', 'Task resource snapshots are immutable')
            session_id = unquote(session[2])
            result = resources.store.session_snapshot(actor, session[1], session_id)
            self.team.grants.require_session(actor, session[1], session_id, action='read')
            return result, 200
        raise TeamError(404, 'resource_not_found', 'Resource operation not found')

    def _team_api(self, method: str, path: str) -> None:
        if path == '/api/team/connections/github/callback' and method == 'GET':
            # OAuth codes and provider errors stay outside the frontend. Even
            # a lost/replaced login is a safe, token-free redirect outcome.
            location = '/?teamConnection=failed'
            try:
                current = self._authenticate()
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=8)
                self.team.connections.finish_oauth(str(current['user']['id']), str(current['sessionId']), query)
                location = '/?teamConnection=connected'
            except Exception:
                # No exception path may serialize provider details or a code
                # into the callback page. A failed handshake is single-use.
                pass
            self.send_response(303)
            self.send_header('Location', location)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if method == 'GET' and path == '/api/team/status':
            self._team_json(200, {'enabled': True, 'name': 'PAW Team'})
            return
        if method == 'POST' and path == '/api/team/login':
            body = self._checked_body(method, login=True)
            if set(body) - {'username', 'password'}:
                raise TeamError(400, 'invalid_request', 'Unsupported sign-in field')
            result = self.team.identity.login(body.get('username'), body.get('password'))
            self._team_json(200, {
                'ok': True, 'user': result['user'], 'csrfToken': result['csrfToken'],
                'spaces': self.team.identity.list_spaces(str(result['user']['id'])),
            }, cookie=str(result['token']))
            return
        current = self._authenticate()
        actor = str(current['user']['id'])
        body = self._checked_body(method)
        result: dict[str, object]
        status = 200
        if method == 'GET' and path == '/api/team/me':
            result = {'user': current['user'], 'csrfToken': current['csrfToken'], 'spaces': self.team.identity.list_spaces(actor)}
        elif path.startswith('/api/team/resources') or _SPACE_RESOURCES.fullmatch(path) or _SESSION_RESOURCES.fullmatch(path):
            result, status = self._shared_resources_api(actor, method, path, body)
            self._authenticate()
        elif connections := _CONNECTIONS.fullmatch(path):
            space_id, operation = connections.groups()
            self.team.identity.require_space(actor, space_id)
            if method == 'GET' and operation is None:
                result = self.team.connections.list(actor, space_id)
            elif method == 'POST' and operation == '/token':
                result = {'connection': self.team.connections.connect_token(actor, space_id, str(current['sessionId']), body)}
                status = 201
            elif method == 'POST' and operation == '/oauth/start':
                result = {'authorizationUrl': self.team.connections.start_oauth(actor, space_id, str(current['sessionId']), body)}
            elif method == 'POST' and operation == '/grants':
                result = {'grant': self.team.connections.create_grant(actor, space_id, body)}
                status = 201
            elif method == 'POST' and operation and operation.endswith('/revoke'):
                if body:
                    raise TeamError(400, 'connection_invalid_input', 'Revocation does not accept parameters')
                self.team.connections._enabled()
                if operation.startswith('/grants/'):
                    result = {'grant': self.team.connections.store.revoke_grant(actor, space_id, operation.split('/')[2])}
                else:
                    result = {'connection': self.team.connections.store.revoke_connection(actor, space_id, operation.split('/')[1])}
            else:
                raise TeamError(405, 'method_not_allowed', 'Unsupported connection operation')
            self._authenticate()
            self.team.identity.require_space(actor, space_id)
        elif method == 'POST' and path == '/api/team/logout':
            self.team.identity.logout(self._cookie_token())
            self._team_json(200, {'ok': True}, cookie='')
            return
        elif path == '/api/team/directory' and method == 'GET':
            result = {'items': self.team.identity.member_directory(actor)}
        elif path == '/api/team/members' and method == 'GET':
            result = {'items': self.team.identity.list_members(actor)}
        elif path == '/api/team/members' and method == 'POST':
            result = {'user': self.team.identity.create_member(
                actor, body.get('username'), body.get('password'),
                display_name=body.get('displayName', ''), role=body.get('role', 'member'),
            )}
            status = 201
        elif (member := _MEMBER_ROUTE.fullmatch(path)) and method == 'POST':
            if not isinstance(body.get('active'), bool):
                raise TeamError(400, 'invalid_request', 'active must be a boolean')
            result = {'user': self.team.identity.set_member_active(actor, member[1], body['active'])}
            if not body['active']:
                self.team.revoke_user(member[1])
        elif path == '/api/team/projects' and method == 'POST':
            result = {'space': self.team.identity.create_project(actor, body.get('name'))}
            status = 201
        elif requirements := _PROJECT_SESSION_REQUIREMENTS.fullmatch(path):
            if method != 'POST':
                raise TeamError(405, 'method_not_allowed', 'Use POST to update a Session requirements baseline')
            if set(body) != {'baseRevision', 'revision'}:
                raise TeamError(400, 'invalid_request', 'Provide only baseRevision and revision')
            space_id, encoded_session = requirements.groups()
            space = self.team.identity.require_space(actor, space_id, action='write')
            if space['kind'] != 'project':
                raise TeamError(404, 'space_not_found', 'Project not found')
            session_id = unquote(encoded_session)
            self.team.grants.require_session(actor, space_id, session_id, action='write')
            result = self.team.workspaces.accept_requirements(
                session_id, actor, base_revision=body['baseRevision'], revision=body['revision'],
            )
        elif project_context := _PROJECT_CONTEXT.fullmatch(path):
            from .project_context import TeamProjectContext
            space_id, operation = project_context.groups()
            context = TeamProjectContext(self.team)
            if operation == 'overview' and method == 'GET':
                result = context.overview(actor, space_id)
            elif operation == 'brief' and method == 'POST':
                result = {'brief': context.publish_brief(actor, space_id, body)}
            else:
                raise TeamError(405, 'method_not_allowed', 'Unsupported project context operation')
            self.team.identity.require_space(actor, space_id)
        elif preview := _PROJECT_PREVIEW.fullmatch(path):
            space_id, operation = preview.groups()
            if method == 'GET' and operation is None:
                result = self.team.previews.status(actor, space_id)
            elif method == 'GET' and operation == '/open':
                location = self.team.previews.open_url(actor, space_id, str(current['sessionId']))
                self.send_response(302)
                self.send_header('Location', location)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            elif method == 'POST' and operation is None:
                if set(body) != {'clientRequestId'}:
                    raise TeamError(400, 'invalid_request', 'Provide only clientRequestId')
                result = self.team.previews.start(actor, space_id, body['clientRequestId'])
                status = 202
            elif method == 'POST' and operation == '/stop':
                if body:
                    raise TeamError(400, 'invalid_request', 'Preview stop does not accept parameters')
                result = self.team.previews.stop(actor, space_id)
            else:
                raise TeamError(405, 'method_not_allowed', 'Unsupported preview operation')
        elif drafts := _PROJECT_DRAFTS.fullmatch(path):
            space_id, draft_id, integrate = drafts.groups()
            self.team.identity.require_space(actor, space_id)
            def public_draft(draft):
                return {**draft, 'title': draft['description'], 'creatorUserId': draft['ownerUserId'], 'manifestHash': draft['manifestSha256']}
            if method == 'GET' and not draft_id:
                result = {'items': [public_draft(item) for item in self.team.workspaces.list_drafts(space_id, actor)]}
            elif method == 'POST' and not draft_id:
                session_id = str(body.get('sessionId') or '')
                self.team.grants.require_session(actor, space_id, session_id, action='write')
                result = {'draft': public_draft(self.team.workspaces.share_draft(session_id, actor, body.get('title', 'Shared draft')))}
                status = 201
            elif method == 'POST' and draft_id and integrate == '/integrate':
                self.team.workspaces.read_draft(space_id, draft_id, actor)
                result = {'integration': self.team.workspaces.integrate_draft(draft_id, actor)}
                if self.team.previews.configured and result['integration'].get('status') == 'integrated':
                    try:
                        result['preview'] = self.team.previews.start(actor, space_id, 'integration-' + draft_id)
                    except TeamError as exc:
                        # A successful Git integration remains successful when
                        # preview startup is busy, stale, or unavailable.
                        result['previewError'] = {'code': exc.code, 'message': exc.message}
            elif method == 'POST' and draft_id and integrate == '/adopt':
                result = {'adoption': self.team.workspaces.adopt_draft(space_id, draft_id, actor, str(body.get('sessionId') or ''))}
            elif method == 'GET' and draft_id and integrate == '/diff':
                from .delivery import draft_diff
                result = draft_diff(self.team, space_id, draft_id, actor)
            elif method == 'GET' and draft_id and not integrate:
                result = {'draft': public_draft(self.team.workspaces.read_draft(space_id, draft_id, actor))}
            else:
                raise TeamError(404, 'route_not_found', 'Draft route not found')
        elif members := _PROJECT_MEMBERS.fullmatch(path):
            space_id, target_user = members.groups()
            if method == 'GET' and not target_user:
                result = {'items': self.team.identity.project_members(actor, space_id)}
            elif method == 'POST' and not target_user:
                result = {'member': self.team.identity.add_project_member(actor, space_id, body.get('userId'), role=body.get('role', 'contributor'))}
                status = 201
            elif method == 'DELETE' and target_user:
                self.team.identity.remove_project_member(actor, space_id, target_user)
                self.team.revoke_user(target_user, space_id=space_id)
                result = {}
            else:
                raise TeamError(405, 'method_not_allowed', 'Unsupported project member operation')
        else:
            raise TeamError(404, 'route_not_found', 'Team route not found')
        self._team_json(status, {'ok': True, **result})

    def _authorize_gateway_request(self, method: str, parsed: Any) -> bool:
        # The parent HTTP dispatch is entered only after this request's team
        # identity, space and canonical route have been authorized above.
        return self._team_request is not None

    def _request_access_context(self):
        if self._team_request is None:
            raise TeamError(401, 'login_required', 'Sign in to PAW Team')
        return self._team_request.context

    def _management_post_security_error(self, path: str, *, require_json: bool = True):
        # Cookie, Origin and CSRF were checked before invoking legacy handlers.
        return None if self._team_request is not None else {'ok': False, 'error': 'Team authorization required'}

    def _read_knowledge_upload(self, length: int) -> bytes:
        from .knowledge import TEAM_KNOWLEDGE_MAX_SOURCE_BYTES
        if self._team_request is None or self._team_request.request.path_id != 'knowledgeBases.document.import':
            raise TeamError(403, 'route_not_allowed', 'Only an authorized Knowledge import accepts file bytes')
        if not 0 < length <= TEAM_KNOWLEDGE_MAX_SOURCE_BYTES:
            raise TeamError(413, 'knowledge_source_too_large', '单个团队资料不能超过 8 MiB。')
        deadline = time.monotonic() + _KNOWLEDGE_UPLOAD_SECONDS
        previous_timeout = self.request.gettimeout()
        data = bytearray()
        try:
            while len(data) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                self.request.settimeout(remaining)
                chunk = self.rfile.read1(min(64 * 1024, length - len(data)))
                if not chunk:
                    raise TeamError(400, 'knowledge_upload_incomplete', '文件上传未完成，请重试。')
                data.extend(chunk)
        except TimeoutError:
            self.close_connection = True
            raise TeamError(408, 'knowledge_upload_timeout', '文件上传超时，请重试。') from None
        finally:
            self.request.settimeout(previous_timeout)
        self._check_current()
        # Recheck write eligibility after the potentially slow body transfer,
        # before intake can persist a scan receipt or a project document.
        self.team.identity.require_space(self._team_request.actor_user_id, str(self._team_request.space['id']), action='write')
        return bytes(data)

    def _serve_isolated_html_preview(self, path: str) -> bool:
        return False

    def _check_current(self) -> None:
        current = self._authenticate()
        request = self._team_request
        if request is not None:
            self.team.identity.require_space(str(current['user']['id']), str(request.space['id']))

    def _write_json(self, status, payload, *, headers=None) -> None:
        self._check_current()
        request = self._team_request
        if request is not None:
            projector = getattr(self.service, 'project_team_response', None)
            if callable(projector):
                payload = projector(request, deepcopy(payload))
        super()._write_json(status, payload, headers=headers)

    def _write_binary(self, status, payload, *, mime_type, etag) -> None:
        self._check_current()
        super()._write_binary(status, payload, mime_type=mime_type, etag=etag)

    def _write_knowledge_binary(self, blob) -> None:
        # Keep knowledge HTML/SVG under the same sandboxed binary policy.
        self._write_binary(HTTPStatus.OK, blob.data, mime_type=blob.media_type, etag=blob.asset_id)

    def _stream_sse(self, stream_factory, *, cache_control: str, nosniff: bool = False) -> None:
        def authorized_stream():
            source = iter(stream_factory())
            try:
                for chunk in source:
                    try:
                        self._check_current()
                    except TeamError:
                        return
                    projector = getattr(self.service, 'project_team_event', None)
                    if callable(projector):
                        chunk = projector(self._team_request, chunk)
                    if chunk:
                        yield chunk
            finally:
                close = getattr(source, 'close', None)
                if callable(close):
                    close()
        super()._stream_sse(authorized_stream, cache_control='no-store', nosniff=True)

    def _team_error(self, error: TeamError) -> None:
        self._team_json(error.status, {'ok': False, 'error': error.message, 'errorCode': error.code})

    def _team_json(self, status: int, payload: dict[str, object], *, cookie: str | None = None) -> None:
        headers = {'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'}
        if cookie is not None:
            value = f'{self._cookie_name()}={cookie}; Path=/; HttpOnly; SameSite=Lax'
            value += '; Secure' if self._origin().startswith('https:') else ''
            value += '; Max-Age=0' if not cookie else '; Max-Age=604800'
            headers['Set-Cookie'] = value
        super()._write_json(HTTPStatus(status), payload, headers=headers)

    def _static(self, path: str) -> None:
        decoded = unquote(path)
        parts = decoded.strip('/').split('/') if decoded.strip('/') else ['index.html']
        if any(part in {'.', '..'} or part.startswith('.') for part in parts):
            raise TeamError(404, 'route_not_found', 'Page not found')
        candidate = (self.team.static_dir / '/'.join(parts)).resolve()
        if not candidate.is_relative_to(self.team.static_dir):
            raise TeamError(404, 'route_not_found', 'Page not found')
        if not candidate.is_file():
            if Path(decoded).suffix:
                raise TeamError(404, 'route_not_found', 'Page not found')
            candidate = self.team.static_dir / 'index.html'
        if not candidate.is_file():
            raise TeamError(503, 'frontend_not_built', 'Build the PAWOS frontend before starting the team server')
        body = candidate.read_bytes()
        if candidate.name == 'index.html':
            marker = b'<meta name="paw-deployment" content="team">'
            body = body.replace(b'<head>', b'<head>' + marker, 1)
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)


class TeamHTTPServer(QuietThreadingHTTPServer):
    max_connections = 128

    def __init__(self, *args, **kwargs):
        self._connection_slots = BoundedSemaphore(self.max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


def make_team_server(app: TeamApplication, *, host: str = '127.0.0.1', port: int = 8770):
    if host not in {'127.0.0.1', '::1', 'localhost'} and not app.public_origin:
        raise ValueError('A public origin is required for a non-loopback team listener')
    if host not in {'127.0.0.1', '::1', 'localhost'} and not app.public_origin.startswith('https://'):
        raise ValueError('Remote team access requires a public HTTPS origin')

    class Handler(TeamRequestHandler):
        team = app
        static_dir = app.static_dir

    return TeamHTTPServer((host, port), Handler)
