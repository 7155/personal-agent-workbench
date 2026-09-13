import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import { agentSnapshotFromResponse, applyAgentSnapshot, createAgentProjection } from '@/contracts/agent-reducer';
import { AgentBlocks } from '@/features/agent/timeline/BlockRenderer';
import { labConnectionKey, requestLabControl } from '../control-request';
import { projectError } from './api';
import { object } from './types';
import './lab-session-record.css';

/** Local read projection: never hydrate or mutate the Guide's live store. */
export function LabSessionRecord({ sessionId, turnId, onClose }: { sessionId: string; turnId?: string; onClose: () => void }) {
  const transport = useControlTransport(); const heading = useRef<HTMLHeadingElement>(null);
  const [rawOpen, setRawOpen] = useState(false);
  useEffect(() => {
    const trigger = document.activeElement;
    heading.current?.focus();
    return () => { if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true }); };
  }, [sessionId, turnId]);
  const query = useQuery({ queryKey: ['lab-session-record', labConnectionKey(transport), sessionId],
    queryFn: async ({ signal }) => {
      // The default route returns the public snapshot; only `view=recent` is an explicit alternative.
      const raw = object(await requestLabControl(transport, { pathId: 'agent.session.snapshot', params: { sessionId }, signal }));
      if (raw.schemaVersion !== 'rag-ime.agent-message-list.v1' || raw.ok !== true || raw.sessionId !== sessionId || !Array.isArray(raw.items)) throw new Error('原 Session 的读取身份不匹配，未替换为其他对话。');
      return raw;
    }, retry: false, refetchOnWindowFocus: false,
    refetchInterval: (query) => ['active', 'busy'].includes(String(query.state.data?.status)) ? 3000 : false });
  const snapshot = useMemo(() => query.data ? agentSnapshotFromResponse(query.data) : undefined, [query.data]);
  const projection = useMemo(() => snapshot ? applyAgentSnapshot(createAgentProjection(sessionId), snapshot) : undefined, [sessionId, snapshot]);
  const messages = projection?.messageOrder.map((id) => projection.messagesById[id]!).filter((message) => !turnId || message.turnId === turnId) ?? [];
  const activities = projection?.activityOrder.map((id) => projection.activitiesById[id]!).filter((activity) => !turnId || activity.turnId === turnId) ?? [];
  const missingTurn = Boolean(turnId && projection && !projection.turnsById[turnId]);
  const sourceRows = (rows: unknown[]) => rows.filter((raw) => { const row = object(raw); return row.sessionId === sessionId && (!turnId || row.turnId === turnId); });
  const roleNames: Record<string, string> = { user: '用户', assistant: 'Agent', system: '系统', tool: '工具' };
  return <aside className="lab-session-record" aria-label="原 Session 记录阅读" onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } }}>
    <header><div><h3 tabIndex={-1} ref={heading}>{turnId ? '原回合记录' : '原 Session 记录'}</h3><p>Session {sessionId}{turnId ? ` · Turn ${turnId}` : ''}</p></div><div><Button size="small" disabled={query.isFetching} onClick={() => void query.refetch()}>刷新原记录</Button><Button size="small" onClick={onClose}>收起 Session 记录</Button></div></header>
    <p>只读来源；项目对话和当前运行保持原身份。</p>
    {query.isPending ? <p role="status">正在读取原 Session…</p> : query.isError ? <p role="alert">{projectError(query.error)}</p> : <>
      {snapshot?.partial || snapshot?.snapshotScope === 'recent' ? <p role="status">执行器本次仅返回部分记录，不能视为完整历史。</p> : null}
      {projection?.diagnostics.length ? <p role="status">部分返回记录无法解析，以下仅展示可核对的消息与工具记录。</p> : null}
      {missingTurn ? <p role="alert">指定回合未出现在原 Session 的返回记录中，未跳转到其他回合。</p> : !messages.length && !activities.length ? <p>原 Session 暂未返回可展示的消息或工具记录。</p> : <div className="lab-session-record__body">
        <section aria-label="原始消息"><h4>消息 · {messages.length}</h4>{messages.map((message) => <article key={message.id}><header><strong>{roleNames[message.role] ?? message.role}</strong><small>{message.turnId} · {message.status}</small></header><AgentBlocks blocks={message.blocks} sessionId={sessionId} allowTraceDiagnosticReceipt={false} /></article>)}</section>
        {activities.length ? <section aria-label="原始工具与运行记录"><h4>工具与运行记录 · {activities.length}</h4>{activities.map((activity) => <details key={activity.id}><summary>{activity.summary || activity.kind} · {activity.status}</summary><pre>{JSON.stringify(activity.payload, null, 2)}</pre><small>{activity.turnId} · {activity.id}</small></details>)}</section> : null}
        <details onToggle={(event) => setRawOpen(event.currentTarget.open)}><summary>查看本次返回的完整公开消息与事件</summary>{rawOpen && snapshot ? <pre>{JSON.stringify({ sessionId, ...(turnId ? { turnId } : {}), items: sourceRows(snapshot.messages), liveEvents: sourceRows(snapshot.liveEvents) }, null, 2)}</pre> : null}</details>
      </div>}
    </>}
  </aside>;
}
