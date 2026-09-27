"""Public Root policy persistence and compatibility across stored generations."""
import json

from tests.test_jev_host_application import JevHostFixture


class JevPolicyAdmissionTests(JevHostFixture):
    def test_new_direct_root_persists_auto_verification(self):
        created = self.create()
        stored = self.app.lifecycle.policy(created['graphId'])
        self.assertEqual(json.loads(stored['policy_json'])['verificationMode'], 'auto')
        self.assertEqual(self.app.projection(self.room['id'], created['graphId'])['policy']['verificationMode'], 'auto')

    def test_explicit_independent_and_existing_roots_remain_independent(self):
        created = self.app.create(self.room['id'], {
            'clientMessageId': 'independent-policy', 'message': '由其他伙伴核验成果',
            'strategy': 'direct', 'modelRouting': 'participant', 'verificationMode': 'independent',
        })
        self.assertEqual(json.loads(self.app.lifecycle.policy(created['graphId'])['policy_json'])['verificationMode'], 'independent')
        with self.app.ledger.connection(write=True) as conn:
            conn.execute('UPDATE agent_jev_host_roots SET policy_json=? WHERE graph_id=?',
                         (json.dumps({'modelRouting': 'balanced', 'toolApprovalMode': 'dispatch'}), created['graphId']))
        policy = self.app.projection(self.room['id'], created['graphId'])['policy']
        self.assertEqual(policy['verificationMode'], 'independent')
        self.assertEqual(policy['modelRouting'], 'balanced')
