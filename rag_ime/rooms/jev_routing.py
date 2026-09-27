"""Opt-in Jev routing at the Room ingress; never replaces explicit ownership."""
from __future__ import annotations

from rag_ime.jev_decisions import choices, question
from rag_ime.space_context import digest


def routing_facts(room):
    return {'id': room['id'], 'status': room['status'], 'policy': room['routingPolicy'],
            'participants': [{k: p.get(k) for k in ('id', 'status', 'displayName', 'collaborationRole', 'sessionId')}
                             for p in room['participants']], 'moderator': room.get('moderatorParticipantId')}


def route_with_jev(rooms, room, message, baseline):
    if room.get('routingPolicy') != 'jev' or len(baseline) != 1 or baseline[0].get('reason') in {'explicit_invite', 'mention', 'work_item_owner'}:
        return baseline
    facts = routing_facts(room)
    available = {p['id']: p for p in facts['participants'] if p['status'] == 'active'}
    criteria = {'unknown': '信息不足，保留原主控接收此消息'}
    criteria.update({key: f"{p['displayName']}；当前责任 {p['collaborationRole']}" for key, p in available.items()})
    try:
        result = choices({'message': message, 'room': facts}, {'route': question(
            '选择唯一最适合负责此消息的已有伙伴。需要协调、拆任务或综合多个结果时选择 coordinator；只按已声明责任匹配，不推测能力或创建新伙伴。', criteria)})
        answer = result['answers']['route']
    except (RuntimeError, ValueError, OSError):
        return [{**baseline[0], 'jev': {'status': 'unavailable', 'message': 'Jev 暂时不可用，保留原主控路由。'}}]
    if digest(routing_facts(rooms.get(room['id']))) != digest(facts):
        raise ValueError('Jev 路由期间 Room 或伙伴已变化，请重新发送。')
    receipt = {'model': result['model'], 'choice': answer['choice'], 'confidence': answer['confidence'],
               'sourceRevision': digest(facts), 'status': 'abstained' if answer['abstained'] else 'selected'}
    if answer['abstained']:
        return [{**baseline[0], 'jev': receipt}]
    decisions = rooms.plan_routes(room['id'], message, requested_participant_ids=[answer['choice']], conversation_only=True)
    return [{**decisions[0], 'reason': 'jev', 'jev': receipt}]
