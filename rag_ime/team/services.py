"""Compose existing PAW services from explicit personal/project dependencies."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
import json

from ..agent_configuration import default_agent_configuration
from ..agent_execution_policy import WORKSPACE_SCOPE_CONFIRMATION
from ..agent_service import AgentService
from ..debug_server import DebugImeService, DebugServerConfig
from ..embeddings import NullEmbeddingProvider
from ..local_sqlite_core import LocalSqliteCoreClient
from ..pi.config import PiRuntimeConfig
from ..pi.factory import PiRuntimeDriverFactory
from ..predictor import NullPredictionProvider
from .access import AuthorizedTeamRequest
from .errors import TeamError
from .grants import execution_session_key
from .files import TeamFiles
from .knowledge import TeamKnowledgeWorker
from .knowledge_control import TeamKnowledgeControl
from .resource_policy import authorize_resources
from .sessions import TeamSessionStore
from .tools import TeamToolGateway, UnavailableTeamWorkspace


class TeamSpaceService(DebugImeService):
    def __init__(self, team: Any, space: Mapping[str, object]) -> None:
        self.team = team
        self.space_id = str(space['id'])
        root = team.data_root / 'spaces' / self.space_id
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        files = root / 'files'
        files.mkdir(exist_ok=True, mode=0o700)
        db = root / 'state.sqlite'
        sessions = TeamSessionStore(db, team=team, space=space)
        execution = team.execution
        runtime_config = PiRuntimeConfig(
            enabled=execution is not None, executable=None,
            agent_dir=root / 'runtime-config', session_dir=root / 'transcripts', logs_dir=root / 'logs',
            provider='team', model=execution.model_id if execution is not None else 'unconfigured',
            model_configured=execution is not None,
            model_configuration_error='' if execution is not None else 'Configure an isolated worker and team model service',
            provider_environment={}, model_providers={},
        )
        def resolve(session: Mapping[str, object]) -> Any:
            if self.team.execution is None:
                raise TeamError(503, 'team_worker_unavailable', 'The team execution worker is not configured')
            return self.team.execution.binding_for(self.space_id, session)
        factory = PiRuntimeDriverFactory(
            runtime_config, execution_binding_resolver=resolve,
            execution_stop=execution.revoke_session if execution is not None else None,
            execution_history_root=self.history_root,
        )
        harness = execution.workspace_harness(self.space_id) if execution is not None else UnavailableTeamWorkspace()
        defaults = default_agent_configuration(
            enabled=execution is not None, coordinator_enabled=True,
            model_profile=f'team/{runtime_config.model}',
        )
        for route in defaults['modelRouting'].values():
            route['modelProfile'] = 'inherit'
        agent = AgentService(
            db_path=db, project=self.space_id, runtime_config=runtime_config, runtime_factory=factory,
            session_store=sessions, workspace_harness=harness,
            human_actor_provider=sessions.human_actor,
            room_participant_identity_provider=sessions.participant_identity,
            additional_session_context_provider=self.requirements_context if space['kind'] == 'project' else None,
            configuration_defaults=defaults,
            memory_embedding_provider=NullEmbeddingProvider(),
            background_job_execution_owner=False, startup_recovery_enabled=False,
            tool_gateway_url='http://127.0.0.1:8766/api/agent/tool/execute',
        )
        worker = TeamKnowledgeWorker(
            root / 'knowledge',
            settings_provider=lambda: self.settings_store.get_settings(),
            intake_db_path=db,
        )
        self._team_knowledge = worker
        super().__init__(DebugServerConfig(
            db_path=db, project=self.space_id, static_dir=team.static_dir,
            seed_if_empty=False, core=LocalSqliteCoreClient(db, embedding_provider=NullEmbeddingProvider()),
            predictor=NullPredictionProvider(), server_name='team space',
            agent_service=agent, knowledge_client=worker,
            memory_projection_worker_enabled=False, workspace_root=files,
            project_skill_roots=(), plugin_inbox_root=root / 'plugins', integration_environment={},
            tool_gateway_factory=lambda **kwargs: TeamToolGateway(team=team, space_id=self.space_id, **kwargs),
            rime_user_dir=root / 'disabled-native', rime_lexicon_backup_root=root / 'disabled-native-backups',
        ))
        self.knowledge_control = TeamKnowledgeControl(
            worker=worker,
            work_contract=self.management.work_contract,
            identity=team.identity,
            space_id=self.space_id,
            actor_provider=self.agent.sessions.current_actor,
        )
        self.agent_tools.knowledge_control = self.knowledge_control
        self.desktop_files = TeamFiles(files)

    def requirements_context(self, session: Mapping[str, object]) -> str:
        requirements = self.team.workspaces.requirements_for_session(str(session['id']))
        if requirements['spaceId'] != self.space_id:
            raise TeamError(404, 'resource_not_found', 'Session not found in this space')
        if not requirements['requirementsRevision']:
            return ''
        return (
            '## Versioned project requirements\n'
            'The following project data is the fixed requirements baseline for this Session. '
            'It does not grant additional access or certify that the work satisfies the requirements.\n'
            + json.dumps(requirements, ensure_ascii=False)
        )

    def history_root(self, session_id: str) -> Path:
        binding = self.team.grants.binding(session_id, check_current=False)
        if binding['spaceId'] != self.space_id:
            raise TeamError(404, 'resource_not_found', 'Session not found in this space')
        return self.team.data_root / 'execution' / execution_session_key(session_id) / 'sessions'

    def team_request_context(self, request: AuthorizedTeamRequest):
        return self.agent.sessions.as_actor(request.actor_user_id, display_name=request.actor_display_name)

    def prepare_team_request(self, request: AuthorizedTeamRequest) -> dict[str, object]:
        body = authorize_resources(self.team, self, request, request.request.body)
        path = request.request.path_id
        if self.team.execution is None and path in {
            'agent.runtime.ensure', 'agent.session.prompt', 'agent.session.rewrite',
            'agent.session.compact', 'agent.session.command.invoke', 'agent.session.forks.create',
            'agent.session.model.select', 'agent.session.thinking.select',
            'agent.session.intercom.send', 'agent.room.message', 'agent.room.participant.steer',
            'agent.room.startGate.confirm', 'agent.room.workItem.resume',
            'agent.subagents.create', 'agent.subagent.control',
        }:
            # This is a definite admission rejection, before any dispatch or
            # pending user-message receipt exists. Preserve ambiguous outcomes
            # only for requests which actually reached the runtime boundary.
            raise TeamError(409, 'team_worker_unavailable', '团队尚未配置执行服务，请联系管理员配置模型与隔离运行环境。')
        if path == 'agent.session.model.select':
            configured = self.team.execution.model_id if self.team.execution is not None else 'unconfigured'
            if body.get('provider') != 'team' or body.get('modelId') != configured:
                raise TeamError(403, 'model_not_allowed', 'Choose the model service configured for this team')
        if path in {'agent.sessions.create', 'agent.sessions.surface.ensure'}:
            body.update(
                mode='coordinator', executionMode='workspace_managed',
                toolProfileVersion='control-center-v1', workspaceRoots=[],
                workspaceScopeConfirmation=WORKSPACE_SCOPE_CONFIRMATION,
                codexSkillsEnabled=False, piSkillsEnabled=False,
            )
            if path == 'agent.sessions.surface.ensure':
                body['surfaceKey'] = request.actor_user_id + ':' + str(body.get('surfaceKey') or '')
        if path == 'agent.session.mode.update':
            # Team SessionStore reasserts the assigned checkout and runtime
            # profile.  The Team UI's mode update intentionally omits the
            # personal-installation confirmation token; this server-owned
            # projection is the only confirmation accepted for that scoped
            # workspace policy.
            body['workspaceScopeConfirmation'] = WORKSPACE_SCOPE_CONFIRMATION
        if path == 'agent.rooms.create':
            # The Session store assigns each facilitator/partner independently.
            body.update(
                workspaceRoots=[str(self.config.workspace_root)], executionMode='workspace_managed',
                workspaceScopeConfirmation=WORKSPACE_SCOPE_CONFIRMATION,
            )
            body.pop('permissionPolicy', None)
        if path.startswith('agent.room.'):
            self._check_room_targets(request, body)
        return body

    def _check_room_targets(self, request: AuthorizedTeamRequest, body: dict[str, object]) -> None:
        path = request.request.path_id
        session_id = str(body.get('sessionId') or '')
        if session_id:
            self.team.grants.require_session(request.actor_user_id, self.space_id, session_id, action='write')
        participant_id = str(body.get('participantId') or request.request.params.get('participantId') or '')
        room_id = str(request.request.params.get('roomId') or '')
        if participant_id and room_id and path in {
            'agent.room.participant.steer', 'agent.room.participant.update', 'agent.room.participant.remove',
        }:
            room = self.agent.rooms.get(room_id)
            for participant in room.get('participants', []):
                if participant.get('id') == participant_id:
                    self.team.grants.require_session(
                        request.actor_user_id, self.space_id, str(participant.get('sessionId') or ''),
                        # Role metadata is already gated by the project manage
                        # route. It does not grant control of the target Pi loop.
                        action=('stop' if path.endswith('remove') else
                                'read' if path.endswith('update') else 'write'),
                    )

    def project_team_response(self, request: AuthorizedTeamRequest, payload: dict[str, object]) -> dict[str, object]:
        # The selected project is an explicitly shared space. Personal Sessions
        # live in another database and cannot enter its lists/events/search.
        bindings = {str(item['sessionId']): item for item in self.team.grants.list_bindings(self.space_id)}
        names = {str(item['id']): str(item['displayName'] or item['username'])
                 for item in self.team.identity.member_directory(request.actor_user_id)}
        def project(value: Any) -> Any:
            if isinstance(value, list):
                return [project(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {key: project(item) for key, item in value.items()}
            binding = bindings.get(str(value.get('id') or ''))
            if binding is None and 'collaborationRole' in value:
                binding = bindings.get(str(value.get('sessionId') or ''))
            if binding is not None:
                result.update(ownerUserId=binding['ownerUserId'], spaceId=self.space_id,
                              ownerDisplayName=names.get(str(binding['ownerUserId']), '已离开的成员'),
                              canControl=(binding['ownerUserId'] == request.actor_user_id
                                          and binding['active']
                                          and binding['membershipRevision'] == request.space['membershipRevision']
                                          and request.space['role'] in {'owner', 'maintainer', 'contributor'}),
                              audience=binding['audience'])
            return result
        result = project(payload)
        if request.request.path_id in {'control.bootstrap', 'control.capabilities'}:
            result['deployment'] = 'team'
            result['space'] = dict(request.space)
            result['features']['nativeBridge'] = False
        return result

    def close(self) -> None:
        super().close()
        self._team_knowledge.close()


def create_space_service(team: Any, space: Mapping[str, object]) -> TeamSpaceService:
    return TeamSpaceService(team, space)


__all__ = ['TeamSpaceService', 'create_space_service']
