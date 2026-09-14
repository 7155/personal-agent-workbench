"""Team publication and task assignment of existing PAW Package sources.

This deployment boundary combines trusted source preparation with current
platform/space authority. Pi retains installation and execution ownership in
each isolated task Host.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any

from ..agent_extensions import _discover_skill_files, _read_skill_file
from ..agent_skill_routing import AGENT_LAB_OWNER_APP_ID, TRACE_AGENT_OWNER_APP_ID
from .errors import TeamError
from .shared_packages import StagedTeamPackage, TeamPackageCatalog, TeamPackageError
from .shared_resource_store import TeamSharedResourceStore


_APP_ID = re.compile(r'^extension:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_BINDING_CAPABILITY = re.compile(r'^pawos\.extension\.binding\.[0-9a-f]{40}$')
_APP_EVIDENCE_FIELDS = (
    'id', 'packageId', 'version', 'bindingSha256', 'skillRef',
    'skillSha256', 'verticalSuiteId', 'verticalSuiteRevision',
    'bindingCapability', 'packageDigest',
)


class TeamSharedResources:
    def __init__(self, team: Any, *, source_root: Path | None = None, catalog_path: Path | None = None) -> None:
        self.team = team
        if (source_root is None) != (catalog_path is None):
            raise ValueError('Configure both the Package source root and catalog path')
        bundle = Path(__file__).resolve().parents[1]
        self.catalog = TeamPackageCatalog(
            source_root=source_root if source_root is not None else bundle,
            catalog_path=catalog_path if catalog_path is not None else bundle / 'plugin_catalog.json',
            storage_root=team.data_root / 'shared-packages',
        )
        self.store = TeamSharedResourceStore(team.identity)

    def _admin(self, actor: str) -> None:
        with self.team.identity._connection() as conn:
            self.team.identity._require_admin(conn, actor)

    def catalog_items(self, actor: str) -> list[dict[str, object]]:
        self._admin(actor)
        try:
            items = self.catalog.list_available()
        except TeamPackageError as exc:
            raise TeamError(400, exc.code, str(exc)) from None
        self._admin(actor)
        return items

    def publish(self, actor: str, body: dict[str, object]) -> dict[str, object]:
        self._admin(actor)
        if set(body) != {'packageId', 'version'} or not all(isinstance(body[key], str) for key in body):
            raise TeamError(400, 'invalid_request', 'Provide only an exact packageId and version')
        try:
            staged = self.catalog.stage_install(str(body['packageId']), str(body['version']))
        except TeamPackageError as exc:
            raise TeamError(400, exc.code, str(exc)) from None
        # Preparation is reversible and has no execution side effect. Recheck
        # current admin authority in the publication transaction after staging.
        return self.store.publish(actor, staged)

    def for_creation(
        self,
        actor: str,
        space_id: str,
        parent_session_id: str | None = None,
    ) -> dict[str, object]:
        """Return the resource set a new Team Session may pin.

        A root task reads the current space selection.  A child task reads the
        already frozen parent snapshot, so withdrawing a package cannot rewrite
        an existing task's dependency set.  The write-role check happens before
        workspace allocation in ``TeamSessionStore.create``.
        """

        self.team.identity.require_space(actor, space_id, action='write')
        if parent_session_id:
            self.team.grants.require_session(
                actor,
                space_id,
                parent_session_id,
                action='write',
            )
            return self.store.session_snapshot(actor, space_id, parent_session_id)
        selection = self.store.selection(actor, space_id)
        items = selection.get('items')
        if not isinstance(items, list):
            raise TeamError(500, 'resource_store_corrupt', 'Current resource selection is invalid')
        if any(
            not isinstance(item, Mapping) or str(item.get('status') or '') != 'published'
            for item in items
        ):
            raise TeamError(
                409,
                'resource_selection_stale',
                'The current resource selection contains a withdrawn package',
            )
        return dict(selection)

    def assert_app(
        self,
        snapshot: Mapping[str, object],
        owner_app_id: object,
        surface_kind: object,
    ) -> dict[str, object]:
        """Ensure an App-owned Session is backed by its fixed package evidence.

        The ordinary Agent and existing Trace/Agent Lab surfaces keep their
        established routing.  Other Extension Apps must match the complete,
        content-addressed evidence emitted by ``TeamPackageCatalog``; a client
        supplied version or source path is never consulted.
        """

        if not isinstance(snapshot, Mapping):
            raise TeamError(500, 'resource_store_corrupt', 'Session resource snapshot is invalid')
        normalized_surface = str(surface_kind or 'agent').strip().lower()
        if normalized_surface != 'extension_app':
            return dict(snapshot)
        normalized_owner = str(owner_app_id or '').strip()
        if normalized_owner in {TRACE_AGENT_OWNER_APP_ID, AGENT_LAB_OWNER_APP_ID}:
            return dict(snapshot)
        if _APP_ID.fullmatch(normalized_owner) is None:
            raise TeamError(403, 'app_not_selected', 'This Extension App is not selected for the space')
        raw_items = snapshot.get('items')
        if not isinstance(raw_items, list):
            raise TeamError(403, 'app_not_selected', 'This Extension App is not selected for the space')
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                continue
            raw_metadata = raw_item.get('metadata')
            evidence = raw_metadata.get('extensionApp') if isinstance(raw_metadata, Mapping) else None
            if not isinstance(evidence, Mapping) or str(evidence.get('id') or '') != normalized_owner:
                continue
            if not self._verified_app_evidence(raw_item, evidence):
                continue
            sandbox = evidence.get('sandbox')
            if sandbox is not None:
                if not isinstance(sandbox, Mapping):
                    raise TeamError(403, 'app_sandbox_unavailable', 'This Extension App sandbox contract is invalid')
                default = str(sandbox.get('default') or '').strip().lower()
                if default == 'required':
                    raise TeamError(
                        403,
                        'app_sandbox_unavailable',
                        'This Extension App requires a sandbox adapter unavailable to Team Sessions',
                    )
                if default not in {'optional', 'disabled'}:
                    raise TeamError(403, 'app_sandbox_unavailable', 'This Extension App sandbox contract is invalid')
            return dict(snapshot)
        raise TeamError(403, 'app_not_selected', 'This Extension App is not selected for the space')

    @staticmethod
    def _verified_app_evidence(
        item: Mapping[str, object],
        evidence: Mapping[str, object],
    ) -> bool:
        package_id = str(item.get('packageId') or '')
        version = str(item.get('version') or '')
        digest = str(item.get('digest') or '')
        if not package_id or not version or _SHA256.fullmatch(digest) is None:
            return False
        for field in _APP_EVIDENCE_FIELDS:
            value = evidence.get(field)
            if not isinstance(value, str) or not value.strip():
                return False
        if (
            str(evidence.get('packageId')) != package_id
            or str(evidence.get('version')) != version
            or str(evidence.get('packageDigest')) != digest
            or _SHA256.fullmatch(str(evidence.get('bindingSha256'))) is None
            or _SHA256.fullmatch(str(evidence.get('skillSha256'))) is None
            or _BINDING_CAPABILITY.fullmatch(str(evidence.get('bindingCapability'))) is None
        ):
            return False
        binding = str(evidence.get('bindingSha256'))
        capability = str(evidence.get('bindingCapability'))
        return capability == 'pawos.extension.binding.' + binding[:40]

    def execution_packages(self, session_id: str) -> tuple[StagedTeamPackage, ...]:
        snapshot = self.store.snapshot_for_binding(session_id)
        packages = []
        for item in snapshot['items']:
            try:
                staged = self.catalog.load(str(item['digest']))
            except TeamPackageError as exc:
                raise TeamError(503, 'task_resources_unavailable', 'The fixed task resource is unavailable; restore its published bytes before resuming') from exc
            if (staged.package_id, staged.version) != (item['packageId'], item['version']):
                raise TeamError(503, 'task_resources_unavailable', 'The fixed task resource identity changed')
            packages.append(staged)
        # Reading fixed files must not outlive membership revocation.
        self.team.grants.binding(session_id)
        return tuple(packages)


def package_skill_refs(packages: tuple[StagedTeamPackage, ...]) -> tuple[str, ...]:
    """Reuse product Skill parsing on the exact verified Package roots only."""
    names: dict[str, Path] = {}
    for package in packages:
        pi = package.manifest.get('pi')
        if not isinstance(pi, dict):
            continue
        for reference in pi.get('skills', []):
            # TeamPackageCatalog has already verified these relative paths and
            # all package bytes. No ambient or project-local Skill roots enter.
            source = package.path / str(reference)
            for path in _discover_skill_files(source):
                parsed = _read_skill_file(path)
                if parsed is None:
                    raise TeamError(400, 'invalid_package_skill', 'A selected Package Skill has invalid metadata')
                name = parsed[0]
                if name in names and names[name] != path:
                    raise TeamError(409, 'duplicate_package_skill', 'Selected Packages contain the same Skill name')
                names[name] = path
                if len(names) > 128:
                    raise TeamError(400, 'package_skill_limit', 'A task can load at most 128 named Skills')
    return tuple(sorted(names))


__all__ = ['TeamSharedResources', 'package_skill_refs']
