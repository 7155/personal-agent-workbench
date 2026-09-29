import unittest
from unittest.mock import patch
from rag_ime.jev_decisions import choices, question
from rag_ime.rooms.jev_routing import route_with_jev
from rag_ime.rooms.routing import plan_room_routes


def reply(questions, selected='a', confidence=.9):
    return {'model': 'test-fixture', 'answers': {key: {'type': 'choice', 'choice': selected,
        'confidence': confidence, 'probabilities': {c: float(c == selected) for c in q['criteria']}}
        for key, q in questions.items()}}


class JevDecisionsTests(unittest.TestCase):
    def test_rejects_malformed_probability_and_unknown_candidate(self):
        qs={'x': question('判断', {'a': '甲', 'unknown': '未知'})}
        for value in [float('nan'), True, -.1, 2]:
            payload=reply(qs);payload['answers']['x']['confidence']=value
            with self.subTest(value=value), patch('rag_ime.jev.api_key', return_value='fixture'), patch('rag_ime.jev.evaluate', return_value=payload):
                with self.assertRaises(RuntimeError): choices({},qs)
        payload=reply(qs,'outside')
        with patch('rag_ime.jev.api_key', return_value='fixture'), patch('rag_ime.jev.evaluate', return_value=payload):
            with self.assertRaises(RuntimeError): choices({},qs)

    def test_low_confidence_is_explicit_abstention(self):
        qs={'x': question('判断', {'a': '甲', 'unknown': '未知'})}
        with patch('rag_ime.jev.api_key', return_value='fixture'), patch('rag_ime.jev.evaluate', return_value=reply(qs,confidence=.6)):
            self.assertTrue(choices({},qs)['answers']['x']['abstained'])

    def test_utf8_budget_precedes_network(self):
        with patch('rag_ime.jev.evaluate') as evaluate:
            with self.assertRaises(ValueError): choices({'text':'汉'*16000},{'x':question('判断',{'a':'甲'})})
            evaluate.assert_not_called()


class JevRoomRoutingTests(unittest.TestCase):
    def setUp(self):
        self.room={'id':'r','status':'active','roomKind':'collaboration','routingPolicy':'jev','moderatorParticipantId':'a',
                   'participants':[{'id':p,'sessionId':'s'+p,'status':'active','displayName':p,
                                    'ordinal':i,'collaborationRole':'coordinator' if i==0 else 'reviewer'} for i,p in enumerate(['a','b'])]}
        outer=self
        class Rooms:
            def get(self, identity):return outer.room
            def plan_routes(self, identity, message, **kwargs):return plan_room_routes(outer.room,message,**kwargs)
        self.rooms=Rooms()

    def test_real_router_target_and_receipt_follow_choice(self):
        baseline=plan_room_routes(self.room,'审查结果',conversation_only=True)
        def evaluate(state, questions, **kwargs):return reply(questions,'b')
        with patch('rag_ime.jev.api_key',return_value='fixture'),patch('rag_ime.jev.evaluate',side_effect=evaluate):
            result=route_with_jev(self.rooms,self.room,'审查结果',baseline)
        self.assertEqual(result[0]['targetParticipantId'],'b');self.assertEqual(result[0]['reason'],'jev')
        self.assertEqual(result[0]['jev']['status'],'selected')

    def test_explicit_owner_or_invite_never_calls_model(self):
        for kwargs in [{'authoritative_participant_id':'a'},{'requested_participant_ids':['b']}]:
            baseline=plan_room_routes(self.room,'继续',conversation_only=True,**kwargs)
            with patch('rag_ime.jev.evaluate') as evaluate:
                self.assertEqual(route_with_jev(self.rooms,self.room,'继续',baseline),baseline);evaluate.assert_not_called()

    def test_room_change_during_judgment_rejects_stale_route(self):
        baseline=plan_room_routes(self.room,'审查',conversation_only=True)
        def evaluate(state, questions, **kwargs):
            self.room['participants'][1]['status']='removed'
            return reply(questions,'b')
        with patch('rag_ime.jev.api_key',return_value='fixture'),patch('rag_ime.jev.evaluate',side_effect=evaluate):
            with self.assertRaisesRegex(ValueError,'已变化'):route_with_jev(self.rooms,self.room,'审查',baseline)

    def test_provider_failure_retains_a_visible_fallback(self):
        baseline=plan_room_routes(self.room,'继续',conversation_only=True)
        with patch('rag_ime.jev.api_key',return_value='fixture'),patch('rag_ime.jev.evaluate',side_effect=RuntimeError('offline')):
            result=route_with_jev(self.rooms,self.room,'继续',baseline)
        self.assertEqual(result[0]['targetParticipantId'],'a');self.assertEqual(result[0]['jev']['status'],'unavailable')
