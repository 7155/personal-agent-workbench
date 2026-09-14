"""Team deployment entry; personal desktop launch paths remain unchanged."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import stat
import sys
from threading import Event, Thread

from ..db import sqlite_connection
from ..secure_files import regular_reader
from .broker import TeamModelConfig
from .coordinator import TeamExecutionCoordinator
from .errors import TeamError
from .grants import TeamGrantStore
from .gateway import TeamApplication, make_team_server
from .identity import TeamIdentityStore
from .workspaces import TeamWorkspaceService, _branch


_MAX_OWNER_USERNAME_LENGTH = 64


def _owner_user_id(identity: TeamIdentityStore, username: object) -> str:
    if not isinstance(username, str) or "\x00" in username:
        raise TeamError(400, "invalid_input", "owner username is invalid")
    normalized = username.strip()
    if not normalized or len(normalized) > _MAX_OWNER_USERNAME_LENGTH:
        raise TeamError(400, "invalid_input", "owner username has an invalid size")
    identity.initialize()
    with sqlite_connection(
        identity.db_path,
        row_factory=sqlite3.Row,
        foreign_keys=True,
    ) as conn:
        row = conn.execute(
            "SELECT id, active FROM team_users WHERE username_key = ?",
            (normalized.casefold(),),
        ).fetchone()
    if row is None:
        raise TeamError(404, "owner_not_found", "active owner username was not found")
    if not bool(row["active"]):
        raise TeamError(409, "owner_not_active", "owner account is not active")
    return str(row["id"])


def _source_branch_commit(
    workspace_service: TeamWorkspaceService,
    source: Path,
    branch: str,
) -> str:
    result = workspace_service._git_run(  # noqa: SLF001 - trusted CLI preflight
        ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
        source,
    )
    if result.returncode != 0:
        raise TeamError(
            400,
            "source_branch_not_found",
            f"source repository branch '{branch}' was not found",
        )
    output = result.stdout
    commit = output.decode("utf-8", errors="strict").strip() if isinstance(output, bytes) else str(output).strip()
    if not commit:
        raise TeamError(400, "source_branch_invalid", "source repository branch has no commit")
    return commit


def provision_project(
    data_root: Path,
    *,
    owner_username: object,
    name: object,
    source_repo: object,
    branch: object = "main",
) -> dict[str, object]:
    """Provision one project from an explicitly selected local Git branch.

    This is a trusted-host operation.  It is intentionally outside the HTTP
    application and does not represent a remote administrator capability.
    All source checks happen before ``create_project`` so invalid operator
    input cannot leave an empty project behind.
    """

    identity = TeamIdentityStore(data_root / "team.sqlite")
    owner_id = _owner_user_id(identity, owner_username)
    grants = TeamGrantStore(identity.db_path, identity)
    workspace_service = TeamWorkspaceService(data_root / "projects", identity, grants)
    normalized_branch = _branch(branch)
    source = workspace_service._trusted_source(source_repo)  # noqa: SLF001 - trusted CLI preflight
    _source_branch_commit(workspace_service, source, normalized_branch)

    project = identity.create_project(owner_id, name)
    project_id = str(project["id"])
    try:
        imported = workspace_service.ensure_project(
            project_id,
            source,
            target_branch=normalized_branch,
        )
    except TeamError as exc:
        raise TeamError(
            exc.status,
            exc.code,
            f"{exc.message}; empty project {project_id} was retained for recovery",
        ) from None
    except Exception as exc:
        raise TeamError(
            500,
            "project_import_failed",
            f"source import failed; empty project {project_id} was retained for recovery",
        ) from exc
    return {
        "ok": True,
        "projectId": project_id,
        "branch": str(imported["targetBranch"]),
        "headCommit": str(imported["headCommit"]),
        "revision": int(imported["revision"]),
    }


def load_execution(path: Path) -> TeamExecutionCoordinator:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError('Runtime configuration must be an owner-only regular file (chmod 600)')
    if path.stat().st_size > 64 * 1024:
        raise ValueError('Runtime configuration is too large')
    data = json.loads(path.read_text())
    allowed = {
        'image', 'command', 'model', 'validationCommand', 'dockerExecutable',
        'workerUser', 'memoryLimit', 'cpuLimit', 'brokerSocketRoot',
    }
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError('Invalid team runtime configuration')
    broker_socket_root = data.get('brokerSocketRoot')
    if 'brokerSocketRoot' in data and broker_socket_root is None:
        raise ValueError('brokerSocketRoot must be an existing absolute directory')
    command = data.get('command', ['python3', '-m', 'rag_ime.team.worker_proxy', '--', 'node', '/opt/pi/runtime-host/cli.mjs'])
    validation = data.get('validationCommand', [])
    if not isinstance(command, list) or not isinstance(validation, list) or not all(isinstance(x, str) for x in [*command, *validation]):
        raise ValueError('Worker and validation commands must be argument arrays')
    return TeamExecutionCoordinator(
        image=data['image'], command=tuple(command), model=TeamModelConfig(**data['model']),
        validation_command=tuple(validation), docker_executable=data.get('dockerExecutable', 'docker'),
        worker_user=data.get('workerUser', ''), memory_limit=data.get('memoryLimit', '2g'), cpu_limit=data.get('cpuLimit', '2.0'),
        broker_socket_root=broker_socket_root,
    )


def load_preview(path: Path, data_root: Path):
    """Read operator-only runtime/origin configuration, never project input."""
    from .preview_runtime import PreviewRuntime
    from .previews import PreviewOrigin
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError('Preview configuration must be an owner-only regular file (chmod 600)')
    if path.stat().st_size > 64 * 1024:
        raise ValueError('Preview configuration is too large')
    data = json.loads(path.read_text())
    allowed = {'image', 'originTemplate', 'host', 'port', 'dockerExecutable', 'workerUser', 'memoryLimit', 'cpuLimit'}
    if not isinstance(data, dict) or set(data) - allowed or not {'image', 'originTemplate'} <= set(data):
        raise ValueError('Invalid team preview configuration')
    origin = PreviewOrigin(data['originTemplate'])
    host, port = data.get('host', '127.0.0.1'), data.get('port', 8771)
    if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError('Preview listener requires a host and port from 1 to 65535')
    if host not in {'127.0.0.1', 'localhost', '::1'} and not origin.secure:
        raise ValueError('Remote preview access requires a dedicated HTTPS origin')
    runtime = PreviewRuntime(
        data_root / 'project-preview-runtime', image=data['image'],
        docker_executable=data.get('dockerExecutable', 'docker'), worker_user=data.get('workerUser', ''),
        memory_limit=data.get('memoryLimit', '2g'), cpu_limit=data.get('cpuLimit', '2.0'),
    )
    return runtime, data['originTemplate'], {'host': host, 'port': port}


def load_connections(path: Path) -> dict[str, object]:
    with regular_reader(path) as handle:
        if os.fstat(handle.fileno()).st_mode & 0o077:
            raise ValueError('Connections configuration must be owner-only (chmod 600)')
        raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError('Connections configuration is too large')
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) - {'githubClientId', 'githubClientSecret', 'vaultKeyFile'}:
        raise ValueError('Connections configuration must be an object with supported fields')
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='PAW team server: personal spaces, shared projects and isolated Pi workers')
    commands = parser.add_subparsers(dest='operation', required=True)
    bootstrap = commands.add_parser('bootstrap-admin', help='Create the first administrator in a new team data directory')
    bootstrap.add_argument('--data-root', type=Path, required=True)
    bootstrap.add_argument('--username', required=True)
    bootstrap.add_argument('--display-name', default='')
    bootstrap.add_argument('--password-stdin', action='store_true')
    provision = commands.add_parser(
        'provision-project',
        help='Import a committed branch from a trusted local Git repository',
    )
    provision.add_argument('--data-root', type=Path, required=True)
    provision.add_argument('--owner', required=True, help='Existing active team username')
    provision.add_argument('--name', required=True, help='New project name')
    provision.add_argument('--source-repo', type=Path, required=True)
    provision.add_argument('--branch', default='main')
    serve = commands.add_parser('serve', help='Serve the team PAWOS frontend and authenticated APIs')
    serve.add_argument('--data-root', type=Path, required=True)
    serve.add_argument('--frontend', type=Path, default=Path('control-center-web/dist'))
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8770)
    serve.add_argument('--public-origin', default='')
    serve.add_argument('--runtime-config', type=Path)
    serve.add_argument('--preview-config', type=Path, help='Operator configuration for isolated HTTP previews on a dedicated origin')
    serve.add_argument('--connections-config', type=Path, help='Operator configuration for private and project external accounts')
    serve.add_argument('--package-source-root', type=Path, help='Trusted root for an existing PAW Package catalog; requires --package-catalog')
    serve.add_argument('--package-catalog', type=Path, help='Existing rag-ime.plugin-catalog.v1 file; requires --package-source-root')
    args = parser.parse_args(argv)
    try:
        root = args.data_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.stat().st_mode & 0o077:
            raise ValueError('Team data directory must be private to the service user (chmod 700)')
        if args.operation == 'bootstrap-admin':
            password = sys.stdin.readline(1025).rstrip('\r\n') if args.password_stdin else getpass.getpass('Initial administrator password: ')
            identity = TeamIdentityStore(root / 'team.sqlite')
            user = identity.bootstrap_admin(args.username, password, display_name=args.display_name)
            print(json.dumps({'ok': True, 'user': user}, ensure_ascii=False))
            return 0
        if args.operation == 'provision-project':
            result = provision_project(
                root,
                owner_username=args.owner,
                name=args.name,
                source_repo=args.source_repo,
                branch=args.branch,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        execution = load_execution(args.runtime_config.expanduser().absolute()) if args.runtime_config else None
        preview_runtime, preview_origin, preview_listener = (
            load_preview(args.preview_config.expanduser().absolute(), root) if args.preview_config else (None, '', None)
        )
        connection_config = None
        if args.connections_config:
            connection_config = load_connections(args.connections_config.expanduser().absolute())
        app = TeamApplication(root, args.frontend, public_origin=args.public_origin, execution=execution,
                              preview_runtime=preview_runtime, preview_origin=preview_origin,
                              connections_config=connection_config,
                              package_source_root=args.package_source_root.expanduser().absolute() if args.package_source_root else None,
                              package_catalog=args.package_catalog.expanduser().absolute() if args.package_catalog else None)
        servers = []
        try:
            server = make_team_server(app, host=args.host, port=args.port)
            servers.append(server)
            if preview_listener is not None:
                from .preview_gateway import make_preview_server
                servers.append(make_preview_server(app.previews, **preview_listener))
        except BaseException:
            for listener in servers:
                listener.server_close()
            app.close()
            raise
        stopped = Event()
        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, lambda *_: stopped.set())
        threads = [Thread(target=listener.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True) for listener in servers]
        for thread in threads:
            thread.start()
        print(f'PAW Team is listening on {args.host}:{server.server_port}; runtime={"isolated" if execution else "not configured"}', flush=True)
        if preview_listener is not None:
            print(f'Project previews use {preview_origin}; internal listener {preview_listener["host"]}:{preview_listener["port"]}', flush=True)
        try:
            while not stopped.wait(1):
                if any(not thread.is_alive() for thread in threads):
                    raise RuntimeError('Team HTTP listener stopped unexpectedly')
        finally:
            for listener in servers:
                listener.shutdown()
                listener.server_close()
            for thread in threads:
                thread.join(timeout=5)
            app.close()
        return 0
    except TeamError as exc:
        print(f'{args.operation} failed [{exc.code}]: {exc.message}', file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
