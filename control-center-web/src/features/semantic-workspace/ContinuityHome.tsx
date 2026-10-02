import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { ArrowRight, ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';
import { useControlTransport } from '@/app/control-transport';
import { AgentFileBlock } from '@/features/agent/file-preview/AgentFileBlock';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import type { ControlRequest } from '@/platform/transport';
import { observeRequest } from './organization-request';
import { recoveryScope } from './workspace-recovery';
import type { ResumeIntent, ResumeProposal, SpaceFacts } from './continuity-model';
import './continuity.css';
import { JevProgress } from './JevProgress';


function readIntent(key: string): ResumeIntent | null {
  try {
    const raw = key && localStorage.getItem(key);
    const value: unknown = raw ? JSON.parse(raw) : null;
    if (!value || typeof value !== 'object') return null;
    const record = value as Record<string, unknown>;
    if (typeof record.spaceKey !== 'string' || !/^(session|room):.+/.test(record.spaceKey)
      || typeof record.proposalId !== 'string' || typeof record.commandId !== 'string') return null;
    return { spaceKey: record.spaceKey, proposalId: record.proposalId, commandId: record.commandId };
  } catch { return null; }
}

export function ContinuityHome({ spaceKeys, onOpen, onOpenIntent }: {
  spaceKeys: string[]; onOpen: (key: string) => void; onOpenIntent?: (key: string) => void;
}) {
  const intentProps = (key: string) => ({
    onFocus: () => onOpenIntent?.(key),
    onPointerEnter: () => onOpenIntent?.(key),
    onPointerDown: () => onOpenIntent?.(key),
  });
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const request = useCallback(<Result,>(input: ControlRequest) => observeRequest<ControlRequest, Result>(value => transport.request<Result>(value), input), [transport]);
  const [page, setPage] = useState(0);
  const signature = JSON.stringify(spaceKeys.slice(page * 6, page * 6 + 6));
  const [items, setItems] = useState<SpaceFacts[]>([]);
  const [selected, setSelected] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [proposal, setProposal] = useState<ResumeProposal | null>(null);
  const [revision, setRevision] = useState(0);
  const journalKey = recoveryScope(transport, 'resume-command');
  const [journal, setJournal] = useState(() => ({ key: journalKey, intent: readIntent(journalKey) }));
  const pending = journal.key === journalKey ? journal.intent : readIntent(journalKey);
  const setPending = (intent: ResumeIntent | null) => setJournal({ key: journalKey, intent });
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setProposal(null);
    const keys = JSON.parse(signature) as string[];
    if (!keys.length) { setItems([]); setLoading(false); return; }
    void request<{ ok: boolean; items: SpaceFacts[]; failures: { key: string; error: string }[] }>({
      pathId: 'agent.continuity.read', body: { keys }, signal: controller.signal,
    }).then(value => {
      if (controller.signal.aborted) return;
      if (!value.ok || !Array.isArray(value.items) || !Array.isArray(value.failures)) throw new Error('复工服务尚未提供有效状态。');
      setItems(value.items);
      if (value.failures.length) setError(`${value.failures.length} 个空间暂时无法核实，当前列表不完整。`);
    }).catch(reason => { if (!controller.signal.aborted) { setItems([]); setError(reason instanceof Error ? reason.message : '读取失败，请重试。'); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [transport, signature, revision]);
  useEffect(() => { if (page > 0 && page * 6 >= spaceKeys.length) setPage(0); }, [page, spaceKeys.length]);
  const current = items.find(item => item.key === selected) ?? items[0];
  const refresh = useCallback(() => { setNotice(''); setError(''); setRevision(value => value + 1); }, []);
  const choose = (key: string) => { setSelected(key); setExpanded(true); setProposal(null); setNotice(''); };
  const decide = async (facts: SpaceFacts, text: string, supersedesId: string) => {
    setBusy(true); setError('');
    try {
      await request({ pathId: 'agent.continuity.decision', body: {
        spaceKey: facts.key, expectedRevision: facts.revision, id: crypto.randomUUID(), text, supersedesId,
      } });
      refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '决定尚未保存。'); }
    finally { setBusy(false); }
  };
  const suggest = async (facts: SpaceFacts, candidateId?: string) => {
    setBusy(true); setError(''); setProposal(null); setNotice('');
    try {
      const result = await request<{ ok: boolean; proposal: ResumeProposal | null; message?: string }>({
        pathId: 'agent.continuity.suggest', body: { spaceKey: facts.key, expectedRevision: facts.revision, ...(candidateId ? { candidateId } : {}) }, timeoutMs: 25_000,
      });
      if (!result.ok) throw new Error('未取得可靠建议。');
      setProposal(result.proposal); setNotice(result.message ?? '');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '暂时无法判断下一步。'); }
    finally { setBusy(false); }
  };
  const resume = async (intent: ResumeIntent) => {
    setBusy(true); setError('');
    setPending(intent);
    try { if (journalKey) localStorage.setItem(journalKey, JSON.stringify(intent)); }
    catch { setNotice('待确认操作目前只能保留在此窗口，请在关闭前核实。'); }
    try {
      const result = await request<{ ok: boolean; accepted: boolean; receipt: Record<string, unknown> }>({
        pathId: 'agent.continuity.resume', body: { ...intent }, timeoutMs: 25_000,
      });
      if (!result.ok || !result.receipt) throw new Error('尚未取得执行回执。');
      setPending(null); try { if (journalKey) localStorage.removeItem(journalKey); } catch { /* The owner receipt remains authoritative. */ }
      if (result.accepted && mounted.current) onOpen(intent.spaceKey);
      else setNotice('原执行入口尚未开始工作，请打开空间查看原因。');
    } catch (reason) {
      const status = typeof reason === 'object' && reason !== null && 'status' in reason ? Number(reason.status) : 0;
      if (!pending && [400, 401, 403, 404, 409, 422].includes(status)) {
        setPending(null); try { if (journalKey) localStorage.removeItem(journalKey); } catch { /* The owner receipt remains authoritative. */ }
        setProposal(null); setRevision(value => value + 1);
        setError(reason instanceof Error ? reason.message : '原执行入口拒绝了这次继续，请核实当前状态。');
      } else setError(`执行结果尚待核实：${reason instanceof Error ? reason.message : '连接中断'}。核实时保留原操作 ID。`);
    }
    finally { setBusy(false); }
  };
  const decisions = useMemo(() => items.flatMap(item => item.pendingDecisions.map(question => ({ item, question }))), [items]);
  const deliveries = useMemo(() => items.flatMap(item => item.deliveries.map(delivery => ({ item, delivery }))), [items]);
  return <section className="continuity-home" aria-label="项目复工">
    <header className="continuity-heading"><h2>继续工作</h2><button aria-label="刷新复工状态" type="button" disabled={loading || busy} onClick={refresh}><RefreshCw size={15} /></button></header>
    {error ? <p role="alert">{error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {pending ? <div className="continuity-pending" role="status">上次继续操作的结果尚待核实。<button disabled={busy} onClick={() => void resume(pending)} type="button">核实继续操作</button><button {...intentProps(pending.spaceKey)} onClick={() => onOpen(pending.spaceKey)} type="button">查看原工作空间</button></div> : null}
    {loading ? <p role="status">正在核实目标、要求与成果…</p> : null}
    {!loading && !items.length && !error ? <p>从上方开始一段工作，或在工作空间目录中打开已有项目。</p> : null}
    {items.length ? <nav aria-label="选择复工空间" className="continuity-spaces">{items.map(item => <div className="continuity-row" key={item.key}>
      <button {...intentProps(item.key)} className="continuity-open" type="button" onClick={() => onOpen(item.key)} title={item.title}><span className="continuity-row-title">{item.title}</span><span className="continuity-row-hint">{item.blockers[0] || item.candidates[0]?.text || item.goal?.objective || '打开对话，接着聊'}</span></button>
      <button className="continuity-progress" type="button" aria-label={`查看进度：${item.title}`} aria-expanded={expanded && current?.key === item.key} onClick={() => choose(item.key)}><span>{item.running === true ? '正在执行' : item.blockers.length ? '遇到阻塞' : item.pendingDecisions.length ? '待你决定' : item.goal?.status === 'completed' ? '目标已完成' : item.goal?.status === 'cancelled' ? '已取消' : item.goal?.status === 'active' ? '待继续' : '查看进度'}</span><ChevronRight size={14} /></button>
    </div>)}</nav> : null}
    {current ? <details className="continuity-focus" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)} aria-busy={loading}>
      <summary>进度、建议与依据</summary>
      <div className="continuity-focus-heading"><h3>{current.title}</h3><span>{current.running === true ? '正在运行' : current.running === false ? '当前空闲' : '运行状态待核实'}</span></div>
      <p className="continuity-scope">{current.pendingDecisions.length} 项待决定 · {current.deliveries.length} 项成果引用 · {current.candidates.length} 项可接续任务。按本次读取记录展示，不代表总体完成比例。</p>
      <JevProgress facts={current} />
      <dl>
        <dt>上次停在</dt><dd className="continuity-last-update"><ReactMarkdown skipHtml allowedElements={['p', 'strong', 'em', 'code', 'ul', 'ol', 'li', 'br']} unwrapDisallowed>{current.blockers[0] || current.lastReply?.text || current.candidates[0]?.text || current.goal?.objective || '尚无可核实的目标与下一步；可先打开原对话。'}</ReactMarkdown></dd>
        <dt>最新要求</dt><dd>{current.requests.at(-1)?.text || '最近窗口没有用户要求，不能从标题推断。'}</dd>
        <dt>当前约束</dt><dd>{current.constraints?.filter(Boolean).join('；') || current.goal?.successCriteria || '未单独记录验收约束。'}{current.decisions.filter(d => d.status === 'current' && d.text).map(d => <p key={d.id}>{d.text}</p>)}</dd>
        <dt>需要注意</dt><dd>{current.goal?.configured && current.goal.status !== 'active' ? `目标状态：${current.goal.status}；不会自动重新启动。` : current.missing[0]}</dd>
      </dl>
      <div className="continuity-actions"><button {...intentProps(current.key)} type="button" onClick={() => onOpen(current.key)}>打开工作空间 <ArrowRight size={14} /></button>
        <button type="button" disabled={busy || loading || !current.executionAllowed || Boolean(pending)} onClick={() => void suggest(current)}>梳理下一步</button></div>
      <p className="continuity-scope">梳理会将此空间的目标、最近对话片段、已采纳决定及成果引用发送给 Jev；不读取其他空间正文。</p>
      {proposal?.spaceKey === current.key ? <div className="continuity-proposal"><strong>{proposal.origin === 'user' ? '已选择下一步' : '建议下一步'}</strong><p>{proposal.text}</p><button type="button" disabled={busy || Boolean(pending) || loading || proposal.expiresAtMs <= Date.now()} onClick={() => void resume({ spaceKey: current.key, proposalId: proposal.id, commandId: crypto.randomUUID() })}>继续这一步</button></div> : null}
      {current.candidates.length ? <details className="continuity-evidence"><summary>我来选择下一步</summary><p>直接选择已有目标或工作项，不调用 Jev；执行前仍会核实当前状态。</p>{current.candidates.map(candidate => <div key={candidate.id}><p>{candidate.text}</p><button type="button" disabled={busy || loading || !current.executionAllowed || Boolean(pending)} onClick={() => void suggest(current, candidate.id)}>准备这一步</button></div>)}</details> : null}
      <details className="continuity-evidence"><summary>查看依据与缺失信息</summary>
        {current.lastReply ? <blockquote><p>{current.lastReply.text}</p><small>最近回复 {current.lastReply.source.id} · {current.lastReply.source.revision.slice(0, 10)}</small></blockquote> : null}
        {current.sources.map(ref => <p key={`${ref.kind}:${ref.id}`}>{ref.label} · <code>{ref.id}</code> · 版本 {ref.revision.slice(0, 10)}</p>)}
        {current.requests.map((req, i) => <blockquote key={i}><p>{req.text}</p><small>原消息 {req.source.id} · {req.source.revision.slice(0, 10)}</small></blockquote>)}
        <ul>{current.missing.map(value => <li key={value}>{value}</li>)}</ul>
        <small>本次读取：{new Date(current.observedAtMs).toLocaleString()}；打开空间可查看原始记录。</small>
      </details>
      <DecisionEditor key={current.key + current.revision} facts={current} busy={busy || loading} onSave={(value, replaces) => void decide(current, value, replaces)} />
    </details> : null}
    {spaceKeys.length > 6 ? <nav className="continuity-pages" aria-label="复工空间分页"><button type="button" aria-label="上一组复工空间" disabled={!page || loading} onClick={() => setPage(p => p - 1)}><ChevronLeft size={15} /></button><span>已载入目录 · {page + 1}/{Math.ceil(spaceKeys.length / 6)}</span><button aria-label="下一组复工空间" type="button" disabled={(page + 1) * 6 >= spaceKeys.length || loading} onClick={() => setPage(p => p + 1)}><ChevronRight size={15} /></button></nav> : null}
    <details className="continuity-more"><summary>待决定与最近交付{decisions.length ? ` · ${decisions.length} 项待决定` : ''}</summary><div className="continuity-secondary">
      <section><h2>需要你决定</h2>{decisions.length ? decisions.map(({ item, question }) => <article key={item.key + question.id}><p>{question.text}</p><button type="button" onClick={() => onOpen(item.key)}>在原对话中回答</button></article>) : <p>{loading || error ? '状态尚未核实完整。' : '本组已读取的记录中暂无明确待决问题；缺失范围见空间依据。'}</p>}</section>
      <section><h2>最近交付</h2>{deliveries.length ? deliveries.map(({ item, delivery }) => <article key={item.key + delivery.id}><strong>{delivery.title}</strong>{delivery.availability === 'unavailable' || delivery.availability === 'changed' ? <p role="status">文件已变化或无法访问，请核实来源记录。</p> : null}<p>{delivery.generated ? '已生成' : '公开文件引用'} · {delivery.verified === 'unknown' ? '验证待核实' : '有验证记录'} · 采纳待核实</p>
        {item.key.startsWith('session:') && Boolean(delivery.data.mediaId) ? <AgentFileBlock data={delivery.data} sessionId={item.key.slice(8)} /> : <button type="button" onClick={() => { if (typeof delivery.data.path === 'string') openPawOsRoute(desktop, `/files?path=${encodeURIComponent(delivery.data.path)}${typeof delivery.data.sessionId === 'string' && delivery.data.sessionId ? `&session=${encodeURIComponent(delivery.data.sessionId)}` : ''}`); else onOpen(item.key); }}>打开成果</button>}
        <details><summary>查看成果依据</summary>{delivery.fileRevision ? <p>当前文件标识：<code>{delivery.fileRevision.slice(0, 16)}</code>；历史验证与此版本的对应关系待核实。</p> : null}<p>{delivery.source.label} · {delivery.source.id}</p><pre>{JSON.stringify(delivery.review ?? { verification: '未取得绑定此版本的验证记录' }, null, 2)}</pre><button type="button" onClick={() => onOpen(item.key)}>查看来源工作空间</button></details>
      </article>) : <p>本组最近窗口尚无明确登记的用户成果；工具日志不会列为交付。</p>}</section>
    </div></details>
  </section>;
}
function DecisionEditor({ facts, busy, onSave }: { facts: SpaceFacts; busy: boolean; onSave: (value: string, replaces: string) => void }) {
  const [value, setValue] = useState(''); const [replaces, setReplaces] = useState('');
  return <details className="continuity-decisions"><summary>记录或修正当前决定</summary><p>只有你明确采纳的内容会进入当前决定；要求变化后会标为待核实。</p>
    {facts.decisions.filter(d => d.status !== 'superseded').map(d => <div key={d.id}><span>{d.status === 'needs_review' ? '待核实' : '已采纳'}：{d.text || '此决定已撤回'}</span><button type="button" onClick={() => { setReplaces(d.id); setValue(d.text); }}>修正这条</button></div>)}
    <form onSubmit={event => { event.preventDefault(); if (value.trim() || replaces) onSave(value.trim(), replaces); }}><textarea aria-label="当前决定内容" maxLength={8000} value={value} onChange={event => setValue(event.target.value)} placeholder="例如：只在实验副本验证，保留原文引用。" /><button disabled={busy || (!value.trim() && !replaces)} type="submit">{replaces ? '采纳修正并替代旧决定' : '采纳为当前决定'}</button></form>
  </details>;
}
