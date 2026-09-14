from __future__ import annotations

import unittest

from rag_ime.control_api.errors import ControlApiError
from rag_ime.control_api.models import ControlAccessContext, ControlRequest
from rag_ime.control_api.route_policy import default_route_policy


class TeamControlPolicyTests(unittest.TestCase):
    def test_team_file_route_requires_explicit_identity_and_route_authority(self) -> None:
        policy = default_route_policy()
        request = ControlRequest('r', 'files.list')
        context = ControlAccessContext.team(
            user_id='alice', space_id='personal-alice', allowed_paths={'files.list'},
        )
        self.assertEqual(policy.authorize(request, context).path_id.value, 'files.list')
        self.assertTrue(context.is_remote)
        self.assertTrue(context.remote_authenticated)
        with self.assertRaises(ControlApiError):
            policy.authorize(ControlRequest('r', 'agent.providers.get'), context)
        with self.assertRaises(ControlApiError):
            policy.authorize(request, ControlAccessContext.remote(device_id='alice', scopes={'control.read'}))

    def test_incomplete_team_context_cannot_become_a_local_owner(self) -> None:
        for user, space in (('', 'p'), ('alice', '')):
            with self.subTest(user=user, space=space), self.assertRaises(ValueError):
                ControlAccessContext.team(user_id=user, space_id=space, allowed_paths={'files.list'})

    def test_team_routes_keep_the_canonical_request_contract(self) -> None:
        context = ControlAccessContext.team(
            user_id='alice', space_id='project', allowed_paths={'agent.sessions.create'},
        )
        with self.assertRaises(ControlApiError):
            default_route_policy().authorize(
                ControlRequest('r', 'agent.sessions.create', body={'title': 'test', 'ownerUserId': 'bob'}), context,
            )

    def test_http_resolution_preserves_the_authenticated_resource_parameters(self) -> None:
        requests = default_route_policy().resolve_http_requests(
            method='GET', path='/api/agent/sessions/session-123/messages', query={}, body={},
        )
        self.assertEqual(requests[0].params['sessionId'], 'session-123')
        self.assertEqual(requests[0].path_id, 'agent.session.snapshot')
        with self.assertRaises(ControlApiError):
            default_route_policy().resolve_http_requests(method='GET', path='/api/unknown', query={}, body={})


if __name__ == '__main__':
    unittest.main()
