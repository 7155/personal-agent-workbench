from __future__ import annotations

import unittest

from rag_ime.control_api import (
    ControlAccessContext, ControlApiError, ControlPathId, ControlRequest,
    Local8766Adapter, default_route_policy,
)
from rag_ime.control_api.route_table import build_arguments, find_route


class PrimaryAssistantRouteTests(unittest.TestCase):
    def test_local_routes_preserve_identity_and_version_fields(self) -> None:
        cases = (
            (ControlPathId.AGENT_PRIMARY_ENSURE, '/api/agent/primary/ensure',
             'agent.ensure_primary_assistant', {'workspaceRoots': ['/work/project']}),
            (ControlPathId.AGENT_PRIMARY_TASK_CREATE, '/api/agent/primary/tasks',
             'agent.create_primary_task', {
                 'clientRequestId': 'request-original', 'sourceSessionId': 'discussion',
                 'sourceMessageId': 'message-original', 'objective': 'Fix the agreed defect',
                 'acceptanceCriteria': ['Regression passes'], 'workspaceRoots': ['/work/project'],
                 'workspaceScopeConfirmation': 'APPROVE_WORKSPACE_SCOPE',
             }),
            (ControlPathId.MEMORY_PROFILE_SAVE, '/api/memory/profile/save',
             'management.save_personal_profile', {
                 'clientRequestId': 'save-original', 'expectedRevision': 'old-revision',
                 'paragraphs': [{'id': 'atom-1', 'memoryIds': ['atom-1'],
                                 'revision': 'old-atom-revision', 'text': 'Corrected fact'}],
             }),
        )
        policy = default_route_policy()
        for path_id, path, handler, body in cases:
            with self.subTest(path_id=path_id):
                request = ControlRequest(request_id='transport-id', path_id=path_id.value, body=body)
                route = policy.authorize(request, ControlAccessContext.native())
                prepared = Local8766Adapter().prepare(route, request, ControlAccessContext.native())
                self.assertEqual(prepared.path, path)
                self.assertEqual(prepared.body, body)
                descriptor = find_route('POST', path)
                self.assertIsNotNone(descriptor)
                self.assertEqual(descriptor.handler, handler)
                self.assertEqual(build_arguments(descriptor, payload=body, query_first=lambda _: ''), body)
                with self.assertRaises(ControlApiError):
                    policy.authorize(request, ControlAccessContext.remote(device_id='remote', scopes={'*'}))

    def test_profile_read_is_argument_free_and_local(self) -> None:
        descriptor = find_route('GET', '/api/memory/profile')
        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor.handler, 'management.personal_profile')
        self.assertFalse(descriptor.takes_arguments)
        request = ControlRequest(request_id='profile', path_id=ControlPathId.MEMORY_PROFILE.value)
        policy = default_route_policy()
        policy.authorize(request, ControlAccessContext.native())
        with self.assertRaises(ControlApiError):
            policy.authorize(request, ControlAccessContext.remote(device_id='remote', scopes={'*'}))

    def test_stale_memory_save_returns_recoverable_conflict(self) -> None:
        from rag_ime.memory_card_mutations import MemoryRevisionConflict
        for path in ('/api/memory/profile/save', '/api/memory/edit'):
            with self.subTest(path=path):
                descriptor = find_route('POST', path)
                current = {'revision': 'new-version', 'text': 'Current user fact'}
                status, body = descriptor.error_response(
                    MemoryRevisionConflict('memory_profile_revision_conflict', current)
                )
                self.assertEqual(status, 409)
                self.assertFalse(body['ok'])
                self.assertEqual(body['current'], current)
                self.assertEqual(body['code'], 'memory_profile_revision_conflict')
                self.assertEqual(descriptor.error_response(ValueError('invalid request'))[0], 400)
                with self.assertRaises(RuntimeError):
                    descriptor.error_response(RuntimeError('internal failure'))

    def test_creation_cannot_smuggle_engine_or_unrestricted_permissions(self) -> None:
        policy = default_route_policy()
        for field in ('runtimeEngine', 'executionMode', 'toolProfileVersion', 'metadata', 'assistantId'):
            with self.subTest(field=field), self.assertRaises(ControlApiError):
                policy.authorize(ControlRequest(
                    request_id='bad', path_id=ControlPathId.AGENT_PRIMARY_ENSURE.value,
                    body={field: 'full_trust'},
                ), ControlAccessContext.native())


if __name__ == '__main__':
    unittest.main()
