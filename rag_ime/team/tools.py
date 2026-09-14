"""Restrict the existing tool gateway to a current team execution scope."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..agent_tools import ControlToolGateway, _normalize_runtime_tool_call
from ..agent_workspace import WorkspaceHarness
from .errors import TeamError
from .sessions import TEAM_TOOLS


_KNOWLEDGE_READ_OPERATIONS = frozenset({
    'list_bases', 'get_base', 'list_documents', 'search', 'find', 'open', 'status',
})
_KNOWLEDGE_IMPORT_OPERATIONS = frozenset({'import_text', 'import_document', 'import'})


class UnavailableTeamWorkspace(WorkspaceHarness):
    """An unconfigured worker cannot silently use the host WorkspaceHarness."""
    def __getattribute__(self, name: str) -> Any:
        if name in {
            'list', 'read', 'search', 'patch', 'edit', 'write', 'shell',
            'run_shell', 'save_file', 'spawn_background', 'apply_patch',
            'prepare_command', 'prepare_background_command', 'prepare_patch', 'prepare_edit', 'prepare_write',
            'apply_edit', 'apply_write', 'execute', 'execute_cancellable', 'prepare_lsp_mutation',
            'apply_lsp_mutation', 'lsp_read',
        }:
            raise TeamError(503, 'team_worker_unavailable', 'Configure the isolated team worker to use task files')
        return super().__getattribute__(name)

    def lsp_status(self, session: Mapping[str, object], payload: Mapping[str, object]) -> dict[str, object]:
        return {'available': False, 'reason': 'Language servers are not enabled in this worker'}

    def file_editability(self, session: Mapping[str, object], path: str) -> dict[str, object]:
        return {'editable': False, 'reason': 'The isolated team worker is unavailable'}


class TeamToolGateway(ControlToolGateway):
    def __init__(self, *, team: Any, space_id: str, **kwargs: Any) -> None:
        self.team = team
        self.space_id = space_id
        connections = getattr(team, 'connections', None)
        if connections is not None and connections.configured:
            kwargs['connections'] = connections
        super().__init__(**kwargs)

    def _manifest_items(self, *args: Any, **kwargs: Any) -> list[dict[str, object]]:
        return [item for item in super()._manifest_items(*args, **kwargs) if item['id'] in TEAM_TOOLS]

    def _authorize_team_tool(
        self,
        binding: Mapping[str, object],
        tool: str,
        args: Mapping[str, object],
    ) -> None:
        actor = str(binding['ownerUserId'])
        operation = str(args.get('op') or '')
        if tool == 'knowledge':
            if operation == 'find' and (
                args.get('useRegex') is True or args.get('regex') is True
            ):
                raise TeamError(
                    403,
                    'regex_not_allowed',
                    'Team Knowledge document search accepts literal patterns only',
                )
            action = (
                'read' if operation in _KNOWLEDGE_READ_OPERATIONS
                else 'write' if operation in _KNOWLEDGE_IMPORT_OPERATIONS
                else 'manage'
            )
            self.team.identity.require_space(actor, self.space_id, action=action)
            return
        if tool == 'memory':
            # Memory remains backed by the existing per-space Agent database.
            # A project Session must not write a purportedly personal capture
            # into that shared project root, where another member could read it.
            space = self.team.identity.require_space(actor, self.space_id, action='read')
            if (
                space.get('kind') == 'project'
                and operation == 'capture'
                and str(args.get('captureScope') or 'project').strip().lower() == 'user'
            ):
                raise TeamError(
                    403,
                    'personal_memory_not_in_team_space',
                    'Personal Memory must be captured from the personal space',
                )

    def execute(self, payload: Mapping[str, object]) -> dict[str, object]:
        session_id = str(payload.get('sessionId') or '')
        binding = self.team.grants.binding(session_id)
        if binding['spaceId'] != self.space_id:
            raise TeamError(403, 'execution_not_authorized', 'Tool scope does not match this space')
        args = payload.get('args')
        tool, normalized = _normalize_runtime_tool_call(
            str(payload.get('tool') or ''), args if isinstance(args, Mapping) else {},
        )
        if tool not in TEAM_TOOLS:
            raise TeamError(403, 'tool_not_available', 'This capability is unavailable in a team task')
        if normalized.get('executionMode') == 'full_trust' or normalized.get('codexSkillsEnabled') is True:
            raise TeamError(403, 'team_scope_required', 'Tools cannot expand their task permissions')
        self._authorize_team_tool(binding, tool, normalized)
        if tool == 'work_documents' and normalized.get('op') == 'register':
            root = str(normalized.get('workspaceRoot') or '')
            if root not in {'/workspace', str(binding['workspacePath'])}:
                raise TeamError(403, 'workspace_scope_fixed', 'Documents must belong to this task workspace')
            normalized = {**normalized, 'workspaceRoot': str(binding['workspacePath'])}
            payload = {**payload, 'tool': tool, 'args': normalized}
        if tool == 'work_documents' and normalized.get('documentId') and normalized.get('op') not in {'get', 'list', 'history.search'}:
            document = self.work_documents.detail(str(normalized['documentId']))['document']
            if document['workspaceRoot'] != binding['workspacePath']:
                raise TeamError(403, 'workspace_scope_fixed', 'Only the owning task can change this working document')
        # Tools that explicitly address another ordinary Session keep the
        # human ownership check. Room dispatch has its own exact active grant.
        if tool != 'room_partner':
            target = str(normalized.get('sessionId') or '')
            if target and target != session_id:
                read = str(normalized.get('op') or '') in {'get', 'list', 'read', 'search', 'status', 'console'}
                self.team.grants.require_session(
                    str(binding['ownerUserId']), self.space_id, target, action='read' if read else 'write',
                )
        with self.sessions.as_actor(str(binding['ownerUserId'])):
            return super().execute(payload)


__all__ = ['TeamToolGateway', 'UnavailableTeamWorkspace']
