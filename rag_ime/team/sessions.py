"""Human ownership around the existing Session store, without a second loop."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
import uuid

from ..agent_sessions import AgentSessionStore
from .errors import TeamError


TEAM_TOOLS = frozenset({
    'memory', 'knowledge', 'todo', 'agent_goal', 'room_partner', 'agents',
    'session_search', 'work_documents', 'structured_output', 'connections',
    'workspace_list', 'workspace_read', 'workspace_search', 'workspace_patch',
    'workspace_edit', 'workspace_write', 'workspace_shell',
})


class TeamSessionStore(AgentSessionStore):
    def __init__(self, db_path: Path, *, team: Any, space: Mapping[str, object]) -> None:
        super().__init__(db_path, persistent_reads=True)
        self.team = team
        self.space_id = str(space['id'])
        self.space_kind = str(space['kind'])
        self._actor: ContextVar[str] = ContextVar('team_session_actor_' + self.space_id, default='')
        self._actor_display_name: ContextVar[str] = ContextVar('team_actor_name_' + self.space_id, default='')
        self._requirements_revision: ContextVar[int | None] = ContextVar('team_requirements_' + self.space_id, default=None)
        self._resource_parent: ContextVar[str | None] = ContextVar('team_resource_parent_' + self.space_id, default=None)

    @contextmanager
    def as_actor(self, actor: str, *, display_name: str = '') -> Iterator[None]:
        self.team.identity.require_space(actor, self.space_id)
        token = self._actor.set(actor)
        display_token = self._actor_display_name.set(display_name)
        try:
            yield
        finally:
            self._actor.reset(token)
            self._actor_display_name.reset(display_token)

    def human_actor(self) -> dict[str, object]:
        display_name = self._actor_display_name.get()
        return {'userId': self._actor.get(), 'displayName': display_name} if display_name else {}

    def current_actor(self) -> dict[str, object]:
        """Return the trusted actor set by ``as_actor`` without display-name coupling."""
        actor = self._actor.get()
        if not actor:
            return {}
        return {
            'userId': actor,
            'displayName': self._actor_display_name.get(),
        }

    def participant_identity(self, session_id: str) -> str:
        binding = self.team.grants.binding(session_id, check_current=False)
        if binding['spaceId'] != self.space_id:
            raise TeamError(404, 'resource_not_found', 'Room participant not found in this space')
        return f"{binding['ownerUserId']}:{binding['membershipRevision']}"

    def _creation_actor(self) -> str:
        actor = self._actor.get()
        if actor:
            self.team.identity.require_space(actor, self.space_id, action='write')
            return actor
        raise TeamError(403, 'session_owner_required', 'An authenticated owner is required to create a team task')

    def create_child(self, parent_session_id: str, **kwargs: Any) -> dict[str, object]:
        # Background delegation has a persisted parent Session, not an actor
        # guessed from a model-supplied directory. This internal store method
        # is not exposed as an HTTP or model tool capability.
        parent = self.team.grants.binding(parent_session_id)
        actor = self._actor.get()
        if parent['spaceId'] != self.space_id or (actor and actor != parent['ownerUserId']):
            raise TeamError(403, 'session_owner_required', 'Child ownership must match the authenticated parent')
        requirements = self.team.workspaces.requirements_for_session(parent_session_id)
        token = self._requirements_revision.set(int(requirements['requirementsRevision']))
        resource_token = self._resource_parent.set(parent_session_id)
        try:
            with self.as_actor(str(parent['ownerUserId'])):
                return self.create(**kwargs)
        finally:
            self._requirements_revision.reset(token)
            self._resource_parent.reset(resource_token)

    def create(self, **kwargs: Any) -> dict[str, object]:
        actor = self._creation_actor()
        parent_session_id = self._resource_parent.get()
        resource_snapshot = self.team.shared_resources.for_creation(
            actor,
            self.space_id,
            parent_session_id=parent_session_id,
        )
        self.team.shared_resources.assert_app(
            resource_snapshot,
            kwargs.get('owner_app_id', ''),
            kwargs.get('surface_kind', 'agent'),
        )
        self.team.workspaces.ensure_project(self.space_id)
        workspace = self.team.workspaces.allocate_workspace(
            self.space_id, 'workspace_' + uuid.uuid4().hex, owner_user_id=actor, bind_session=False,
            requirements_revision=self._requirements_revision.get(),
        )
        path = Path(str(workspace['path']))
        # A team conversation always executes within its own assigned checkout.
        # read_only children retain their policy; ordinary chat uses the same
        # coordinator workspace contract that the product already supports.
        readonly = kwargs.get('execution_mode') == 'read_only'
        kwargs.update(
            mode='coordinator', workspace_roots=[str(path)],
            execution_mode='read_only' if readonly else 'workspace_managed',
            tool_profile_version='subagent-readonly-v1' if readonly else 'control-center-v1',
            codex_skills_enabled=False, pi_skills_enabled=False,
            model_profile='team/' + (self.team.execution.model_id if self.team.execution is not None else 'unconfigured'),
        )
        session = super().create(**kwargs)
        try:
            self.team.workspaces.attach_session(
                self.space_id, workspace['workspaceId'], str(session['id']), actor,
                audience='project' if self.space_kind == 'project' else 'owner',
            )
            captured_resources = self.team.shared_resources.store.capture_session(
                actor, self.space_id, str(session['id']), parent_session_id=parent_session_id,
            )
            self.team.shared_resources.assert_app(
                captured_resources,
                kwargs.get('owner_app_id', ''),
                kwargs.get('surface_kind', 'agent'),
            )
        except BaseException:
            # A failed snapshot may follow a successful workspace attach. Such
            # a partially created task must not start with unfixed resources.
            with self.team.identity._connection(write=True) as conn:
                conn.execute('UPDATE team_session_bindings SET active=0 WHERE session_id=? AND owner_user_id=?',
                             (str(session['id']), actor))
            raise
        return session

    def set_runtime_policy(self, session_id: str, **kwargs: Any) -> dict[str, object]:
        binding = self.team.grants.binding(session_id, check_current=False)
        # Room policy restoration calls this same store. Its shared directory
        # and personal full-access defaults cannot replace a team assignment.
        # A revoked Session remains visible as history, without restoring work.
        if not binding['active']:
            return self.get(session_id)
        readonly = kwargs.get('execution_mode') == 'read_only'
        kwargs.update(
            mode='coordinator', workspace_roots=[binding['workspacePath']],
            execution_mode='read_only' if readonly else 'workspace_managed',
            tool_profile_version='subagent-readonly-v1' if readonly else 'control-center-v1',
            grant_workspace_scope=not readonly,
        )
        kwargs['codex_skills_enabled'] = False
        kwargs['pi_skills_enabled'] = False
        if kwargs.get('allowed_tools') is not None:
            kwargs['allowed_tools'] = [tool for tool in kwargs['allowed_tools'] if tool in TEAM_TOOLS]
        return super().set_runtime_policy(session_id, **kwargs)


__all__ = ['TeamSessionStore', 'TEAM_TOOLS']
