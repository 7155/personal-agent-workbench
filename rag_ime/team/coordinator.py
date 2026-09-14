"""Bind team identities, the existing Pi factory and trusted OCI launchers."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import selectors
import secrets
import shutil
import stat
import tempfile
from threading import Event, RLock, Thread
import time
from typing import Any
import uuid

from .broker import AttemptBroker, TeamModelBroker, TeamModelConfig
from .errors import TeamError
from .execution import DockerExecutionLauncher, ExecutionSpec, TeamExecutionError
from .runtime import TeamRuntimeBinding
from .shared_resources import package_skill_refs
from .grants import current_execution_authority, execution_session_key
from ..secure_files import atomic_write, regular_reader

_LOGGER = logging.getLogger(__name__)

_MAX_UNIX_SOCKET_PATH = 100
_BROKER_ROOT_PREFIX = 'paw-brokers-'
_TEMPFILE_SUFFIX_LENGTH = 8
_BROKER_SOCKET_NAME_LENGTH = 32 + len('.sock')


def _validate_broker_socket_root(value: str | os.PathLike[str]) -> Path:
    """Validate an operator-owned, daemon-visible broker parent directory."""

    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise ValueError('Broker socket root must be a path string') from exc
    if not isinstance(raw, str) or not raw or raw != raw.strip() or any(ord(char) < 32 for char in raw):
        raise ValueError('Broker socket root must be a non-empty path')
    try:
        path = Path(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('Broker socket root is invalid') from exc
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Broker socket root must be an absolute path without traversal')
    if path == Path('/'):
        raise ValueError('Broker socket root must not be the filesystem root')

    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError('Broker socket root must already exist') from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('Broker socket root cannot contain symlink components')
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError('Broker socket root parent must be a directory')

    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError('Broker socket root must already exist') from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('Broker socket root must be an existing directory')
    if info.st_uid != os.getuid():
        raise ValueError('Broker socket root must be owned by the team service')
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError('Broker socket root must not be group- or world-writable')
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError('Broker socket root must be writable by the team service')

    candidate = path / (
        _BROKER_ROOT_PREFIX + ('x' * _TEMPFILE_SUFFIX_LENGTH)
    ) / (('x' * (_BROKER_SOCKET_NAME_LENGTH - len('.sock'))) + '.sock')
    if len(os.fsencode(str(candidate))) > _MAX_UNIX_SOCKET_PATH:
        raise ValueError('Broker socket root is too deep for its Unix socket path')
    return path


def _remove_private_socket_root(path: Path) -> None:
    """Remove one coordinator child without following a swapped symlink."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        path.unlink()
    elif stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


@dataclass
class _Operation:
    spec: ExecutionSpec
    process: Any
    launcher: Any
    owner: str
    space_id: str
    membership_revision: int
    is_validation: bool = False
    authority: Any = None
    cancelled: Event = field(default_factory=Event)
    removed: bool = False


class _GrantLauncher:
    def __init__(self, owner: 'TeamExecutionCoordinator') -> None:
        self.owner = owner

    def start(self, spec, command, environment):
        owner = self.owner
        with owner._lock:
            binding = owner._bindings.get(spec.session_id)
            if spec.session_id in owner._frozen or binding is None or binding.spec.attempt_id != spec.attempt_id:
                raise TeamError(409, 'execution_paused', 'The task workspace is paused for version publication')
            grant = owner.team.grants.resolve_token(binding.gateway_token, spec.session_id, owner._spaces[spec.session_id])
            owner._check_limits(str(grant['ownerUserId']))
            atomic_write(spec.scope_root / 'active-container.json', json.dumps({
                'containerId': spec.container_id, 'attemptId': spec.attempt_id, 'sessionId': spec.session_id,
            }).encode())
            try:
                process = owner.launcher.start(spec, command, environment)
            except BaseException:
                # A launcher error does not prove that the container was
                # never created.  Keep the registry marker and block further
                # execution until the reaper verifies Docker absence.
                owner._recovery_pending.add(spec.scope_root)
                raise
            owner._processes[spec.session_id] = (spec, process, str(grant['ownerUserId']))
            return process

    def stop(self, spec, process):
        receipt = self.owner.launcher.stop(spec, process)
        if not receipt.verified:
            raise TeamExecutionError('Container termination could not be verified')
        atomic_write(spec.scope_root / ('stop-' + spec.attempt_id + '.json'), json.dumps(receipt.as_dict()).encode())
        (spec.scope_root / 'active-container.json').unlink(missing_ok=True)
        with self.owner._lock:
            entry = self.owner._processes.get(spec.session_id)
            if entry is not None and entry[0].attempt_id == spec.attempt_id:
                self.owner._processes.pop(spec.session_id, None)
        return receipt


