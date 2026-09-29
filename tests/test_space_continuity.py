import json
import unittest
from unittest.mock import patch

from tests import test_agent_service as base
from rag_ime.space_continuity import SpaceContinuity
from rag_ime.space_organization import SpaceOrganization, OrganizationConflict


class SpaceContinuityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.AgentServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.agent = self.fixture.service
        self.session = self.agent.create_session({'title': '已有项目', 'mode': 'coordinator', 'workspaceRoots': [str(self.fixture.root)]})['session']
        self.sid = self.session['id']
        self.key = 'session:' + self.sid
        self.messages = [self.message('u1', '继续实现实验报告')]
        self.recent = patch.object(self.agent.runtime, 'recent_session_snapshot', side_effect=lambda sid: {'messages': self.messages if sid == self.sid else [], 'projectionCurrent': True})
        self.recent.start(); self.addCleanup(self.recent.stop)
        runtime = patch.object(self.agent.runtime, 'runtime_status', return_value={'enabled': True, 'status': 'ready', 'activeSessionIds': []})
        runtime.start(); self.addCleanup(runtime.stop)
        self.org = SpaceOrganization(self.fixture.root/'rag-ime.sqlite', sessions=self.agent.sessions, rooms=self.agent.rooms)
        self.now = 1000
        self.service = SpaceContinuity(self.org.db_path, agent=self.agent, organization=self.org, clock=lambda: self.now)
        self.agent.sessions.mutate_agent_goal(self.sid, {'action': 'confirm_setup', 'confirmed': True, 'expectedRevision': 0,
            'objective': '完成实验报告', 'successCriteria': '不修改正式知识库，保留原文引用'})

    @staticmethod
    def message(identity, text, role='user'):
        return {'id': identity, 'role': role, 'status': 'completed', 'createdAtMs': 100,
                'blocks': [{'id': identity + '-text', 'type': 'text', 'data': {'text': text}}]}

    def suggest(self, key=None, choice='goal', effect=None):
        key = key or self.key
        facts = self.service.facts(key)
        def evaluate(state, questions, **kwargs):
            self.assertEqual(json.loads(state)['spaceKey'], key)
            if effect: effect()
            criteria = questions['next']['criteria']
            selected = choice if choice in criteria else next(k for k in criteria if k not in {'waiting', 'clarify'})
            return {'model': 'fixture-not-production', 'answers': {'next': {'type': 'choice', 'choice': selected,
                'confidence': .95, 'probabilities': {k: float(k == selected) for k in criteria}}}}
        with patch('rag_ime.jev.api_key', return_value='fixture'), patch('rag_ime.jev.evaluate', side_effect=evaluate):
            return self.service.suggest({'spaceKey': key, 'expectedRevision': facts['revision']})

    def intent(self, proposal, cid='resume-one'):
        return {'spaceKey': proposal['spaceKey'], 'proposalId': proposal['id'], 'commandId': cid}

    def test_jev_progress_uses_bound_context_and_rejects_changed_requirements(self):
        facts = self.service.facts(self.key)
        def evaluate(state, questions, **kwargs):
            self.assertEqual(json.loads(state)['spaceKey'], self.key)
            return {'model': 'fixture', 'answers': {name: {'type': 'choice', 'choice': 'unknown',
                'confidence': .9, 'probabilities': {c: float(c == 'unknown') for c in q['criteria']}}
                for name, q in questions.items()}}
        with patch('rag_ime.jev.api_key', return_value='fixture'), patch('rag_ime.jev.evaluate', side_effect=evaluate):
            result = self.service.analyze({'spaceKey': self.key, 'expectedRevision': facts['revision']})
        self.assertEqual(result['analysis']['revision'], facts['revision'])
        self.assertTrue(result['analysis']['answers']['stage']['abstained'])
        self.assertEqual(len(result['analysis']['tasks']), len(facts['candidates']))
        self.messages.append(self.message('u2', '先不要继续'))
        with patch('rag_ime.jev.evaluate') as call:
            with self.assertRaises(OrganizationConflict):
                self.service.analyze({'spaceKey': self.key, 'expectedRevision': facts['revision']})
            call.assert_not_called()

    def test_same_title_changed_requirement_changes_context_and_waits(self):
        before = self.service.facts(self.key)
        self.messages.append(self.message('u2', '先搁置，等新数据，暂时不要实现'))
        after = self.service.facts(self.key)
        self.assertEqual(before['title'], after['title'])
        self.assertNotEqual(before['revision'], after['revision'])
        self.assertIn('等新数据', after['contextPack']['recentUserRequirements'][-1]['text'])
        result = self.suggest(choice='waiting')
        self.assertIsNone(result['proposal'])
        self.assertIn('等待', result['message'])

    def test_decisions_are_explicit_superseded_and_invalidated_by_new_requirements(self):
        facts = self.service.facts(self.key)
        first = {'spaceKey': self.key, 'expectedRevision': facts['revision'], 'id': 'd1', 'text': '只在副本验证\n保留引用', 'supersedesId': ''}
        self.service.decision(first)
        self.assertTrue(self.service.decision(first)['replayed'])
        facts = self.service.facts(self.key)
        self.assertEqual(facts['decisions'][0]['status'], 'current')
        self.service.decision({**first, 'id': 'd2', 'text': '改为只做设计', 'supersedesId': 'd1', 'expectedRevision': facts['revision']})
        facts = self.service.facts(self.key)
        self.assertEqual([d['status'] for d in facts['decisions']], ['superseded', 'current'])
        self.messages.append(self.message('u2', '现在可以实现了'))
        facts = self.service.facts(self.key)
        self.assertEqual(facts['decisions'][-1]['status'], 'needs_review')
        self.assertEqual(facts['contextPack']['acceptedDecisions'], [])

    def test_provider_late_result_does_not_survive_requirement_change(self):
        with self.assertRaises(OrganizationConflict):
            self.suggest(effect=lambda: self.messages.append(self.message('u2', '不要继续了')))

    def test_open_read_has_no_runtime_prompt_or_jev_side_effect(self):
        with patch.object(self.agent, 'prompt') as prompt, patch('rag_ime.jev.evaluate') as model:
            result = self.service.read({'keys': [self.key]})
        self.assertEqual(len(result['items']), 1)
        prompt.assert_not_called(); model.assert_not_called()

    def test_cancelled_goal_and_expired_proposal_do_not_execute(self):
        proposal = self.suggest()['proposal']
        goal = self.agent.sessions.agent_goal(self.sid)
        self.agent.sessions.mutate_agent_goal(self.sid, {'action': 'cancel', 'expectedRevision': goal['revision'], 'reason': '用户取消'})
        with patch.object(self.agent, 'prompt') as prompt, self.assertRaises(OrganizationConflict):
            self.service.resume(self.intent(proposal))
        prompt.assert_not_called()
        self.assertFalse(self.service.facts(self.key)['executionAllowed'])

    def test_existing_session_owner_receipt_recovers_lost_response_without_reexecution(self):
        proposal = self.suggest()['proposal']
        intent = self.intent(proposal)
        calls = []
        def dispatch(**kwargs):
            calls.append(kwargs)
            return {'ok': True, 'accepted': True, 'turnId': 'real-owner-fixture-turn', 'clientMessageId': kwargs['client_message_id']}
        with patch.object(self.agent.prompt_application, 'dispatch_checkpoint', side_effect=dispatch):
            first = self.service.resume(intent)
            self.messages.append(self.message('u-after', '接下来换一个方向'))
            replay = self.service.resume(intent)
        self.assertTrue(first['accepted']); self.assertTrue(replay['replayed'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(first['receipt']['turnId'], replay['receipt']['turnId'])
        self.assertIn('不修改正式知识库', calls[0]['message'])

    def test_revision_and_permission_change_reject_old_action(self):
        proposal = self.suggest()['proposal']
        self.messages.append(self.message('u2', '只检查，不写入'))
        with patch.object(self.agent, 'prompt') as prompt, self.assertRaises(OrganizationConflict):
            self.service.resume(self.intent(proposal))
        prompt.assert_not_called()

    def test_room_uses_existing_message_owner_and_real_work_item(self):
        room = self.agent.create_room({'title': '已有实验协作', 'workspaceRoots': [str(self.fixture.root)], 'participants': [
            {'roleId': 'companion-present-v1', 'roleVersion': '1'}, {'roleId': 'companion-firstlight-v1', 'roleVersion': '1'}]})['room']
        work = self.agent.room_work.create(room_id=room['id'], objective='生成实验结果', expected_output='带来源的报告',
            acceptance_criteria=['报告包含来源'], current_owner_participant_id=room['participants'][0]['id'], created_by_participant_id=room['participants'][0]['id'], client_message_id='existing-work')
        key = 'room:' + room['id']
        proposal = self.suggest(key, choice=work['id'])['proposal']
        with patch.object(self.agent, '_post_room_message_once', return_value={'ok': True, 'accepted': True, 'rootId': 'owner-root'}) as owner:
            first = self.service.resume(self.intent(proposal))
            second = self.service.resume(self.intent(proposal))
        owner.assert_called_once()
        self.assertEqual(owner.call_args.kwargs['work_item_id'], work['id'])
        self.assertEqual(first['receipt']['rootId'], 'owner-root')
        self.assertTrue(second['replayed'])

    def test_media_restoration_checks_owner_and_bytes(self):
        media = self.agent.import_media(session_id=self.sid, data=base.PNG_1X1, mime_type='image/png', file_name='source.png')['media']
        refs = [{'id': media['mediaId'], 'sha256': media['sha256']}]
        self.assertTrue(self.service.media({'spaceKey': self.key, 'attachments': refs})['items'][0]['available'])
        other = self.agent.create_session({'title': '不同项目'})['session']
        self.assertFalse(self.service.media({'spaceKey': 'session:' + other['id'], 'attachments': refs})['items'][0]['available'])

    def test_only_user_facing_file_blocks_are_deliveries(self):
        self.messages.append({'id': 'a1', 'role': 'assistant', 'status': 'completed', 'blocks': [
            {'id': 'tool1', 'type': 'tool_result', 'data': {'text': 'log'}},
            {'id': 'file1', 'type': 'file', 'data': {'mediaId': 'receipt-file', 'fileName': '报告.md'}}]})
        result = self.service.facts(self.key)
        self.assertEqual([d['id'] for d in result['deliveries']], ['file1'])
        self.assertEqual(result['deliveries'][0]['verified'], 'unknown')
        self.assertEqual(result['deliveries'][0]['adopted'], 'unknown')

    def test_open_idle_session_is_not_running(self):
        with patch.object(self.agent.runtime, 'runtime_status', return_value={'activeSessionIds': [], 'activeSessionId': self.sid}):
            self.assertFalse(self.service.facts(self.key)['running'])

    def test_stale_projection_cannot_propose_or_execute(self):
        proposal = self.suggest()['proposal']
        with patch.object(self.agent.runtime, 'recent_session_snapshot', return_value={'messages': self.messages, 'projectionCurrent': False}):
            self.assertFalse(self.service.facts(self.key)['executionAllowed'])
            with self.assertRaises(OrganizationConflict):
                self.service.resume(self.intent(proposal))

    def test_expiry_boundary_rejects_new_execution(self):
        proposal = self.suggest()['proposal']
        self.now = proposal['expiresAtMs'] / 1000
        with patch.object(self.agent, 'prompt') as prompt, self.assertRaises(OrganizationConflict):
            self.service.resume(self.intent(proposal))
        prompt.assert_not_called()

    def test_one_proposal_cannot_create_two_commands(self):
        proposal = self.suggest()['proposal']
        with patch.object(self.agent, 'prompt', return_value={'ok': True, 'accepted': True, 'turnId': 'one'}) as prompt:
            self.service.resume(self.intent(proposal, 'first'))
            with self.assertRaises(OrganizationConflict):
                self.service.resume(self.intent(proposal, 'second'))
        prompt.assert_called_once()

    def test_room_deliverable_change_invalidates_context_and_missing_file_is_visible(self):
        room = self.agent.create_room({'title': '报告', 'workspaceRoots': [str(self.fixture.root)], 'participants': [
            {'roleId': 'companion-present-v1', 'roleVersion': '1'}, {'roleId': 'companion-firstlight-v1', 'roleVersion': '1'}]})['room']
        path = self.fixture.root / 'report.md'; path.write_text('旧版本')
        self.agent.rooms.add_artifact(room['id'], path=str(path))
        key = 'room:' + room['id']; first = self.service.facts(key)
        self.assertEqual(first['deliveries'][0]['availability'], 'available')
        path.write_text('新内容，原验证未覆盖')
        second = self.service.facts(key)
        self.assertNotEqual(first['revision'], second['revision'])
        path.unlink(); third = self.service.facts(key)
        self.assertEqual(third['deliveries'][0]['availability'], 'unavailable')
        self.assertTrue(any('无法访问' in message for message in third['missing']))

    def test_failed_original_owner_receipt_can_resolve_uncertain_intent(self):
        proposal = self.suggest()['proposal']; intent = self.intent(proposal)
        with patch.object(self.agent, 'prompt', side_effect=RuntimeError('lost connection')):
            with self.assertRaises(RuntimeError): self.service.resume(intent)
        with patch.object(self.agent.command_receipts, 'failure_evidence_for_exact_command', return_value={'message': 'rejected', 'causeCode': 'GOAL_CANCELLED'}), patch.object(self.agent, 'prompt') as owner:
            result = self.service.resume(intent)
        self.assertFalse(result['accepted']); self.assertTrue(result['replayed']); owner.assert_not_called()

    def test_continuation_message_does_not_invalidate_adopted_decisions(self):
        facts = self.service.facts(self.key)
        self.service.decision({'spaceKey': self.key, 'expectedRevision': facts['revision'], 'id': 'd1', 'text': '仅改副本', 'supersedesId': ''})
        proposal = self.suggest()['proposal']; intent = self.intent(proposal)
        with patch.object(self.agent, 'prompt', return_value={'ok': True, 'accepted': True}):
            self.service.resume(intent)
        message = self.message('continued', '继续这一步')
        message['clientMessageId'] = intent['commandId']; self.messages.append(message)
        self.assertEqual(self.service.facts(self.key)['decisions'][0]['status'], 'current')

    def test_explicit_selection_uses_current_candidate_without_model_permission_gate(self):
        facts = self.service.facts(self.key)
        with patch('rag_ime.jev.evaluate') as model:
            result = self.service.suggest({'spaceKey': self.key, 'expectedRevision': facts['revision'], 'candidateId': 'goal'})
        model.assert_not_called()
        self.assertEqual(result['proposal']['origin'], 'user')
        with self.assertRaises(ValueError):
            self.service.suggest({'spaceKey': self.key, 'expectedRevision': facts['revision'], 'candidateId': 'invented'})
        self.messages.append(self.message('changed', '先只做设计'))
        with self.assertRaises(OrganizationConflict):
            self.service.resume(self.intent(result['proposal']))

    def test_generated_file_receipts_are_visible_but_uploads_and_diff_logs_are_not_deliveries(self):
        self.agent.media.import_bytes(session_id=self.sid, data=b'Input', mime_type='text/plain', file_name='source.txt')
        self.agent.media.import_bytes(session_id=self.sid, data=b'Report', mime_type='text/markdown', file_name='report.md', origin='tool_result', origin_tool='workspace_write', origin_receipt_id='actual-write')
        self.agent.media.import_bytes(session_id=self.sid, data=b'Diff', mime_type='text/x-diff', file_name='report.md.diff', origin='tool_result', origin_tool='workspace_write', origin_receipt_id='actual-write')
        files = self.service.facts(self.key)['deliveries']
        self.assertEqual([item['title'] for item in files], ['report.md'])
        self.assertEqual(files[0]['availability'], 'available')
        self.assertEqual(files[0]['verified'], 'unknown')

    def test_public_room_file_reference_preserves_scope_and_unknown_verification(self):
        room = self.agent.create_room({'title': '公开文件', 'workspaceRoots': [str(self.fixture.root)], 'participants': [
            {'roleId': 'companion-present-v1', 'roleVersion': '1'}, {'roleId': 'companion-firstlight-v1', 'roleVersion': '1'}]})['room']
        report = self.fixture.root / 'report.md'; report.write_text('Report')
        self.agent.rooms.append_event(room_id=room['id'], event_type='participant_message', payload={'data': {'message': self.message('out', f'文件：`{report}`；另一个引用 `/outside/private.md`', 'assistant')}})
        snapshot = self.agent.rooms.snapshot(room['id'])
        snapshot['room']['workspaceRoots'] = [str(self.fixture.root)]
        with patch.object(self.agent.rooms, 'snapshot', return_value=snapshot):
            files = self.service.facts('room:' + room['id'])['deliveries']
        self.assertEqual([item['title'] for item in files], ['report.md'])
        self.assertFalse(files[0]['generated'])
        self.assertEqual(files[0]['verified'], 'unknown')

    def test_editing_goal_constraints_invalidates_old_adoption_without_a_chat_message(self):
        facts = self.service.facts(self.key)
        self.service.decision({'spaceKey': self.key, 'expectedRevision': facts['revision'], 'id': 'd-goal', 'text': '可以开始实现', 'supersedesId': ''})
        goal = self.agent.sessions.agent_goal(self.sid)
        self.agent.sessions.mutate_agent_goal(self.sid, {'action': 'update', 'expectedRevision': goal['revision'], 'objective': '只保留材料，等待新数据'})
        after = self.service.facts(self.key)
        self.assertEqual(after['decisions'][0]['status'], 'needs_review')
        self.assertEqual(after['contextPack']['acceptedDecisions'], [])
