"""Repository integration tests. Run after merging into the complete PAW checkout.

These use real PAW stores via the test setup in the user's integration log.
They are NOT counted as passed by the standalone delivery tests.
"""
from unittest.mock import patch
from tests import test_space_organization as base
from rag_ime.space_organization import OrganizationConflict


class SpaceOrganizationV21Tests(base.SpaceOrganizationTests):
    def test_noop_does_not_create_a_revision(self):
        response = self.command('category', 'unknown')
        self.assertTrue(response['noChange'])
        self.assertEqual(self.read()['items'][0]['revision'], 0)
        self.assertEqual(self.read()['receipts'], [])

    def test_manual_edit_during_jev_response_prevents_a_new_proposal(self):
        def evaluate(*args, **kwargs):
            self.command('category', 'reference')
            return self.answer()
        with patch('rag_ime.jev.api_key', return_value='offline-only'), \
             patch('rag_ime.jev.evaluate', side_effect=evaluate), \
             self.assertRaises(OrganizationConflict):
            self.service.suggest({'spaceKey': self.key})

    def test_expiry_equal_to_now_is_expired(self):
        proposal = self.suggest()['proposal']
        self.now += 300
        with self.assertRaises(OrganizationConflict):
            self.command('proposal', proposal['id'])

    def test_bad_unhashable_placement_is_a_validation_error(self):
        with self.assertRaises(ValueError):
            self.command('placement', [])

    def test_replay_after_undo_does_not_reapply(self):
        self.command()
        self.service.undo({'receiptId': 'c1'})
        replay = self.command()
        self.assertTrue(replay['undone'])
        self.assertEqual(self.read()['items'][0]['placement'], 'desk')

    def test_old_receipts_do_not_hide_another_spaces_current_undo(self):
        other = self.sessions.create(title='仍有有效撤销的工作')
        other_key = 'session:' + other['id']
        self.service.command({'spaceKey': other_key, 'commandId': 'older-valid',
            'expectedRevision': 0, 'operation': 'category', 'value': 'reference'})
        for index in range(35):
            self.now += 1
            self.command('group', f'分组 {index}', index, f'newer-{index}')
        result = self.service.read({'keys': [self.key, other_key]})
        self.assertEqual({r['id'] for r in result['receipts']}, {'older-valid', 'newer-34'})

    def test_provider_response_keeps_required_model_identity(self):
        for model in (None, '', []):
            response = self.answer()
            response['model'] = model
            with self.subTest(model=model), self.assertRaises(RuntimeError):
                self.suggest(response)