class TeamExecutionCoordinator:
    def __init__(
        self, *, image: str, command: tuple[str, ...], model: TeamModelConfig,
        validation_command: tuple[str, ...] = (), docker_executable: str = 'docker',
        worker_user: str = '', memory_limit: str = '2g', cpu_limit: str = '2.0',
        broker_socket_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.image, self.command, self.model_config = image, command, model
        self.validation_command = validation_command
        self.model_id = model.model
        self.docker_executable = docker_executable
        self.worker_user = worker_user or f'{os.getuid()}:{os.getgid()}'
        if self.worker_user.startswith('0:'):
            raise ValueError('Run the team server as a dedicated non-root service user')
        self.memory_limit, self.cpu_limit = memory_limit, cpu_limit
        self.launcher = DockerExecutionLauncher(image=image, command=command, docker_executable=docker_executable)
        self._grant_launcher = _GrantLauncher(self)
        self._lock = RLock()
        self._bindings: dict[str, TeamRuntimeBinding] = {}
        self._spaces: dict[str, str] = {}
        self._brokers: dict[str, AttemptBroker] = {}
        self._processes: dict[str, tuple[Any, Any, str]] = {}
        self._operations: dict[str, _Operation] = {}
        self._frozen: dict[str, int] = {}
        self._recovery_pending: set[Path] = set()
        self._broker_socket_root_config = (
            _validate_broker_socket_root(broker_socket_root)
            if broker_socket_root is not None
            else None
        )
        self._socket_root: Path | None = None
        self.team: Any = None
        self._closed = Event()
        self._reaper: Thread | None = None

    def bind_application(self, team: Any) -> None:
        if self.team is not None:
            raise ValueError('Execution coordinator already belongs to a team application')
        self.team = team
        self.model_broker = TeamModelBroker(team, self.model_config)
        # Unix socket names have short platform limits. The private parent is
        # never mounted; only each attempt's socket enters its own worker.
        if self._broker_socket_root_config is None:
            # Keep the existing /tmp behavior for local macOS and Linux
            # deployments; /tmp may be a system alias on macOS.
            parent = '/tmp'
        else:
            parent = _validate_broker_socket_root(self._broker_socket_root_config)
        try:
            socket_root = Path(tempfile.mkdtemp(prefix=_BROKER_ROOT_PREFIX, dir=parent))
            socket_root.chmod(0o700)
            info = socket_root.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError('Broker socket root child must be a private directory')
            if stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Broker socket root child must be mode 700')
            candidate = socket_root / (('x' * 32) + '.sock')
            if len(os.fsencode(str(candidate))) > _MAX_UNIX_SOCKET_PATH:
                raise ValueError('Broker socket root is too deep for its Unix socket path')
            self._socket_root = socket_root
        except (OSError, ValueError) as exc:
            if 'socket_root' in locals():
                _remove_private_socket_root(socket_root)
            raise ValueError('Broker socket root is not usable') from exc
        self._scan_orphans()
        self._reaper = Thread(target=self._reap_revoked, daemon=True, name='paw-team-revocation')
        self._reaper.start()

    def _session_root(self, session_id: str) -> Path:
        return self.team.data_root / 'execution' / execution_session_key(session_id)

    def _new_broker_socket_path(self) -> Path:
        if self._socket_root is None:
            raise TeamError(503, 'execution_unavailable', 'The execution coordinator is not bound to a team')
        path = self._socket_root / (uuid.uuid4().hex + '.sock')
        if len(os.fsencode(str(path))) > _MAX_UNIX_SOCKET_PATH:
            raise ValueError('Broker socket path is too long')
        return path

    def _check_limits(self, actor: str) -> None:
        if self._closed.is_set() or self._recovery_pending:
            raise TeamError(503, 'worker_cleanup_pending', 'Execution is waiting for earlier containers to be removed')
        running = [item[2] for item in self._processes.values() if item[1].poll() is None]
        running.extend(item.owner for item in self._operations.values() if not item.removed)
        if len(running) >= 32 or running.count(actor) >= 8:
            raise TeamError(429, 'worker_concurrency_limit', 'The team or member execution limit has been reached')

    def _scan_orphans(self) -> None:
        """Recover only server-owned registry roots, never a mounted worker tree."""
        root = self.team.data_root / 'execution'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidates = [item for item in root.iterdir() if item.name != 'operations']
        operations = root / 'operations'
        if operations.is_dir() and not operations.is_symlink():
            candidates.extend(operations.iterdir())
        for candidate in candidates:
            if candidate.is_dir() and not candidate.is_symlink() and (candidate / 'active-container.json').exists():
                try:
                    self._recover_container(candidate)
                except Exception:
                    self._recovery_pending.add(candidate)
                    _LOGGER.exception('Earlier team container requires recovery before execution')

    def _reap_revoked(self) -> None:
        while not self._closed.wait(5):
            with self._lock:
                ids = list(self._bindings)
                for root in list(self._recovery_pending):
                    try:
                        self._recover_container(root)
                        self._recovery_pending.discard(root)
                    except Exception:
                        _LOGGER.warning('Team container recovery is still pending')
                for operation in list(self._operations.values()):
                    try:
                        self._check_operation(operation)
                    except TeamError:
                        try:
                            self._stop_operation(operation, cancel=True)
                        except Exception:
                            _LOGGER.exception('Revoked workspace worker still requires cleanup')
            for session_id in ids:
                try:
                    with self._lock:
                        binding = self._bindings.get(session_id)
                        if binding is None:
                            continue
                        self.team.grants.resolve_token(binding.gateway_token, session_id, self._spaces[session_id])
                except TeamError:
                    try:
                        self.revoke_session(session_id)
                    except Exception:
                        _LOGGER.exception('Revoked team worker still requires cleanup')

    def _recover_container(self, root: Path) -> None:
        active = root / 'active-container.json'
        if not active.exists():
            return
        with regular_reader(active) as handle:
            data = json.loads(handle.read(8192))
        container_id = str(data.get('containerId') or '')
        if not container_id.startswith('paw-team-') or len(container_id) != 41 or any(c not in '0123456789abcdef' for c in container_id[9:]):
            raise TeamExecutionError('Persisted team container identity is invalid')
        self.team.grants.retire_attempt(str(data['attemptId']))
        removed = self.launcher._run_docker([self.docker_executable, 'rm', '-f', container_id], allow_failure=True)
        inspected = self.launcher._run_docker([self.docker_executable, 'inspect', container_id], allow_failure=True)
        if not self.launcher._is_missing_container(inspected):
            raise TeamExecutionError('An earlier task container must be stopped before this task resumes')
        atomic_write(root / ('recovered-' + str(data['attemptId']) + '.json'), json.dumps({
            'containerId': container_id, 'verified': True, 'state': 'absent', 'removalExitCode': removed.returncode,
        }).encode())
        active.unlink()

    def binding_for(self, space_id: str, session: Mapping[str, object]) -> TeamRuntimeBinding:
        session_id = str(session['id'])
        with self._lock:
            ownership = self.team.grants.binding(session_id)
            if ownership['spaceId'] != space_id:
                raise TeamError(403, 'execution_not_authorized', 'Execution space does not match the task')
            if session_id in self._frozen:
                raise TeamError(409, 'execution_paused', 'This task is publishing a fixed workspace version')
            current = self._bindings.get(session_id)
            process = self._processes.get(session_id)
            if current is not None and process is not None and process[1].poll() is not None:
                self._stop_locked(session_id)
                current = None
            if current is not None:
                try:
                    self.team.grants.resolve_token(current.gateway_token, session_id, space_id)
                    return current
                except TeamError:
                    self._stop_locked(session_id)
            self._check_limits(str(ownership['ownerUserId']))
            packages = self.team.shared_resources.execution_packages(session_id)
            skill_refs = package_skill_refs(packages)
            root = self._session_root(session_id)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._recover_container(root)
            broker_path = self._new_broker_socket_path()
            attempt = self.team.grants.issue_attempt(session_id)
            attempt_root = root / 'attempts' / str(attempt['attemptId'])
            broker = AttemptBroker(self.team, socket_path=broker_path, session_id=session_id, space_id=space_id, model=self.model_broker)
            gateway_url = 'http://127.0.0.1:8766/api/agent/tool/execute'
            spec = ExecutionSpec(
                attempt_id=str(attempt['attemptId']), session_id=session_id, scope_root=root,
                workspace_root=Path(str(ownership['workspacePath'])),
                agent_dir=attempt_root / 'agent', session_dir=root / 'sessions', logs_dir=attempt_root / 'logs',
                home_dir=attempt_root / 'home', tmp_dir=attempt_root / 'tmp',
                container_id='paw-team-' + uuid.uuid4().hex, runtime_image=self.image,
                runtime_command=self.command, broker_socket=broker_path, gateway_url=gateway_url,
                container_user=self.worker_user, cpu_limit=self.cpu_limit, memory_limit=self.memory_limit,
                workspace_read_only=session.get('executionMode') == 'read_only',
                resource_roots=tuple(package.path for package in packages),
            )
            provider = {
                'api': self.model_config.wire_api, 'baseUrl': 'http://127.0.0.1:8766/v1',
                'apiKey': str(attempt['token']),
                'compat': {'supportsStore': False, 'supportsToolSearch': False},
                'models': [{'id': self.model_id, 'name': self.model_id, 'reasoning': False,
                            'input': ['text'], 'contextWindow': 128000, 'maxTokens': self.model_config.max_output_tokens,
                            'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}}],
            }
            binding = TeamRuntimeBinding(
                spec=spec, gateway_url=gateway_url, gateway_token=str(attempt['token']),
                grant_id=str(attempt['attemptId']), generation=int(attempt['generation']),
                provider='team', model=self.model_id, model_providers={'team': provider}, launcher=self._grant_launcher,
                packages=packages, package_activation_token=secrets.token_urlsafe(32) if packages else '',
                skill_refs=skill_refs,
            )
            self._bindings[session_id] = binding
            self._spaces[session_id] = space_id
            self._brokers[session_id] = broker
            return binding

    def _stop_locked(self, session_id: str) -> None:
        binding = self._bindings.get(session_id)
        if binding is not None:
            self.team.grants.retire_attempt(binding.spec.attempt_id)
        for operation in list(self._operations.values()):
            if operation.spec.session_id == session_id:
                self._stop_operation(operation, cancel=True)
        entry = self._processes.get(session_id)
        if entry is not None:
            self._grant_launcher.stop(entry[0], entry[1])
        else:
            self._recover_container(self._session_root(session_id))
        self._bindings.pop(session_id, None)
        broker = self._brokers.pop(session_id, None)
        if broker is not None:
            broker.close()

    @contextmanager
    def quiesce(self, session_id: str) -> Iterator[None]:
        with self._lock:
            self._frozen[session_id] = self._frozen.get(session_id, 0) + 1
        try:
            with self._lock:
                self._stop_locked(session_id)
            yield
        finally:
            with self._lock:
                remaining = self._frozen[session_id] - 1
                if remaining:
                    self._frozen[session_id] = remaining
                else:
                    self._frozen.pop(session_id, None)

    def revoke_session(self, session_id: str) -> None:
        with self.quiesce(session_id):
            pass

    def revoke_user(self, user_id: str, *, space_id: str | None = None) -> None:
        with self._lock:
            for operation in list(self._operations.values()):
                if operation.owner == user_id and (space_id is None or operation.space_id == space_id):
                    self._stop_operation(operation, cancel=True)

    def workspace_harness(self, space_id: str):
        from .workspace_remote import TeamRemoteWorkspaceHarness
        return TeamRemoteWorkspaceHarness(self, space_id)

    def verify_candidate(self, path: Path, context: Mapping[str, object]) -> dict[str, object]:
        if not self.validation_command:
            raise TeamError(503, 'validation_not_configured', 'Configure the project validation command before integration')
        raw = self.run_isolated(path, self.validation_command, payload=None, timeout=300, authorization=context)
        return {'ok': raw['exitCode'] == 0, 'exitCode': raw['exitCode'], 'output': raw['stdout'],
                'stderr': raw['stderr'], 'containerRemoved': True}

    def _new_operation_launcher(self, command: tuple[str, ...]):
        return DockerExecutionLauncher(image=self.image, command=command, docker_executable=self.docker_executable)

    @staticmethod
    def _session_workspace_read_only(payload: Mapping[str, object], session_id: str) -> bool:
        """Return the mount policy projected from the server-owned Session.

        WorkspaceRemote serializes this small Session projection immediately
        before dispatch.  The one-shot request may contain other convenience
        flags (including ``workspaceReadOnly``), but those fields are never an
        authority for the OCI mount mode.  Requiring the projection also makes
        an unbound/direct caller fail closed instead of silently receiving a
        writable host checkout.
        """
        session = payload.get('session')
        if not isinstance(session, Mapping) or str(session.get('id') or '') != session_id:
            raise TeamError(403, 'execution_not_authorized', 'Session execution policy is unavailable')
        mode = str(session.get('executionMode') or '').strip()
        if mode == 'read_only':
            return True
        if mode == 'workspace_managed':
            return False
        raise TeamError(403, 'execution_not_authorized', 'Session execution policy is unavailable')

    def _check_operation(self, operation: _Operation) -> None:
        if operation.cancelled.is_set():
            raise TeamError(403, 'execution_not_authorized', 'The workspace operation was stopped')
        if operation.authority is not None:
            operation.authority()
        if operation.is_validation:
            scope = self.team.identity.require_space(operation.owner, operation.space_id, action='manage')
            revision = scope['membershipRevision']
        else:
            binding = self.team.grants.binding(operation.spec.session_id)
            if binding['spaceId'] != operation.space_id or binding['ownerUserId'] != operation.owner:
                raise TeamError(403, 'execution_not_authorized', 'Workspace ownership changed')
            revision = binding['membershipRevision']
        if revision != operation.membership_revision:
            raise TeamError(403, 'execution_not_authorized', 'Workspace authorization changed')

    def _stop_operation(self, operation: _Operation, *, cancel: bool = False) -> None:
        # Called under _lock, including concurrent revocation and normal exit.
        if cancel:
            operation.cancelled.set()
        if operation.removed:
            return
        try:
            receipt = operation.launcher.stop(operation.spec, operation.process)
        except Exception:
            operation.cancelled.set()
            raise
        if not receipt.verified:
            raise TeamExecutionError('Could not verify one-shot container removal')
        operation.removed = True
        (operation.spec.scope_root / 'active-container.json').unlink(missing_ok=True)
        self._operations.pop(operation.spec.attempt_id, None)

    def run_isolated(
        self, workspace: Path, command: tuple[str, ...], *, payload: dict[str, object] | None,
        timeout: float = 120, authorization: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """One-shot workers share the same revocation, freeze and quota fence."""
        encoded = json.dumps(payload).encode() + b'\n' if payload is not None else b''
        if len(encoded) > 2 * 1024 * 1024:
            raise TeamError(413, 'workspace_request_too_large', 'Workspace request is too large')
        with self._lock:
            authority = current_execution_authority()
            if authority is not None:
                authority()
            if payload is not None:
                session_id = str(payload.get('sessionId') or '')
                space_id = str(payload.get('spaceId') or '')
                workspace_read_only = self._session_workspace_read_only(payload, session_id)
                binding = self.team.grants.binding(session_id)
                if binding['spaceId'] != space_id or Path(str(binding['workspacePath'])) != workspace.resolve():
                    raise TeamError(403, 'execution_not_authorized', 'Workspace does not match this task')
                if session_id in self._frozen:
                    raise TeamError(409, 'execution_paused', 'This task is publishing a fixed workspace version')
                if any(item.spec.session_id == session_id and not item.removed for item in self._operations.values()):
                    raise TeamError(409, 'workspace_busy', 'Another workspace operation is still running')
                actor, revision = str(binding['ownerUserId']), int(binding['membershipRevision'])
            else:
                context = authorization or {}
                actor, space_id = str(context.get('actorUserId') or ''), str(context.get('spaceId') or '')
                scope = self.team.identity.require_space(actor, space_id, action='manage')
                revision = int(scope['membershipRevision'])
                session_id = 'validation:' + uuid.uuid4().hex
            self._check_limits(actor)
            operation_id = 'oneshot_' + uuid.uuid4().hex
            root = self.team.data_root / 'execution' / 'operations' / operation_id
            root.mkdir(parents=True, exist_ok=False, mode=0o700)
            spec = ExecutionSpec(
                attempt_id=operation_id, session_id=session_id, scope_root=root,
                workspace_root=workspace, agent_dir=root / 'agent', session_dir=root / 'sessions',
                logs_dir=root / 'logs', home_dir=root / 'home', tmp_dir=root / 'tmp',
                container_id='paw-team-' + uuid.uuid4().hex, runtime_image=self.image, runtime_command=command,
                container_user=self.worker_user, cpu_limit=self.cpu_limit, memory_limit=self.memory_limit,
                workspace_read_only=workspace_read_only if payload is not None else True,
            )
            launcher = self._new_operation_launcher(command)
            atomic_write(root / 'active-container.json', json.dumps({
                'containerId': spec.container_id, 'attemptId': operation_id, 'sessionId': session_id,
            }).encode())
            try:
                process = launcher.start(spec, command, spec.container_environment_for())
            except Exception:
                self._recovery_pending.add(root)
                raise
            operation = _Operation(spec, process, launcher, actor, space_id, revision, payload is None, authority)
            self._operations[operation_id] = operation
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        collected = {'stdout': bytearray(), 'stderr': bytearray()}
        try:
            deadline = time.monotonic() + timeout
            offset = 0
            os.set_blocking(process.stdin.fileno(), False)
            if encoded:
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            else:
                process.stdin.close()
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                if time.monotonic() > deadline:
                    raise TeamExecutionError('Isolated worker exceeded its deadline')
                self._check_operation(operation)
                for key, _ in selector.select(timeout=0.1):
                    if key.data == 'stdin':
                        try:
                            offset += os.write(key.fd, encoded[offset:offset + 16384])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            offset = len(encoded)
                        if offset == len(encoded):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                    else:
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        collected[key.data].extend(chunk)
                        if sum(len(value) for value in collected.values()) > 2 * 1024 * 1024:
                            raise TeamExecutionError('Isolated worker output exceeded its limit')
            exit_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            self._check_operation(operation)
            return {'exitCode': exit_code, **{key: value.decode('utf-8', errors='replace') for key, value in collected.items()}}
        finally:
            selector.close()
            try:
                with self._lock:
                    self._stop_operation(operation)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                if operation.removed:
                    shutil.rmtree(root)

    def close(self) -> None:
        self._closed.set()
        if self._reaper is not None:
            self._reaper.join(timeout=6)
        with self._lock:
            for session_id in list(self._bindings):
                self._stop_locked(session_id)
            for operation in list(self._operations.values()):
                self._stop_operation(operation, cancel=True)
        if self._socket_root is not None:
            socket_root = self._socket_root
            if self._broker_socket_root_config is not None and socket_root.parent != self._broker_socket_root_config:
                raise ValueError('Refusing to remove a broker path outside its configured parent')
            _remove_private_socket_root(socket_root)
            self._socket_root = None


__all__ = ['TeamExecutionCoordinator']
