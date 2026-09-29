"""Bounded projections from existing owners, with no execution or completion authority."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .agent_tool_artifacts import managed_file_block
from collections.abc import Mapping
from typing import Any

from .space_organization_validation import canonical


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def message_text(message: Mapping[str, Any]) -> str:
    blocks = message.get('blocks')
    if isinstance(blocks, list):
        return '\n'.join(str(b.get('data', {}).get('text') or b.get('summary') or '')
                         for b in blocks if isinstance(b, Mapping) and b.get('type') == 'text')
    return str(message.get('text') or message.get('message') or '')


def source(kind: str, identity: str, value: Any, label: str) -> dict[str, Any]:
    return {'kind': kind, 'id': identity, 'revision': digest(value), 'label': label}


def session_context(agent: Any, identity: str) -> dict[str, Any]:
    workflow = agent.workflow_state(identity)
    goal, todo = workflow['goal'], workflow['todo']
    # This owner projects a bounded durable window without starting/restoring Pi.
    recent = agent.runtime.recent_session_snapshot(identity)
    messages = [m for m in recent.get('messages', []) if isinstance(m, Mapping)]
    refs = [source('goal', identity, goal, '当前目标'), source('todo', identity, todo, '当前计划')]
    requests = []
    deliveries = []
    for m in messages:
        ref = source('message', str(m.get('id') or ''), m, '用户原话' if m.get('role') == 'user' else '成果来源消息')
        if m.get('role') == 'user':
            requests.append({'text': message_text(m), 'source': ref, 'createdAtMs': m.get('createdAtMs'), 'clientMessageId': m.get('clientMessageId')})
        if m.get('role') == 'assistant' and m.get('status') == 'completed':
            for b in m.get('blocks', []):
                if isinstance(b, Mapping) and b.get('type') in {'file', 'artifact'}:
                    data = b.get('data') or {}
                    if isinstance(data, Mapping) and (data.get('mediaId') or data.get('path')):
                        deliveries.append({'id': str(b.get('id')), 'title': str(data.get('fileName') or data.get('name') or '成果文件'),
                            'source': ref, 'data': dict(data), 'generated': True, 'verified': 'unknown', 'adopted': 'unknown'})
    # These are file receipts minted after verified workspace mutations, not
    # arbitrary Tool text, uploads, or a model claim of completion.
    for receipt in reversed(agent.media.list_for_session(identity, limit=40)):
        if (receipt.get('origin') == 'tool_result' and receipt.get('originTool') in {'workspace_write', 'workspace_edit', 'workspace_patch'}
                and receipt.get('mimeType') not in {'text/x-diff', 'text/x-patch'}
                and not any(d['data'].get('mediaId') == receipt['mediaId'] for d in deliveries)):
            deliveries.append({'id': receipt['mediaId'], 'title': receipt['fileName'],
                'source': source('media', receipt['mediaId'], receipt, '工作区写入回执'),
                'data': managed_file_block(receipt)['data'], 'generated': True, 'verified': 'unknown', 'adopted': 'unknown'})
    candidates = []
    blockers = []
    for phase in todo.get('phases', []):
        for index, task in enumerate(phase.get('tasks', [])):
            if task.get('status') in {'pending', 'in_progress'}:
                candidates.append({'id': f'task-{len(candidates)}', 'text': str(task['content']),
                                   'source': refs[1], 'workItemId': ''})
            elif task.get('status') == 'blocked':
                blockers.append(str(task.get('reason') or task.get('content') or '工作受阻'))
    if not candidates and goal.get('configured') and goal.get('status') == 'active':
        candidates.append({'id': 'goal', 'text': str(goal.get('objective') or ''), 'source': refs[0], 'workItemId': ''})
    if not candidates and not goal.get('configured') and requests:
        candidates.append({'id': 'latest-request', 'text': requests[-1]['text'], 'source': requests[-1]['source'], 'workItemId': ''})
    pending_provider = getattr(agent.runtime, 'pending_ui_requests', None)
    pending = pending_provider(identity) if callable(pending_provider) else None
    decisions = [{'id': str(p.get('requestId')), 'text': str(p.get('title') or p.get('message') or '有一个问题需要你回答'),
                  'source': source('user_input', str(p.get('requestId')), p, '待回答的问题')}
                 for p in (pending or []) if isinstance(p, Mapping) and p.get('requestId')]
    last_reply = next(({'text': message_text(m), 'source': source('message', str(m.get('id') or ''), m, '最近回复')} for m in reversed(messages) if m.get('role') == 'assistant' and message_text(m)), None)
    return {'lastReply': last_reply, 'goal': goal, 'todo': todo, 'requests': requests[-12:], 'candidates': candidates[:12],
            'blockers': blockers, 'deliveries': deliveries[-12:], 'pendingDecisions': decisions,
            'recentMessages': [{'role': m.get('role'), 'text': message_text(m), 'source': source('message', str(m.get('id') or ''), m, '最近对话片段')}
                               for m in messages[-12:] if m.get('role') in {'user', 'assistant'}],
            'sources': refs, 'missing': ['只读取最近对话窗口；更早要求仍由原 Pi 上下文管理，未在此完整核实。']
                + ([] if pending is not None else ['尚未读取待用户处理事项。'])
                + ([] if recent.get('projectionCurrent') is True else ['最近消息尚未同步，刷新后再判断下一步。']),
            'control': {'goalRevision': goal.get('revision'), 'updatedAtMs': goal.get('updatedAtMs'), 'objective': goal.get('objective'), 'successCriteria': goal.get('successCriteria'),
                        'evidenceExpectations': goal.get('evidenceExpectations')},
            'executionAllowed': not goal.get('budgetExceeded') and recent.get('projectionCurrent') is True and pending is not None and bool(not goal.get('configured') or goal.get('status') == 'active')}


def room_context(agent: Any, identity: str) -> dict[str, Any]:
    snapshot = agent.rooms.snapshot(identity)
    room = snapshot['room']
    work = agent.room_work.list(room_id=identity, limit=200)
    refs = [source('room', identity, room, '协作空间'), source('work_items', identity, work, '责任与验收记录')]
    events = snapshot.get('events', [])
    requests = []
    pending = []
    candidates = []
    blockers = []
    deliveries = []
    for event in events:
        payload = event.get('payload') or {}
        if not isinstance(payload, Mapping):
            continue
        post = payload.get('post') or {}
        if event.get('eventType') == 'room_post' and isinstance(post, Mapping):
            if (post.get('kind') == 'wait' and post.get('visibility') == 'room'
                    and (post.get('publicationSource') or {}).get('kind') == 'room_commit'
                    and isinstance(post.get('question'), Mapping) and post.get('postId') and post['question'].get('prompt')
                    and post.get('rootId') == event.get('turnId') and post.get('roomId') == identity):
                pending = [{'id': post['postId'], 'rootId': post['rootId'], 'text': post['question']['prompt'],
                    'source': source('room_post', post['postId'], post, 'Room 待回答问题')}]
            elif (pending and post.get('rootId') == pending[0]['rootId']
                    and (post.get('publicationSource') or {}).get('kind') == 'user'
                    and str(post.get('authorActorRef') or '').startswith('user:')):
                pending = []
        if pending and event.get('turnId') == pending[0]['rootId']:
            if event.get('eventType') in {'turn_completed', 'turn_failed'} or payload.get('answerToPostId') == pending[0]['id']:
                pending = []
        if event.get('eventType') == 'participant_message':
            public_data = payload.get('data') or {}
            message = public_data.get('message') if isinstance(public_data, Mapping) else None
            if isinstance(message, Mapping) and message.get('role') == 'assistant' and message.get('status') == 'completed':
                # A public file reference is not a registered artifact or a
                # verification verdict. Only resolve within this Room's roots.
                for path in re.findall(r'`(/[^`\n]{1,4096})`', message_text(message))[:12]:
                    target = Path(path).resolve(strict=False)
                    if target.suffix.lower() not in {'.md', '.txt', '.html', '.pdf', '.csv', '.json', '.docx', '.xlsx', '.png', '.jpg'}:
                        continue
                    if not any(target.is_relative_to(Path(root).resolve(strict=False)) for root in room.get('workspaceRoots', [])):
                        continue
                    if not any(d.get('data', {}).get('path') == str(target) for d in deliveries):
                        deliveries.append({'id': 'public-file:' + digest(str(target)), 'title': target.name,
                            'source': source('room_event', str(event.get('eventId') or event.get('sequence')), event, 'Room 公开回复中的文件引用'),
                            'data': {'path': str(target), 'sessionId': message.get('sessionId') if any(p.get('sessionId') == message.get('sessionId') for p in room.get('participants', [])) else ''}, 'generated': False, 'verified': 'unknown', 'adopted': 'unknown'})
        if event.get('eventType') == 'user_message':
            requests.append({'text': str(payload.get('text') or payload.get('message') or ''),
                'source': source('room_event', str(event.get('eventId') or event.get('sequence')), event, 'Room 用户原话'),
                'createdAtMs': event.get('createdAtMs'), 'clientMessageId': payload.get('clientMessageId')})
    for item in work:
        ref = source('work_item', item['id'], item, '工作项：' + item['objective'])
        if item['state'] in {'queued', 'active'}:
            candidates.append({'id': item['id'], 'text': item['objective'], 'workItemId': item['id'], 'source': ref})
        if item['state'] in {'blocked', 'failed'}:
            blockers.append(str(item.get('blocker', {}).get('reason') or item.get('resultSummary') or item['objective']))
        if item['state'] in {'review', 'done'} and item.get('artifactRefs'):
            review = item.get('review') or {}
            for artifact in item['artifactRefs']:
                deliveries.append({'id': artifact, 'title': artifact, 'source': ref, 'data': {'artifactRef': artifact},
                    'generated': True, 'verified': 'recorded' if review.get('evidenceRefs') else 'unknown',
                    'adopted': 'unknown', 'review': review})
    for artifact in room.get('artifacts', []):
        if artifact.get('status') != 'active':
            continue
        matched = next((d for d in deliveries if d['id'] in {artifact['id'], artifact['path']}), None)
        if matched:
            matched['data'] = {'path': artifact['path']}
            matched['title'] = artifact['displayName']
        else:
            deliveries.append({'id': artifact['id'], 'title': artifact['displayName'],
                'data': {'path': artifact['path']}, 'source': source('artifact', artifact['id'], artifact, 'Room 成果'),
                'generated': True, 'verified': 'unknown', 'adopted': 'unknown'})
    # Work blockers are not automatically user questions. Read unresolved public
    # question posts from the owning clarification projection when available.
    return {'goal': None, 'todo': None, 'requests': requests[-12:], 'candidates': candidates[:12],
        'blockers': blockers, 'deliveries': deliveries[-12:], 'pendingDecisions': pending, 'sources': refs,
        'missing': (['Room 历史窗口存在截断；窗口以外的决定与问题尚未核实。'] if snapshot.get('truncated') else []),
        'control': {'workItems': [{k: w.get(k) for k in ('id', 'objective', 'expectedOutput', 'acceptanceCriteria', 'state', 'revision', 'currentOwnerParticipantId')} for w in work]},
        'executionAllowed': room.get('status') == 'active', 'participants': room.get('participants', [])}


def delivery_versions(agent: Any, kind: str, identity: str, context: dict[str, Any]) -> None:
    """Recheck deliverable references through their existing scoped owners.

    A readable current file is not proof that a past review tested these bytes.
    File identity participates in the proposal version to catch later changes.
    """
    from .rooms.store import _authorized_artifact_path
    roots = agent.rooms.get(identity).get('workspaceRoots', []) if kind == 'room' else []
    for delivery in context['deliveries']:
        data = delivery['data']
        try:
            if kind == 'session' and data.get('mediaId'):
                receipt, _ = agent.media.read(str(data['mediaId']), session_id=identity)
                current_hash = receipt['sha256']
                delivery['availability'] = 'changed' if data.get('sha256') and data['sha256'] != current_hash else 'available'
                delivery['fileRevision'] = current_hash
            elif kind == 'room' and data.get('path'):
                path = _authorized_artifact_path(data['path'], roots)
                stat = path.stat()
                delivery['availability'] = 'available'
                delivery['fileRevision'] = digest([str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])
            else:
                delivery['availability'] = 'unknown'
        except (KeyError, ValueError, OSError):
            delivery['availability'] = 'unavailable'
        if delivery['availability'] in {'changed', 'unavailable'}:
            context['missing'].insert(0, '成果已变化或无法访问：' + delivery['title'] + '；旧记录不能证明当前文件有效。')
