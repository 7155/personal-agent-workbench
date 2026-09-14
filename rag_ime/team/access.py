"""Explicit team route and resource authorization over the existing Control API."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..control_api.errors import ControlApiError
from ..control_api.models import ControlAccessContext, ControlRequest
from ..control_api.route_policy import ControlPathId, default_route_policy
from .errors import TeamError
from .grants import TeamGrantStore
from .identity import TeamIdentityStore


# These routes operate on a space-bound service/database. Native installation,
# host configuration, provider secrets and desktop bridges are deliberately
# absent. Adding a route requires its resource/side-effect owner to be scoped.
_READ_PATHS = frozenset('''
control.bootstrap control.capabilities control.events system.health overview.get
agent.runtime.get agent.configuration.get agent.sessions.list agent.session.snapshot
agent.session.workspace.list agent.session.workspace.read agent.session.events
agent.session.workflow.get agent.session.commands agent.session.models
agent.session.backgroundJobs.list agent.session.backgroundJob.get agent.session.backgroundJob.logs
agent.session.intercom.list agent.session.contextItems.list agent.session.contextTraces.list
agent.session.contextTrace.get agent.session.forks.list agent.rooms.list agent.room.get
agent.room.snapshot agent.room.conversationSnapshot agent.room.history agent.room.events
agent.room.startGate.get agent.room.topics agent.room.artifacts agent.room.workItems.list
agent.room.workItem.get agent.roles.list agent.role.models agent.subagents.templates
agent.tools.list agent.memoryMaintenance.run workDocuments.list workDocuments.history.search workDocuments.get
agent.media.list agent.media.preview agent.artifact.get agent.knowledge.search agent.knowledge.read
agent.subagent.get agent.subagent.console agent.memorySources.list
files.list files.read planning.dashboard configuration.settings configuration.schema
memory.summary memory.pages memory.reference.get memory.graph.get memory.entity.get
memory.lifecycle.status memory.lifecycle.report knowledgeBases.list knowledgeBases.get
knowledgeBases.documents.list knowledgeBases.document.get knowledgeBases.document.source
knowledgeBases.asset.get knowledgeBases.jobs.list knowledgeBases.graph.get
knowledgeWorker.health knowledgeParsers.list
'''.split())

# Knowledge uses POST for search/find, so those canonical routes belong to the
# read set.  Keep the product's document-management routes separate from the
# generic Session write set: contributors may import into an existing project
# base, while only project managers may change configuration or lifecycle.
_KNOWLEDGE_READ_PATHS = frozenset('''
knowledgeBases.search knowledgeBases.find knowledgeBases.open knowledgeEmbedding.profile
'''.split())

_KNOWLEDGE_IMPORT_PATHS = frozenset('''
knowledgeBases.document.import
'''.split())

_KNOWLEDGE_MANAGE_PATHS = frozenset('''
knowledgeBases.create knowledgeBases.update knowledgeBases.delete.preview knowledgeBases.delete.apply
knowledgeBases.document.retry knowledgeBases.document.delete knowledgeBases.job.cancel
knowledgeBases.chunkPreview knowledgeBases.graph.rebuild knowledgeBases.reindexPreview
knowledgeBases.rebuild knowledgeEmbedding.probe knowledgeEmbedding.impact
'''.split())

_WRITE_PATHS = frozenset('''
agent.sessions.create agent.sessions.surface.ensure agent.runtime.ensure agent.session.rename agent.session.mode.update
agent.session.archive agent.session.delete agent.session.prompt agent.session.rewrite
agent.session.forks.create agent.session.abort agent.session.review.resolve agent.session.ui.resolve
agent.session.compact agent.session.command.invoke agent.session.model.select agent.session.thinking.select
agent.session.workspace.save agent.session.backgroundJob.cancel agent.session.contextItems.ack
agent.session.intercom.send agent.session.goal.mutate agent.rooms.create agent.room.message
agent.room.topic.create agent.room.topic.update agent.room.artifact.add agent.room.artifact.update
agent.room.workItem.create agent.room.participant.add agent.room.participant.steer
agent.subagents.create agent.subagent.control agent.subagent.abort
'''.split())

_MANAGE_PATHS = frozenset('''
agent.room.archive agent.room.delete agent.room.participant.remove agent.room.participant.update
agent.room.startGate.confirm agent.room.abort agent.room.workItem.reassign agent.room.workItem.resume
agent.roles.create agent.roles.update agent.roles.archive
'''.split())


@dataclass(frozen=True)
class AuthorizedTeamRequest:
    actor_user_id: str
    space: Mapping[str, object]
    request: ControlRequest
    context: ControlAccessContext
    actor_display_name: str = ''


class TeamAccessPolicy:
    def __init__(self, identity: TeamIdentityStore, grants: TeamGrantStore) -> None:
        self.identity = identity
        self.grants = grants
        # Fail startup on a typo in an allowlist rather than silently exposing
        # a path that has no corresponding canonical contract.
        known = {item.value for item in ControlPathId}
        unknown = (
            _READ_PATHS
            | _WRITE_PATHS
            | _MANAGE_PATHS
            | _KNOWLEDGE_READ_PATHS
            | _KNOWLEDGE_IMPORT_PATHS
            | _KNOWLEDGE_MANAGE_PATHS
        ) - known
        if unknown:
            raise ValueError(f'Unknown team Control routes: {sorted(unknown)}')
        self.read_paths = _READ_PATHS | _KNOWLEDGE_READ_PATHS
        self.write_paths = _WRITE_PATHS | _KNOWLEDGE_IMPORT_PATHS
        self.manage_paths = _MANAGE_PATHS | _KNOWLEDGE_MANAGE_PATHS

    def context(self, actor: str, space_id: str) -> tuple[dict[str, object], ControlAccessContext]:
        space = self.identity.require_space(actor, space_id)
        paths = set(self.read_paths)
        if space['role'] in {'owner', 'maintainer', 'contributor'}:
            paths.update(self.write_paths)
        if space['role'] in {'owner', 'maintainer'}:
            paths.update(self.manage_paths)
        return space, ControlAccessContext.team(user_id=actor, space_id=space_id, allowed_paths=paths)

    def authorize_http(
        self, actor: str, space_id: str, *, method: str, path: str,
        query: Mapping[str, object], body: Mapping[str, object],
    ) -> AuthorizedTeamRequest:
        space, context = self.context(actor, space_id)
        policy = default_route_policy()
        errors: list[ControlApiError] = []
        for request in policy.resolve_http_requests(method=method, path=path, query=query, body=body):
            try:
                policy.authorize(request, context)
            except ControlApiError as exc:
                errors.append(exc)
                continue
            self._resources(actor, space, request)
            return AuthorizedTeamRequest(actor, space, request, context)
        raise errors[0]

    def _resources(self, actor: str, space: Mapping[str, object], request: ControlRequest) -> None:
        path = request.path_id
        # Workspace authority is assigned by the server at creation. Later
        # requests cannot change it through the old local capability controls.
        if request.body.get('executionMode') == 'full_trust' or request.body.get('dangerousModeConfirmation'):
            raise TeamError(403, 'team_scope_required', 'Team tasks use their assigned workspace permissions')
        if request.body.get('toolProfileVersion') in {'control-center-full-access-v1', 'control-center-auto-approve-v1'}:
            raise TeamError(403, 'team_scope_required', 'Unrestricted host access is unavailable in team spaces')
        if request.body.get('codexSkillsEnabled') is True:
            raise TeamError(403, 'team_scope_required', 'Personal host skills are not shared with this task')
        values = {**request.query, **request.body, **request.params}
        session_id = str(values.get('sessionId') or '')
        if session_id:
            action = 'stop' if path in {'agent.session.abort', 'agent.session.backgroundJob.cancel'} else (
                'read' if path in self.read_paths else 'write'
            )
            self.grants.require_session(actor, str(space['id']), session_id, action=action)

    def readable_session(self, actor: str, space_id: str, session_id: str) -> bool:
        try:
            self.grants.require_session(actor, space_id, session_id)
            return True
        except TeamError:
            return False


__all__ = ['AuthorizedTeamRequest', 'TeamAccessPolicy']
