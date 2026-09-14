"""Project-owned preview lifecycle; Pi remains the Session execution owner."""
from __future__ import annotations

import logging
import re
from threading import Event, RLock, Thread
import time
from typing import Any
from urllib.parse import quote, urlsplit

from .errors import TeamError
from .origins import canonical_http_origin
from .preview_store import TeamPreviewStore


_LOGGER = logging.getLogger(__name__)
_DEPLOYMENT = re.compile(r'pv-[0-9a-f]{32}')


class PreviewOrigin:
    """One host-only browser origin per immutable deployment."""

    def __init__(self, template: str) -> None:
        if not isinstance(template, str) or template.count('{deployment}') != 1:
            raise ValueError('Preview origin must contain one {deployment} hostname label')
        parsed = urlsplit(template.replace('{deployment}', 'pv-' + '0' * 32))
        if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment
                or not parsed.hostname or not re.fullmatch(r'[a-z0-9.-]+', parsed.hostname)
                or not template.startswith(parsed.scheme + '://{deployment}.')
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError('Preview origin must be an HTTP(S) origin with {deployment} as its first hostname label')
        suffix = parsed.hostname.partition('.')[2]
        if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in suffix.split('.')):
            raise ValueError('Preview hostname contains an invalid DNS label')
        if parsed.scheme == 'http' and suffix != 'localhost' and not suffix.endswith('.localhost'):
            raise ValueError('Remote preview origins require HTTPS; HTTP is for .localhost development only')
        port = f':{parsed.port}' if parsed.port not in {None, 80 if parsed.scheme == 'http' else 443} else ''
        self.template = parsed.scheme + '://{deployment}.' + suffix + port
        self.secure = parsed.scheme == 'https'
        self.cookie_name = '__Host-paw_preview' if self.secure else 'paw_preview_dev'

    def origin(self, deployment_id: str) -> str:
        if not _DEPLOYMENT.fullmatch(deployment_id):
            raise TeamError(404, 'preview_not_found', 'Preview not found')
        return self.template.replace('{deployment}', deployment_id)

    def deployment_for_host(self, host: str) -> str:
        candidate = host.partition('.')[0]
        origin = self.origin(candidate)
        if urlsplit(origin).netloc != host:
            raise TeamError(404, 'preview_not_found', 'Preview not found')
        return candidate


