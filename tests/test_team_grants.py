from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from rag_ime.team.errors import TeamError
from rag_ime.team.identity import TeamIdentityStore
from rag_ime.team.grants import TeamGrantStore


class TeamGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.identity = TeamIdentityStore(root / 'team.sqlite')
        self.admin = self.identity.bootstrap_admin('admin', 'administrator-password')
        self.alice = self.identity.create_member(self.admin['id'], 'alice', 'alice-password-123')
        self.bob = self.identity.create_member(self.admin['id'], 'bob', 'bob-password-123')
        self.project = self.identity.create_project(self.admin['id'], 'Shared site')
        for user in (self.alice, self.bob):
            self.identity.add_project_member(self.admin['id'], self.project['id'], user['id'])
        self.now = 1_000_000
        self.grants = TeamGrantStore(root / 'team.sqlite', self.identity, now_ms=lambda: self.now)
        self.workspace = root / 'workspace'
        self.workspace.mkdir()
        self.grants.bind_session(self.alice['id'], self.project['id'], 'alice-session', self.workspace)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_capability_cannot_change_session_or_space_and_is_not_stored_plaintext(self) -> None:
        attempt = self.grants.issue_attempt('alice-session')
        result = self.grants.resolve_token(attempt['token'], 'alice-session', self.project['id'])
        self.assertEqual(result['ownerUserId'], self.alice['id'])
        for session, space in (('bob-session', self.project['id']), ('alice-session', 'other-space')):
            with self.subTest(session=session, space=space), self.assertRaises(TeamError):
                self.grants.resolve_token(attempt['token'], session, space)
        self.assertNotIn(attempt['token'].encode(), (Path(self.tmp.name) / 'team.sqlite').read_bytes())

    def test_new_attempt_fences_old_token_and_expiry_stops_new_admission(self) -> None:
        first = self.grants.issue_attempt('alice-session')
        second = self.grants.issue_attempt('alice-session', ttl_ms=1000)
        with self.assertRaises(TeamError):
            self.grants.resolve_token(first['token'], 'alice-session', self.project['id'])
        self.now += 1001
        with self.assertRaises(TeamError):
            self.grants.resolve_token(second['token'], 'alice-session', self.project['id'])

    def test_removal_and_rejoin_do_not_restore_old_authority(self) -> None:
        attempt = self.grants.issue_attempt('alice-session')
        self.identity.remove_project_member(self.admin['id'], self.project['id'], self.alice['id'])
        with self.assertRaises(TeamError):
            self.grants.resolve_token(attempt['token'], 'alice-session', self.project['id'])
        self.identity.add_project_member(self.admin['id'], self.project['id'], self.alice['id'])
        with self.assertRaises(TeamError):
            self.grants.issue_attempt('alice-session')

    def test_other_members_cannot_drive_or_rebind_a_private_session(self) -> None:
        with self.assertRaises(TeamError):
            self.grants.require_session(self.bob['id'], self.project['id'], 'alice-session', action='write')
        with self.assertRaises(TeamError):
            self.grants.bind_session(self.bob['id'], self.project['id'], 'alice-session', self.workspace)


if __name__ == '__main__':
    unittest.main()