class TeamPreviewManager:
    def __init__(self, team: Any, *, runtime: Any = None, origin_template: str = '') -> None:
        if bool(runtime) != bool(origin_template):
            raise ValueError('Configure both the isolated preview runtime and its dedicated origin')
        self.team = team
        self.store = TeamPreviewStore(team.identity)
        self.runtime = runtime
        self.origin = PreviewOrigin(origin_template) if origin_template else None
        if self.origin and getattr(team, 'public_origin', ''):
            try:
                self.origin.deployment_for_host(urlsplit(canonical_http_origin(team.public_origin)).netloc)
            except TeamError:
                pass
            else:
                raise ValueError('The PAW console origin must be outside the deployment hostname range')
        self._lock = RLock()
        self._closed = Event()
        self._jobs: dict[str, Thread] = {}
        self._cancelled: set[str] = set()
        # Durable ready is a previous process's observation, never a live claim.
        for item in self.store.list_live():
            self.store.mark_recovery_required(str(item['id']), 'Server restarted; previous preview needs verified cleanup')
        self._reaper: Thread | None = None
        if runtime is not None:
            self._reaper = Thread(target=self._reap_loop, name='paw-preview-cleanup', daemon=True)
            self._reaper.start()

    @property
    def configured(self) -> bool:
        return self.runtime is not None and self.origin is not None and not self._closed.is_set()

    def status(self, actor: str, space_id: str) -> dict[str, object]:
        scope = self.team.identity.require_space(actor, space_id)
        with self._lock:
            value = self.store.public_status(actor, space_id)
            active = value['active']
            if active and (not self.configured or not self.runtime.is_running(active['id'])):
                self.store.mark_recovery_required(active['id'], 'Preview process is unavailable; cleanup is pending')
                value = self.store.public_status(actor, space_id)
            return {
                **value, 'configured': self.configured, 'canManage': scope['role'] in {'owner', 'maintainer'},
                'openPath': f'/api/team/projects/{quote(space_id, safe="")}/preview/open' if value['active'] else None,
            }

    def start(self, actor: str, space_id: str, client_request_id: object) -> dict[str, object]:
        self.team.identity.require_space(actor, space_id, action='manage')
        with self._lock:
            if not self.configured:
                raise TeamError(503, 'preview_not_configured', 'Configure the isolated preview service and its dedicated origin')
            baseline = self.team.workspaces.preview_baseline(actor, space_id)
            deployment = self.store.reserve(
                actor, space_id, branch=baseline['branch'], commit=baseline['commit'],
                requirements_revision=baseline['requirementsRevision'], client_request_id=client_request_id,
            )
            deployment_id = str(deployment['id'])
            if deployment['status'] == 'starting' and deployment_id not in self._jobs:
                if len(self._jobs) >= 2 or len(self.store.list_live()) > 8:
                    self.store.mark_failed(deployment_id, 'Preview capacity is busy; retry with a new request after current work finishes')
                else:
                    job = Thread(target=self._start_job, args=(actor, space_id, deployment_id), name='paw-preview-start', daemon=True)
                    self._jobs[deployment_id] = job
                    job.start()
            return self.status(actor, space_id)

    def _still_starting(self, deployment_id: str) -> bool:
        return (not self._closed.is_set() and deployment_id not in self._cancelled
                and self.store.snapshot(deployment_id)['status'] == 'starting')

    def _start_job(self, actor: str, space_id: str, deployment_id: str) -> None:
        launch_attempted = False
        try:
            deployment = self.store.snapshot(deployment_id)
            source = self.team.workspaces.create_preview_source(
                actor, space_id, branch=deployment['branch'], commit=deployment['commit'],
                requirements_revision=deployment['requirementsRevision'], deployment_id=deployment_id,
            )
            with self._lock:
                if not self._still_starting(deployment_id):
                    raise TeamError(409, 'preview_cancelled', 'Preview startup was stopped')
            # Build and health checks happen in the isolated runtime. The
            # existing active pointer is untouched throughout this slow work.
            launch_attempted = True
            self.runtime.start(deployment_id, source)
            with self._lock:
                if not self._still_starting(deployment_id):
                    raise TeamError(409, 'preview_cancelled', 'Preview startup was stopped')
                self.store.activate(actor, deployment_id)
        except Exception as exc:
            message = exc.message if isinstance(exc, TeamError) else 'Preview startup failed; inspect the operator runtime logs'
            if not isinstance(exc, TeamError):
                _LOGGER.exception('Isolated preview startup failed for %s', deployment_id)
            with self._lock:
                self._remove(deployment_id, message, failed=deployment_id not in self._cancelled,
                             never_launched=not launch_attempted)
        finally:
            with self._lock:
                self._jobs.pop(deployment_id, None)
                self._cancelled.discard(deployment_id)

    def _remove(self, deployment_id: str, message: str, *, failed: bool = False, never_launched: bool = False) -> None:
        try:
            if not never_launched:
                receipt = self.runtime.stop(deployment_id) if self.runtime is not None else None
                if receipt is None or not receipt.verified:
                    raise RuntimeError('Container removal is not verified')
        except Exception:
            self.store.mark_recovery_required(deployment_id, 'Preview stop could not be verified; runtime cleanup is pending')
            _LOGGER.warning('Preview cleanup pending for %s', deployment_id)
            return
        if failed:
            self.store.mark_failed(deployment_id, message)
        else:
            self.store.mark_stopped(deployment_id, message)
        try:
            self.team.workspaces.remove_preview_source(deployment_id)
        except (OSError, TeamError):
            # Container removal is already known. A retained source directory
            # is an operator cleanup issue, not evidence of a live process.
            _LOGGER.exception('Stopped preview source cleanup failed for %s', deployment_id)

    def stop(self, actor: str, space_id: str) -> dict[str, object]:
        self.team.identity.require_space(actor, space_id, action='manage')
        with self._lock:
            for item in self.store.list_live():
                if item['spaceId'] != space_id:
                    continue
                deployment_id = str(item['id'])
                self._cancelled.add(deployment_id)
                # Deny leases and late activation before performing slow stop.
                self.store.mark_recovery_required(deployment_id, 'Preview stop requested; checking container removal')
                if deployment_id not in self._jobs:
                    self._remove(deployment_id, 'Preview stopped by a project maintainer')
                    self._cancelled.discard(deployment_id)
            return self.status(actor, space_id)

    def open_url(self, actor: str, space_id: str, login_session_id: str) -> str:
        if not self.status(actor, space_id)['active'] or self.origin is None:
            raise TeamError(409, 'preview_unavailable', 'No healthy shared preview is available')
        ticket = self.store.issue_ticket(actor, space_id, login_session_id=login_session_id)
        return self.origin.origin(ticket['deployment']['id']) + '/__paw/enter?ticket=' + quote(ticket['ticket'], safe='')

    def reap(self) -> None:
        with self._lock:
            live = self.store.list_live()
            retained: dict[str, list[dict[str, object]]] = {}
            for item in live:
                if item['status'] == 'retained':
                    retained.setdefault(str(item['spaceId']), []).append(item)
            expired = {
                str(item['id']) for items in retained.values()
                for index, item in enumerate(reversed(items))
                if index > 0 or time.time() * 1000 - int(item['updatedAtMs']) >= 900_000
            }
            for item in live:
                deployment_id = str(item['id'])
                if deployment_id in self._jobs:
                    continue
                if (item['status'] == 'recovery_required' or deployment_id in expired
                        or not self.runtime.is_running(deployment_id)):
                    self.store.mark_recovery_required(deployment_id, 'Preview runtime cleanup is pending')
                    self._remove(deployment_id, 'Preview retired; reopen the project preview for its current version')

    def _reap_loop(self) -> None:
        while not self._closed.is_set():
            try:
                self.reap()
            except Exception:
                _LOGGER.exception('Project preview cleanup could not complete')
            self._closed.wait(15)

    def begin_close(self) -> None:
        """Revoke access before waiting for validation and runtime shutdown."""
        self._closed.set()
        with self._lock:
            for item in self.store.list_live():
                deployment_id = str(item['id'])
                self._cancelled.add(deployment_id)
                self.store.mark_recovery_required(deployment_id, 'Server stopping; checking preview cleanup')

    def close(self) -> None:
        self.begin_close()
        if self._reaper is not None:
            self._reaper.join(timeout=5)
        if self.runtime is not None:
            try:
                # The runtime seals its launch boundary before stopping live
                # handles, so a job between validation and start cannot launch
                # a new container after shutdown returns.
                self.runtime.shutdown()
            except Exception:
                _LOGGER.exception('Preview runtime shutdown needs recovery')
        with self._lock:
            jobs = list(self._jobs.values())
        deadline = time.monotonic() + 10
        for job in jobs:
            job.join(timeout=max(0, deadline - time.monotonic()))
        with self._lock:
            for item in self.store.list_live():
                deployment_id = str(item['id'])
                if deployment_id not in self._jobs:
                    self._remove(deployment_id, 'Preview stopped with the server')
            if self._jobs:
                raise TeamError(503, 'preview_shutdown_pending', 'Preview startup has not finished stopping; recovery is required')
