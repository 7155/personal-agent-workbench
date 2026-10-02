/**
 * PawContextTrace — 对话轨迹视图（真实上下文装配，deepseek-harness 式）
 *
 * 设计合同：
 * - UR-071：轨迹与对话同 Session 平级切换；本组件由 PawSessionWorkspace 的
 *   workspaceView === 'trace' 挂载，active 控制数据拉取。
 * - UR-080：轨迹展示真实 trace（上下文装配、模型调用、工具生命周期、状态因果），
 *   不复制原文聊天，不另造事件源。数据全部来自既有权威 route：
 *     · agent.session.debugContext.get   → 每轮 systemPrompt / 上下文消息 /
 *       公共前缀增量 / Provider 请求 / 缓存证据 / 工具执行（context-debug model）
 *     · agent.session.contextTraces.list → 轮次装配摘要（tokenEstimate 等）
 *     · agent.session.contextTrace.get   → 单轮上下文节点（含 disposition）
 *   事件流模式消费既有 reducer projection（与对话视图同源）。
 * - 状态合同：loading / unavailable / empty 如实呈现，不伪造装配数据。
 *
 * 样式：paw-os/styles/paw-os-agent-next.css 与
 * paw-os/styles/paw-os-agent.css（类名 an-* / paw-agent-trace-v1-* 作用域）。
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent as ReactKeyboardEvent,
  type SetStateAction,
} from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Disclosure } from '@/components/primitives';
import type {
  AgentActivityProjection,
  AgentMessageProjection,
  AgentProjectionState,
  AgentTurnStatus,
} from '@/contracts/agent-reducer';
import type { AgentContextTraceV1 } from '@/contracts/generated/agent-context-trace.v1';
import {
  describeDebugTurn,
  normalizeDebugContextResponse,
  type DebugCacheEvidence,
  type DebugContextRecord,
  type DebugModelCall,
  type DebugToolExecution,
  type DebugTurnSummary,
} from '@/features/context-debug/model';
import {
  assemblyStageEvidence,
  formatEvidenceValue,
  modelContextMessages,
  modelSystemPrompt,
  modelToolSchemas,
  orderContextTraceNodes,
  type AssemblyEvidenceValue,
} from '@/features/agent/status/context-evidence';
import {
  evidenceEchoAppLabel,
  evidenceEchoNodeEntities,
  openEvidenceEchoEntity,
  type EvidenceEchoEntity,
} from '@/features/evidence-echo/evidence-echo';
import '@/features/evidence-echo/evidence-echo.css';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import { AgentBlocks } from '@/features/agent/timeline/BlockRenderer';
import { CopyTextButton } from '@/features/agent/file-preview/CopyTextButton';
import { SmoothDisclosureReveal } from '@/features/agent/timeline/SmoothDisclosureReveal';
import { TraceAgentHandoffButton } from '@/features/trace-agent/handoff';
import { uniqueToolActivities } from '@/features/agent/tool-count';
import { publicAgentErrorText } from '@/features/agent/public-error';
import {
  toggleDisclosureOnKeyPreservingAnchor,
  toggleDisclosurePreservingAnchor,
} from '@/features/agent/timeline/disclosure-anchor';

type TraceMode = 'assembly' | 'events';
type TraceFilter = 'all' | 'msg' | 'tool' | 'appr' | 'sub' | 'state';
const traceModes: TraceMode[] = ['assembly', 'events'];
type TraceUnavailable = {
  message: string;
  retryable: boolean;
};

type ProjectedTraceEvent = {
  id: string;
  atMs: number;
  sequence?: number;
  category: Exclude<TraceFilter, 'all'>;
  type: string;
  title: string;
  summary: string;
  status: string;
  evidence:
    | { kind: 'message'; message: AgentMessageProjection }
    | { kind: 'activity'; activity: AgentActivityProjection };
};

type ProjectedTraceTurn = {
  id: string;
  ordinal: number;
  status: AgentTurnStatus;
  title: string;
  createdAtMs: number;
  updatedAtMs: number;
  events: ProjectedTraceEvent[];
};

/* 阶段色板：与 tokenbar 段一一对应。取值来自 paw-os-agent-next.css 的
   --an-stage-*，而不是本文件里的私有 hex——同一屏里的状态点、阶段标记与
   节点条必须来自同一套色板，否则第二种紫色或红色会读成第二套设计。 */
const STAGE_COLORS = [
  'var(--an-stage-1)',
  'var(--an-stage-2)',
  'var(--an-stage-3)',
  'var(--an-stage-4)',
  'var(--an-stage-5)',
  'var(--an-stage-6)',
  'var(--an-stage-7)',
];
const traceEventDisclosureOverrides = new Map<string, boolean>();
const traceEventDisclosureOverrideLimit = 512;

export function PawContextTrace({
  active,
  focusNodeId = '',
  projection,
  sessionId,
}: {
  active: boolean;
  /** 反向证据链落点：打开后要滚到并高亮的那个装配节点。 */
  focusNodeId?: string;
  projection?: AgentProjectionState;
  sessionId: string;
}) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const [mode, setMode] = useState<TraceMode>(focusNodeId ? 'assembly' : 'events');
  const [filter, setFilter] = useState<TraceFilter>('all');
  const [turns, setTurns] = useState<DebugTurnSummary[]>([]);
  const [selectedTurnId, setSelectedTurnId] = useState('');
  const [context, setContext] = useState<DebugContextRecord | undefined>();
  const [trace, setTrace] = useState<AgentContextTraceV1 | undefined>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [unavailable, setUnavailable] = useState<TraceUnavailable>();
  const requestGeneration = useRef(0);
  const traceModeId = `paw-trace-mode-${useId().replaceAll(':', '')}`;
  const traceModeTabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const moveTraceModeFocus = useCallback((event: ReactKeyboardEvent<HTMLButtonElement>, current: TraceMode) => {
    const currentIndex = traceModes.indexOf(current);
    if (currentIndex < 0) return;
    let nextIndex: number;
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % traceModes.length;
    else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + traceModes.length) % traceModes.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = traceModes.length - 1;
    else return;
    event.preventDefault();
    const nextMode = traceModes[nextIndex];
    setMode(nextMode);
    traceModeTabRefs.current[nextIndex]?.focus();
  }, []);

  /* A historical Session can have a complete persisted event projection while
     its Pi runtime is no longer resident. Keep the event-derived rounds as a
     read-only rail fallback; debugContext remains the only source for the
     assembly view and is still allowed to report unavailable. */
  const traceTurns = useMemo(() => projectionTraceTurns(projection), [projection]);
  const projectedTurnSummaries = useMemo(
    () => projectionDebugTurnSummaries(traceTurns),
    [traceTurns],
  );
  const visibleTurns = useMemo(
    () => reconcileTraceTurnSummaries(turns, projectedTurnSummaries),
    [turns, projectedTurnSummaries],
  );
  const projectionOnly = turns.length === 0 && projectedTurnSummaries.length > 0;

  const fetchTraceForTurn = useCallback(async (turnId: string): Promise<AgentContextTraceV1 | undefined> => {
    if (!turnId) return undefined;
    try {
      const list = await transport.request({
        pathId: 'agent.session.contextTraces.list',
        params: { sessionId },
        query: { limit: 64 },
      });
      const items = array(record(list).items);
      const summary = items.map(record).find((item) => text(item.turnId) === turnId);
      const traceId = text(summary?.traceId);
      if (!traceId) return undefined;
      const detail = await transport.request({
        pathId: 'agent.session.contextTrace.get',
        params: { sessionId, traceId },
      });
      return record(detail) as unknown as AgentContextTraceV1;
    } catch {
      return undefined;
    }
  }, [sessionId, transport]);

  const loadLatest = useCallback(async () => {
    const generation = ++requestGeneration.current;
    setLoading(true);
    setError('');
    setUnavailable(undefined);
    try {
      const value = await transport.request({
        pathId: 'agent.session.debugContext.get',
        params: { sessionId },
      });
      const normalized = normalizeDebugContextResponse(value);
      if (requestGeneration.current !== generation) return;
      setTurns(normalized.availableTurns);
      if (!normalized.available) {
        setUnavailable(traceUnavailableState(normalized.error));
        setContext(undefined);
        setTrace(undefined);
        return;
      }
      setContext(normalized.context);
      const latestTurnId = normalized.context?.turnId
        ?? normalized.availableTurns[normalized.availableTurns.length - 1]?.turnId
        ?? '';
      setSelectedTurnId(latestTurnId);
      const nextTrace = await fetchTraceForTurn(latestTurnId);
      if (requestGeneration.current !== generation) return;
      setTrace(nextTrace);
    } catch (reason) {
      if (requestGeneration.current === generation) setError(traceErrorText(reason));
    } finally {
      if (requestGeneration.current === generation) setLoading(false);
    }
  }, [fetchTraceForTurn, sessionId, transport]);

  async function selectTurn(turnId: string): Promise<void> {
    if (turnId === selectedTurnId && context) return;
    const generation = ++requestGeneration.current;
    setSelectedTurnId(turnId);
    setLoading(true);
    setError('');
    setUnavailable(undefined);
    setContext(undefined);
    setTrace(undefined);
    try {
      const value = await transport.request({
        pathId: 'agent.session.debugContext.get',
        params: { sessionId },
        query: { turnId },
      });
      const normalized = normalizeDebugContextResponse(value);
      if (requestGeneration.current !== generation) return;
      if (normalized.availableTurns.length) setTurns(normalized.availableTurns);
      if (!normalized.available) {
        setUnavailable(traceUnavailableState(normalized.error));
        return;
      }
      setContext(normalized.context);
      const nextTrace = await fetchTraceForTurn(turnId);
      if (requestGeneration.current !== generation) return;
      setTrace(nextTrace);
    } catch (reason) {
      if (requestGeneration.current === generation) setError(traceErrorText(reason));
    } finally {
      if (requestGeneration.current === generation) setLoading(false);
    }
  }

  useEffect(() => {
    requestGeneration.current += 1;
    setTurns([]);
    setSelectedTurnId('');
    setContext(undefined);
    setTrace(undefined);
    setError('');
    setUnavailable(undefined);
  }, [sessionId]);

  useEffect(() => {
    if (!active) {
      requestGeneration.current += 1;
      setLoading(false);
      return;
    }
    void loadLatest();
    return () => {
      requestGeneration.current += 1;
    };
  }, [active, loadLatest]);

  const description = context ? describeDebugTurn(context, visibleTurns) : undefined;
  const assemblyNodes = useMemo(() => orderContextTraceNodes(trace?.nodes), [trace]);
  const stageSegments = useMemo(() => buildStageSegments(assemblyNodes, context), [assemblyNodes, context]);
  const totalTokens = stageSegments.reduce((sum, segment) => sum + segment.tokens, 0);
  const cacheSummary = useMemo(() => summarizeCache(context), [context]);
  const selectedTraceTurns = useMemo(
    () => selectedTurnId ? traceTurns.filter((turn) => turn.id === selectedTurnId) : traceTurns,
    [selectedTurnId, traceTurns],
  );
  const traceCounts = useMemo(() => countTraceEvents(selectedTraceTurns), [selectedTraceTurns]);
  const traceRootRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!selectedTurnId && visibleTurns.length) {
      setSelectedTurnId(visibleTurns.at(-1)?.turnId ?? '');
    }
  }, [selectedTurnId, visibleTurns]);

  /* 反向落点：Session 打开后把那一个装配节点滚进视野。找不到就什么也不做，
     不改变用户当前的阅读位置。 */
  useEffect(() => {
    if (!focusNodeId || mode !== 'assembly' || !assemblyNodes.length) return;
    const node = traceRootRef.current?.querySelector(`[data-trace-node="${CSS.escape(focusNodeId)}"]`);
    node?.scrollIntoView({ block: 'center' });
  }, [assemblyNodes, focusNodeId, mode]);

  return (
    <section className="paw-agent-next an-trace" aria-label="当前 Agent 轨迹" ref={traceRootRef}>
      <aside className="an-trace-rail">
        <header>
          <strong>轮次</strong>
          <small>{visibleTurns.length
            ? projectionOnly
              ? `已投影 ${visibleTurns.length} 轮 · 持久化 Session 事件`
              : `本机保留 ${visibleTurns.length} 轮 · 真实上下文装配记录`
            : '等待 Session 事件'}</small>
        </header>
        <div className="an-turn-list">
          {visibleTurns.map((turn, index) => {
            const status = turn.runningToolCount > 0 ? 'is-run' : 'is-ok';
            return (
              <button
                aria-current={turn.turnId === selectedTurnId}
                className="an-turn-item"
                key={turn.turnId}
                onClick={() => void selectTurn(turn.turnId)}
                type="button"
              >
                <span className="ti-top">
                  <span className={`an-dot ${status}`} />
                  <span className="ti-no">T{turn.turnOrdinal ?? index + 1}</span>
                  <span className="ti-time">{formatTime(turn.capturedAtMs)}</span>
                </span>
                <span className="ti-sum">{turn.summary || '本轮未记录摘要'}</span>
                <span className="ti-meta">
                  <span>{turn.modelCallCount} 调用</span>
                  <span>{turn.toolCallCount} 工具</span>
                  <span>{projectionOnly ? '事件投影' : phaseLabel(turn.assemblyPhase)}</span>
                </span>
              </button>
            );
          })}
          {!loading && !visibleTurns.length ? (
            <div className="an-trace-empty" style={{ padding: 20 }}>还没有可显示的轮次。</div>
          ) : null}
        </div>
      </aside>

      <div className="an-trace-main">
        <div className="an-topbar an-topbar--flush">
          <div className="an-crumb">
            <h1>{selectedTurnId ? `T${turnOrdinalOf(visibleTurns, selectedTurnId)} · Agent 轨迹` : 'Agent 轨迹'}</h1>
            <div className="an-crumb-sub">
              <span className={`an-dot ${context ? 'is-ok' : ''}`} />
              {description ? `${description.label} · ${shortId(selectedTurnId)}` : loading ? '读取中…' : '无数据'}
            </div>
          </div>
          <span aria-label="轨迹模式" aria-orientation="horizontal" className="an-seg" role="tablist">
            <button
              aria-controls={`${traceModeId}-panel`}
              aria-selected={mode === 'assembly'}
              id={`${traceModeId}-assembly-tab`}
              onClick={() => setMode('assembly')}
              onKeyDown={(event) => moveTraceModeFocus(event, 'assembly')}
              ref={(node) => { traceModeTabRefs.current[0] = node; }}
              role="tab"
              tabIndex={mode === 'assembly' ? 0 : -1}
              type="button"
            >上下文装配</button>
            <button
              aria-controls={`${traceModeId}-panel`}
              aria-selected={mode === 'events'}
              id={`${traceModeId}-events-tab`}
              onClick={() => setMode('events')}
              onKeyDown={(event) => moveTraceModeFocus(event, 'events')}
              ref={(node) => { traceModeTabRefs.current[1] = node; }}
              role="tab"
              tabIndex={mode === 'events' ? 0 : -1}
              type="button"
            >事件流</button>
          </span>
        </div>

        {error ? (
          <div className="an-trace-notice" role="status">
            <span>{error}</span>
            <button onClick={() => void loadLatest()} type="button">重试</button>
            <TraceAgentHandoffButton handoff={{
              kind: 'context',
              entityId: selectedTurnId || sessionId,
              title: 'Agent 轨迹读取失败',
              summary: error,
              error,
              sessionId,
              traceId: trace?.traceId,
              sourceRoute: `/agent?session=${encodeURIComponent(sessionId)}`,
              refs: { selectedTurnId, mode },
            }} />
          </div>
        ) : null}
        {unavailable ? (
          <div className="an-trace-notice" role="status">
            <span>{unavailable.message}</span>
            {unavailable.retryable ? (
              <button onClick={() => void loadLatest()} type="button">重新读取</button>
            ) : null}
            <TraceAgentHandoffButton handoff={{
              kind: 'context',
              entityId: selectedTurnId || sessionId,
              title: 'Agent 轨迹暂不可用',
              summary: unavailable.message,
              sessionId,
              traceId: trace?.traceId,
              sourceRoute: `/agent?session=${encodeURIComponent(sessionId)}`,
              refs: { selectedTurnId, mode, retryable: unavailable.retryable },
            }} />
          </div>
        ) : null}

        {mode === 'events' ? (
          <SessionEventTrace
            assemblyAvailable={Boolean(context)}
            counts={traceCounts}
            context={context?.turnId === selectedTurnId ? context : undefined}
            debugTurn={visibleTurns.find((turn) => turn.turnId === selectedTurnId)}
            filter={filter}
            onFilterChange={setFilter}
            onShowAssembly={() => setMode('assembly')}
            panelId={`${traceModeId}-panel`}
            panelLabelledBy={`${traceModeId}-events-tab`}
            turns={selectedTraceTurns}
          />
        ) : context ? (
          <div
            aria-labelledby={`${traceModeId}-assembly-tab`}
            className="an-trace-body"
            id={`${traceModeId}-panel`}
            role="tabpanel"
            tabIndex={0}
          >
            <div className="an-trace-center">
              <div className="an-assembly">
                <div className="ah-top">
                  <span className={`an-phase ${description?.phase === 'compaction_recovery' ? 'is-alt' : ''}`}>
                    {description?.label ?? '上下文装配'}{description?.ordinal ? ` · 第 ${description.ordinal} 轮` : ''}
                  </span>
                  {totalTokens ? <span className="an-ah-stat">装配估算 <b>{formatNumber(totalTokens)}</b> tok</span> : null}
                  {cacheSummary ? <span className="an-ah-stat">缓存读取 <b>{cacheSummary.read}</b> tok（{cacheSummary.ratio}%）</span> : null}
                  {description ? <span className="an-ah-stat">{description.description}</span> : null}
                </div>
                {totalTokens ? (
                  <>
                    <div className="an-tokenbar" role="img" aria-label="装配节点 Token 估算构成">
                      {stageSegments.map((segment, index) => (
                        <span
                          key={segment.stage}
                          style={{ width: `${Math.max(1, (segment.tokens / totalTokens) * 100)}%`, background: STAGE_COLORS[index % STAGE_COLORS.length] }}
                          title={`${segment.stage} ${formatNumber(segment.tokens)}`}
                        />
                      ))}
                    </div>
                    <div className="an-token-legend">
                      {stageSegments.map((segment, index) => (
                        <span key={segment.stage}><i style={{ background: STAGE_COLORS[index % STAGE_COLORS.length] }} />{segment.stage} <b>{formatNumber(segment.tokens)}</b></span>
                      ))}
                    </div>
                  </>
                ) : null}
                {cacheSummary ? (
                  <div className="an-cache-line">
                    <span>前缀缓存命中</span>
                    <span className="cache-bar"><i style={{ width: `${cacheSummary.ratio}%` }} /></span>
                    <span><b>{cacheSummary.ratio}%</b> · 复用 {cacheSummary.reused}</span>
                  </div>
                ) : null}
              </div>

              <div className="an-stage-group">上下文节点 · 按装配顺序</div>
              {assemblyNodes.length
                ? assemblyNodes.map((node) => {
                    const evidence = assemblyStageEvidence(node.stage, context);
                    const entities = evidenceEchoNodeEntities(node, { sessionId });
                    const focused = Boolean(focusNodeId) && node.nodeId === focusNodeId;
                    return (
                    <Disclosure
                      className="an-node"
                      contentClassName="an-node-body"
                      data-disp={node.disposition}
                      data-echo-focus={focused || undefined}
                      data-trace-node={node.nodeId}
                      defaultOpen={focused}
                      key={node.nodeId}
                      summary={(
                        <>
                        <span className="n-ord">{node.ordinal}</span>
                        <span className={`an-disp is-${node.disposition}`} />
                        <span style={{ minWidth: 0, flex: 1 }}>
                          <span className="n-label">{node.label || node.stage}</span>
                          <span className="n-sub">{node.sourceKind}{node.summary ? ` · ${node.summary}` : ''}</span>
                        </span>
                        <span className="n-bar"><i style={{ width: `${barWidth(node.tokenEstimate, maxToken(assemblyNodes))}%` }} /></span>
                        <span className="n-tok">{formatNumber(node.tokenEstimate)} <small>tok</small></span>
                        <span aria-hidden="true" className="an-disclosure-caret">›</span>
                        </>
                      )}
                    >
                        {entities.length ? (
                          <div aria-label="关联来源" className="an-node-sources" role="group">
                            {entities.map((entity) => (
                              <EvidenceEchoOpen desktop={desktop} entity={entity} key={`${entity.appId}:${entity.entityId}`} />
                            ))}
                          </div>
                        ) : null}
                        <div className="nb-row">
                          <span>字符 <b>{formatNumber(node.charCount)}</b></span>
                          <span>耗时 <b>{node.durationMs} ms</b></span>
                          <span>指纹 <b style={{ fontFamily: 'var(--an-mono)' }}>{shortHash(node.fingerprint)}</b></span>
                          <span>处置 <b>{node.disposition}</b></span>
                        </div>
                        {node.reason ? <div className="nb-row"><span>原因 <b>{node.reason}</b></span></div> : null}
                        {evidence ? (
                          <AssemblyEvidence evidence={evidence} />
                        ) : (
                          /* PF-CM-010: a summary row is never a dead end. When
                             contextTrace carries no captured body for this
                             stage, open to the node's complete real capture
                             record and say so — do not stay silent and do not
                             invent content. */
                          <AssemblyEvidence
                            evidence={{
                              label: '节点捕获记录',
                              value: traceNodeCaptureRecord(node),
                              kind: 'json',
                            }}
                            note="contextTrace 未附带该阶段的原文捕获；以上为该节点记录的全部真实字段。"
                          />
                        )}
                    </Disclosure>
                    );
                  })
                : <FallbackNodes context={context} />}
            </div>

            <section aria-label="模型调用与装配元数据" className="an-trace-side">
              <div className="an-side-h">模型调用 · {context.modelCalls.length}</div>
              {context.modelCalls.map((call) => (
                <ModelCallCard
                  cache={findCache(context.cacheEvidence, call)}
                  call={call}
                  key={call.index}
                  tools={context.toolExecutions.filter((tool) => tool.modelCallIndex === call.index)}
                />
              ))}
              <div className="an-side-h">装配元数据</div>
              <dl className="an-kv" style={{ padding: '0 2px' }}>
                <dt>turnId</dt><dd className="mono">{shortId(context.turnId)}</dd>
                <dt>模型</dt><dd className="mono">{modelLabel(context)}</dd>
                <dt>可用工具</dt><dd>{context.activeTools.length} 个</dd>
                <dt>工具执行</dt><dd>{context.toolExecutions.length} 次</dd>
                <dt>记录时间</dt><dd>{formatTime(context.capturedAtMs)} – {formatTime(context.updatedAtMs)}</dd>
              </dl>
            </section>
          </div>
        ) : (
          <div
            aria-labelledby={`${traceModeId}-assembly-tab`}
            className="an-trace-body"
            id={`${traceModeId}-panel`}
            role="tabpanel"
            tabIndex={0}
          ><div className="an-trace-empty">{loading ? '正在读取上下文装配记录…' : '选择左侧轮次查看真实上下文。'}</div></div>
        )}
      </div>
    </section>
  );
}

/* ---------- 模型调用卡 ---------- */

function ModelCallCard({
  cache,
  call,
  tools,
}: {
  cache?: DebugCacheEvidence;
  call: DebugModelCall;
  tools: DebugToolExecution[];
}) {
  const exchange = call.providerExchanges[call.providerExchanges.length - 1];
  const duration = call.completedAtMs ? call.completedAtMs - call.capturedAtMs : undefined;
  return (
    <Disclosure
      className="an-call"
      contentClassName="an-call-body"
      summary={(
        <>
          <span className="c-idx">#{call.index + 1}</span>
          {tools.length ? `发起 ${tools.length} 个工具` : '生成回复'}
          <span className="c-time">{duration !== undefined ? `${(duration / 1000).toFixed(1)}s` : '…'}</span>
        </>
      )}
    >
        <dl className="an-kv">
          <dt>上下文增量</dt>
          <dd>公共前缀 {call.contextDelta.commonPrefixMessages} 条 · 新增 {call.contextDelta.addedMessageCount} 条{call.contextDelta.removedMessageCount ? ` · 移除 ${call.contextDelta.removedMessageCount} 条` : ''}</dd>
          {exchange?.status !== undefined ? <><dt>Provider</dt><dd>{exchange.status}</dd></> : null}
          {cache ? (
            <>
              <dt>输入 / 输出</dt>
              <dd>{formatNumber(cache.inputTokens)} / {formatNumber(cache.outputTokens)} tok</dd>
              <dt>缓存</dt>
              <dd className="mono">read {formatNumber(cache.cacheReadTokens)} · write {formatNumber(cache.cacheWriteTokens)}</dd>
            </>
          ) : null}
        </dl>
        {tools.length ? (
          <div className="an-mini-tools">
            {tools.length > 1 ? <span className="an-batch-tag">⇉ 并行批次</span> : null}
            {tools.map((tool) => (
              <div className="an-mini-tool" key={tool.toolCallId}>
                <span className={`an-dot ${tool.status === 'completed' ? 'is-ok' : tool.status === 'failed' ? 'is-fail' : 'is-run'}`} />
                <span className="mt-name">{tool.toolName}</span>
                <span className="mt-key">{summarizeArgs(tool.args)}</span>
                <span className="mt-dur">{tool.endedAtMs ? `${((tool.endedAtMs - tool.startedAtMs) / 1000).toFixed(1)}s` : '运行中'}</span>
              </div>
            ))}
          </div>
        ) : null}
    </Disclosure>
  );
}

/* ---------- trace 不可用时的 debugContext 兜底节点 ---------- */

function FallbackNodes({ context }: { context: DebugContextRecord }) {
  const resolvedSystemPrompt = modelSystemPrompt(context);
  const resolvedToolSchemas = modelToolSchemas(context);
  const resolvedContextMessages = modelContextMessages(context);
  const nodes: Array<{
    label: string;
    sub: string;
    tokens: number;
    disp: 'included';
    evidence: AssemblyEvidenceValue;
  }> = [
    {
      label: '系统指令',
      sub: 'agent.system',
      tokens: estimateTokens(resolvedSystemPrompt),
      disp: 'included',
      evidence: { label: '本次模型调用收到的系统指令', value: resolvedSystemPrompt || '本轮未捕获系统指令。', kind: 'text' },
    },
    {
      label: '工具定义',
      sub: `tools.schemas · ${resolvedToolSchemas.length} 个可用工具`,
      tokens: resolvedToolSchemas.length * 110,
      disp: 'included',
      evidence: { label: '本次模型调用收到的工具 Schema', value: formatEvidenceValue(resolvedToolSchemas), kind: 'json' },
    },
    {
      label: '上下文消息',
      sub: `context.messages · 最近一次调用 ${resolvedContextMessages.length} 条`,
      tokens: estimateTokens(formatEvidenceValue(resolvedContextMessages)),
      disp: 'included',
      evidence: { label: '按 Provider 顺序装配的上下文消息', value: formatEvidenceValue(resolvedContextMessages), kind: 'json' },
    },
    {
      label: '当前输入',
      sub: 'user.prompt',
      tokens: estimateTokens(context.prompt),
      disp: 'included',
      evidence: { label: '本轮用户输入原文', value: context.prompt || '本轮未捕获用户输入。', kind: 'text' },
    },
  ];
  const max = Math.max(1, ...nodes.map((node) => node.tokens));
  return (
    <>
      {nodes.map((node) => (
        <Disclosure
          className="an-node an-node--evidence"
          contentClassName="an-node-body"
          data-disp={node.disp}
          key={node.label}
          summary={(
            <>
              <span className="an-disp is-included" />
              <span style={{ minWidth: 0, flex: 1 }}>
                <span className="n-label">{node.label}</span>
                <span className="n-sub">{node.sub}</span>
              </span>
              <span className="n-bar"><i style={{ width: `${(node.tokens / max) * 100}%` }} /></span>
              <span className="n-tok">{node.tokens ? `≈${formatNumber(node.tokens)}` : '—'} <small>tok</small></span>
              <span aria-hidden="true" className="an-disclosure-caret">›</span>
            </>
          )}
        >
          <AssemblyEvidence evidence={node.evidence} />
        </Disclosure>
      ))}
      <div className="an-trace-notice" role="status">
        <span>当前由 Runtime 的 debugContext 提供完整捕获内容；节点时序与处置证据需 contextTrace 才能显示。</span>
      </div>
    </>
  );
}

/* 正向证据链：只有当这个装配节点确实记录了某个具体证据实体的标识时，摘要行
   才多出一个可点的入口。解析不出标识的节点保持不可点击——一个打不开东西的
   链接比没有链接更糟。 */
function EvidenceEchoOpen({
  desktop,
  entity,
}: {
  desktop: ReturnType<typeof usePawOsDesktop>;
  entity: EvidenceEchoEntity;
}) {
  return (
    <button
      aria-label={`在 ${evidenceEchoAppLabel(entity.appId)} 打开 ${entity.label}`}
      className="evidence-echo-open"
      onClick={(interaction) => {
        interaction.stopPropagation();
        openEvidenceEchoEntity(desktop, entity);
      }}
      type="button"
    >
      <b>{evidenceEchoAppLabel(entity.appId)}</b>
      <span>{entity.label}</span>
    </button>
  );
}

function AssemblyEvidence({ evidence, note }: { evidence: AssemblyEvidenceValue; note?: string }) {
  return (
    <section className="an-assembly-evidence" aria-label={evidence.label}>
      <header>
        <strong>{evidence.label}</strong>
        <span className="agent-trace-evidence-actions">
          <small>{formatNumber(countLines(evidence.value))} 行 · {formatNumber(evidence.value.length)} 字符</small>
          <CopyTextButton label={evidence.label} value={evidence.value} />
        </span>
      </header>
      <pre
        aria-label={`${evidence.label}，可滚动原文`}
        data-kind={evidence.kind}
        role="region"
        tabIndex={0}
      >
        {evidence.value}
      </pre>
      {note ? <p className="agent-trace-evidence-note">{note}</p> : null}
    </section>
  );
}

function traceNodeCaptureRecord(node: AgentContextTraceV1['nodes'][number]): string {
  return formatEvidenceValue(safeTraceEvidence({
    stage: node.stage,
    label: node.label || undefined,
    sourceKind: node.sourceKind,
    disposition: node.disposition,
    summary: node.summary || undefined,
    reason: node.reason || undefined,
    tokenEstimate: node.tokenEstimate,
    charCount: node.charCount,
    durationMs: node.durationMs,
    fingerprint: node.fingerprint,
    metadata: node.metadata,
    createdAtMs: node.createdAtMs,
  }));
}

/* ---------- 数据派生 ---------- */

function buildStageSegments(
  nodes: AgentContextTraceV1['nodes'],
  context: DebugContextRecord | undefined,
): Array<{ stage: string; tokens: number }> {
  if (nodes.length) {
    /* Nodes arrive in assembly order, and Map keeps insertion order, so the
       tokenbar segments read as the same sequence as the node list below. */
    const byStage = new Map<string, number>();
    for (const node of nodes) {
      if (node.disposition !== 'included') continue;
      byStage.set(node.stage, (byStage.get(node.stage) ?? 0) + node.tokenEstimate);
    }
    return [...byStage.entries()].map(([stage, tokens]) => ({ stage, tokens }));
  }
  if (!context) return [];
  const segments = [
    { stage: '系统指令', tokens: estimateTokens(context.systemPrompt) },
    { stage: '工具定义', tokens: context.activeTools.length * 110 },
    { stage: '当前输入', tokens: estimateTokens(context.prompt) },
  ];
  const cacheTokens = context.cacheEvidence[0]?.inputTokens ?? 0;
  if (cacheTokens) segments.unshift({ stage: '历史与前缀', tokens: Math.max(0, cacheTokens) });
  return segments.filter((segment) => segment.tokens > 0);
}

function summarizeCache(context: DebugContextRecord | undefined): { read: string; ratio: number; reused: string } | undefined {
  if (!context?.cacheEvidence.length) return undefined;
  const totalRead = Math.max(0, context.cacheEvidence.reduce((sum, item) => sum + item.cacheReadTokens, 0));
  const totalInput = context.cacheEvidence.reduce((sum, item) => sum + item.inputTokens, 0);
  const reused = Math.max(0, context.cacheEvidence.reduce((sum, item) => sum + item.prefixBytes, 0));
  if (totalInput <= 0) return undefined;
  return {
    read: formatNumber(totalRead),
    ratio: Math.min(100, Math.max(0, Math.round((totalRead / totalInput) * 100))),
    reused: `${(reused / 1024).toFixed(1)} KB`,
  };
}

function SessionEventTrace({
  assemblyAvailable,
  context,
  counts,
  debugTurn,
  filter,
  onFilterChange,
  onShowAssembly,
  panelId,
  panelLabelledBy,
  turns,
}: {
  assemblyAvailable: boolean;
  context?: DebugContextRecord;
  counts: Record<TraceFilter, number>;
  debugTurn?: DebugTurnSummary;
  filter: TraceFilter;
  onFilterChange: (filter: TraceFilter) => void;
  onShowAssembly: () => void;
  panelId: string;
  panelLabelledBy: string;
  turns: ProjectedTraceTurn[];
}) {
  const filters: Array<{ id: TraceFilter; label: string }> = [
    { id: 'all', label: '全部' },
    { id: 'msg', label: '消息' },
    { id: 'tool', label: '工具' },
    { id: 'appr', label: '审批' },
    { id: 'sub', label: '子 Agent' },
    { id: 'state', label: '状态' },
  ];
  const filteredTurns = turns.flatMap((turn) => {
    const events = filter === 'all' ? turn.events : turn.events.filter((event) => event.category === filter);
    return events.length ? [{ ...turn, events }] : [];
  });
  const tail = sessionTraceTail(turns);
  return (
    <div aria-labelledby={panelLabelledBy} className="paw-agent-trace-v1" id={panelId} role="tabpanel" tabIndex={0}>
      <nav aria-label="Agent 轨迹筛选" className="paw-agent-trace-v1__filters">
        {filters.map((item) => (
          <button
            aria-pressed={filter === item.id}
            key={item.id}
            onClick={() => onFilterChange(item.id)}
            type="button"
          >
            {item.label}<span>{counts[item.id]}</span>
          </button>
        ))}
      </nav>
      <div className="paw-agent-trace-v1__turns">
        {context ? <TurnContextEvidence context={context} key={`${context.sessionId}:${context.turnId}`} /> : null}
        {filteredTurns.map((turn) => (
          <article className="paw-agent-trace-v1__turn" data-status={turn.status} key={turn.id}>
            <header>
              <span className="paw-agent-trace-v1__turn-no">TURN #{turn.ordinal}</span>
              <strong>{turn.title}</strong>
              <i data-status={turn.status}>{traceStatusLabel(turn.status)}</i>
              <time>{formatTraceRange(turn.createdAtMs, turn.updatedAtMs, turn.status)}</time>
            </header>
            <ol>
              {turn.events.map((event) => (
                <TraceEventDisclosure event={event} key={event.id} />
              ))}
            </ol>
          </article>
        ))}
        {tail ? (
          <div className="paw-agent-trace-v1__tail" data-status={tail.status} role="status">
            <span className="paw-agent-trace-v1__tail-dot" />
            <strong>{tail.label}</strong>
            <time>{formatTime(tail.atMs)}</time>
          </div>
        ) : null}
        {!filteredTurns.length ? counts.all === 0 && debugTurn ? (
          <div className="an-trace-empty an-trace-empty--events" role="status" aria-label="当前轮次没有可投影事件">
            <strong>T{debugTurn.turnOrdinal ?? '—'} 暂无可投影的 Session 事件</strong>
            <span>上下文装配记录可用 · {formatTime(debugTurn.updatedAtMs)} 更新。</span>
            {assemblyAvailable ? <button onClick={onShowAssembly} type="button">查看上下文装配</button> : null}
          </div>
        ) : (
          <div className="an-trace-empty an-trace-empty--events" role="status" aria-label="当前筛选没有可投影事件">
            <strong>当前筛选没有事件</strong>
            <span>选择其他筛选，或切换到上下文装配查看这一轮的真实输入结构。</span>
            {assemblyAvailable ? <button onClick={onShowAssembly} type="button">查看上下文装配</button> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** The event projection has no model input. Keep the retained debug capture
 * alongside it, with a separate selector for each actual model call. */
function TurnContextEvidence({ context }: { context: DebugContextRecord }) {
  const [selectedIndex, setSelectedIndex] = useState(context.modelCalls[0]?.index);
  const call = context.modelCalls.find((item) => item.index === selectedIndex) ?? context.modelCalls[0];
  const provider = call?.providerContext;
  const exactSystem = typeof provider?.systemPrompt === 'string';
  const exactTools = Array.isArray(provider?.tools);
  const system = exactSystem ? provider.systemPrompt as string : context.systemPrompt;
  const messages = Array.isArray(provider?.messages) ? provider.messages : call?.contextMessages;
  const tools = exactTools ? provider.tools : context.toolSchemas;
  return (
    <section aria-label="本轮完整内容" className="an-turn-context" role="region">
      <Disclosure className="an-turn-context__disclosure" defaultOpen summary={<>
        <h2>本轮完整内容</h2><span>{context.modelCalls.length} 次模型调用</span><span aria-hidden="true" className="an-disclosure-caret">›</span>
      </>}>
      <header className="an-turn-context__header">
        <p>系统提示词、上下文、工具与模型请求原文</p>
        {context.modelCalls.length ? (
          <label>模型调用
            <select aria-label="模型调用" onChange={(event) => setSelectedIndex(Number(event.target.value))} value={call?.index}>
              {context.modelCalls.map((item, ordinal) => <option key={item.index} value={item.index}>第 {ordinal + 1} / {context.modelCalls.length} 次 · {formatTime(item.capturedAtMs)}</option>)}
            </select>
          </label>
        ) : null}
      </header>
      {!call ? <p className="agent-trace-evidence-note">本轮未捕获逐次模型调用；以下为整轮保留记录。</p>
        : !exactSystem || !exactTools ? <p className="agent-trace-evidence-note">历史快照：缺失的系统提示词或工具定义使用整轮保留记录，不能证明本次请求的精确内容。</p> : null}
      <div key={call?.index ?? 'turn'}>
        <TurnEvidenceField label="系统提示词" value={system || (exactSystem ? '本次调用的系统提示词为空。' : '未捕获系统提示词。')} kind="text" defaultOpen />
        <TurnEvidenceField label="本轮输入原文" value={context.prompt || '未捕获本轮输入。'} kind="text" />
        <TurnEvidenceField label="完整上下文消息" value={messages ?? '未捕获本次调用的上下文消息。'} />
        <TurnEvidenceField label="工具定义" value={tools} />
        {call ? <>
          <TurnEvidenceField label="模型服务请求与回执" value={call.providerExchanges.length ? call.providerExchanges : '未捕获模型服务请求与回执。'} />
          <TurnEvidenceField label="模型回复" value={call.assistantMessage ?? '未捕获本次模型回复。'} />
          <TurnEvidenceField label="上下文增量" value={call.contextDelta} />
          <TurnEvidenceField label="本次工具执行" value={context.toolExecutions.filter((tool) => tool.modelCallIndex === call.index)} />
        </> : null}
        <TurnEvidenceField label="整轮完整捕获记录" value={context.raw} />
      </div>
      </Disclosure>
    </section>
  );
}

function TurnEvidenceField({ label, value, kind = 'json', defaultOpen = false }: {
  label: string;
  value: unknown;
  kind?: 'text' | 'json';
  defaultOpen?: boolean;
}) {
  // Reuse the event evidence policy for credential fields without truncating
  // prompt text, context bodies or tool results.
  const content = formatEvidenceValue(kind === 'text' ? value : safeTraceEvidence(value));
  return (
    <Disclosure className="an-turn-context__field" contentClassName="an-turn-context__body" defaultOpen={defaultOpen} summary={<>
      <strong>{label}</strong><span>{formatNumber(content.length)} 字符</span><span aria-hidden="true" className="an-disclosure-caret">›</span>
    </>}>
      <AssemblyEvidence evidence={{ label, value: content, kind }} />
    </Disclosure>
  );
}

function TraceEventDisclosure({ event }: { event: ProjectedTraceEvent }) {
  const [open, setOpen] = useTraceEventDisclosure(event.id);
  const [presence, setPresence] = useState(open);
  const contentId = `paw-agent-trace-event-${useId().replaceAll(':', '')}`;
  return (
    <li data-category={event.category} data-status={event.status}>
      <details
        className="paw-agent-trace-v1__event"
        data-expanded={open || undefined}
        open={open || presence}
      >
        <summary
          aria-controls={contentId}
          aria-expanded={open}
          aria-label={`${open ? '收起' : '展开'}${event.title}事件证据：${event.type}`}
          onClick={(interaction) => toggleDisclosurePreservingAnchor(interaction, setOpen)}
          onKeyDown={(interaction) => toggleDisclosureOnKeyPreservingAnchor(interaction, setOpen)}
        >
          <time>{formatTime(event.atMs)}</time>
          <span aria-hidden="true" className="paw-agent-trace-v1__node" />
          <span className="paw-agent-trace-v1__event-type">{event.type}</span>
          <strong>{event.title}</strong>
          {event.summary ? <span className="paw-agent-trace-v1__summary">{event.summary}</span> : <span />}
          <i>{traceEventStatusLabel(event.status)}</i>
          <span aria-hidden="true" className="paw-agent-trace-v1__event-caret">›</span>
        </summary>
        <SmoothDisclosureReveal
          className="paw-agent-trace-v1__event-reveal"
          id={contentId}
          onPresenceChange={setPresence}
          open={open}
        >
          <TraceEventEvidence event={event} />
        </SmoothDisclosureReveal>
      </details>
    </li>
  );
}

function TraceEventEvidence({
  event,
}: {
  event: ProjectedTraceEvent;
}) {
  if (event.evidence.kind === 'message') {
    const message = event.evidence.message;
    return (
      <section
        aria-label={`${event.title}事件证据`}
        className="paw-agent-trace-v1__evidence"
        role="region"
        tabIndex={0}
      >
        <header>
          <strong>消息原文与内容块</strong>
          <span>{message.blocks.length} 块 · {message.attachments.length} 个附件</span>
        </header>
        <dl className="paw-agent-trace-v1__evidence-meta">
          <dt>消息</dt><dd className="mono">{message.id}</dd>
          <dt>角色</dt><dd>{traceMessageRoleLabel(message.role)}</dd>
          <dt>状态</dt><dd>{traceEventStatusLabel(message.status)}</dd>
        </dl>
        {message.blocks.length ? (
          <div className="paw-agent-trace-v1__message-evidence">
            <AgentBlocks
              blocks={message.blocks}
              sessionId={message.sessionId}
              streaming={message.status === 'streaming'}
            />
          </div>
        ) : <p>这条消息没有可显示的内容块。</p>}
      </section>
    );
  }

  const activity = event.evidence.activity;
  const sections = activityEvidenceSections(activity.payload);
  return (
    <section
      aria-label={`${event.title}事件证据`}
      className="paw-agent-trace-v1__evidence"
      role="region"
      tabIndex={0}
    >
      <header>
        <strong>活动证据</strong>
        <span>{activity.kind} · {formatDurationMs(activity.updatedAtMs - activity.createdAtMs)}</span>
      </header>
      <dl className="paw-agent-trace-v1__evidence-meta">
        <dt>活动</dt><dd className="mono">{activity.id}</dd>
        <dt>状态</dt><dd>{traceEventStatusLabel(activity.status)}</dd>
        <dt>摘要</dt><dd>{activity.summary || '无摘要'}</dd>
      </dl>
      <div className="paw-agent-trace-v1__payloads">
        {sections.map((section) => <TraceEvidenceSection key={section.label} section={section} />)}
      </div>
    </section>
  );
}

type TraceEvidenceSectionValue = {
  label: string;
  value: string;
};

function TraceEvidenceSection({ section }: { section: TraceEvidenceSectionValue }) {
  return (
    <Disclosure
      className="paw-agent-trace-v1__payload"
      contentClassName="paw-agent-trace-v1__payload-body"
      summary={(
        <>
          <strong>{section.label}</strong>
          <span>{formatNumber(section.value.length)} 字符</span>
          <span aria-hidden="true" className="paw-agent-trace-v1__event-caret">›</span>
        </>
      )}
    >
      <div className="agent-trace-evidence-tools">
        <small>{formatNumber(countLines(section.value))} 行 · {formatNumber(section.value.length)} 字符</small>
        <CopyTextButton label={section.label} value={section.value} />
      </div>
      <pre
        aria-label={`${section.label}，可滚动原文`}
        role="region"
        tabIndex={0}
      >
        {section.value}
      </pre>
    </Disclosure>
  );
}

function useTraceEventDisclosure(
  key: string,
): [boolean, Dispatch<SetStateAction<boolean>>] {
  const [open, setOpenState] = useState(() => traceEventDisclosureOverrides.get(key) ?? false);
  const setOpen = useCallback<Dispatch<SetStateAction<boolean>>>((nextValue) => {
    setOpenState((current) => {
      const next = typeof nextValue === 'function' ? nextValue(current) : nextValue;
      if (traceEventDisclosureOverrides.size >= traceEventDisclosureOverrideLimit
        && !traceEventDisclosureOverrides.has(key)) {
        const oldest = traceEventDisclosureOverrides.keys().next().value;
        if (typeof oldest === 'string') traceEventDisclosureOverrides.delete(oldest);
      }
      traceEventDisclosureOverrides.set(key, next);
      return next;
    });
  }, [key]);
  return [open, setOpen];
}

function activityEvidenceSections(payload: Record<string, unknown>): TraceEvidenceSectionValue[] {
  const consumed = new Set<string>();
  const sections: TraceEvidenceSectionValue[] = [];
  const addFirst = (label: string, keys: readonly string[]) => {
    const key = keys.find((candidate) => Object.hasOwn(payload, candidate));
    if (!key) return;
    consumed.add(key);
    sections.push({ label, value: formatEvidenceValue(safeTraceEvidence(payload[key])) });
  };
  addFirst('调用参数', ['args', 'arguments', 'input', 'request']);
  addFirst('过程记录', ['progressHistory', 'updates', 'progress']);
  addFirst('工具返回', ['result', 'output', 'response']);

  const remaining = Object.fromEntries(
    Object.entries(payload).filter(([key]) => !consumed.has(key)),
  );
  if (Object.keys(remaining).length || sections.length === 0) {
    sections.push({ label: '事件载荷', value: formatEvidenceValue(safeTraceEvidence(remaining)) });
  }
  return sections;
}

function safeTraceEvidence(value: unknown, seen = new WeakSet<object>()): unknown {
  if (Array.isArray(value)) return value.map((item) => safeTraceEvidence(item, seen));
  if (!value || typeof value !== 'object') return value;
  if (seen.has(value)) return '[循环引用]';
  seen.add(value);
  const safe: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    safe[key] = isSensitiveTraceKey(key) ? '[已隐藏敏感字段]' : safeTraceEvidence(item, seen);
  }
  return safe;
}

function isSensitiveTraceKey(key: string): boolean {
  return /(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie)/iu.test(key);
}

function traceMessageRoleLabel(role: AgentMessageProjection['role']): string {
  if (role === 'user') return '用户';
  if (role === 'assistant') return 'Agent';
  return 'Tool';
}

function sessionTraceTail(turns: ProjectedTraceTurn[]): { label: string; status: AgentTurnStatus; atMs: number } | undefined {
  const turn = turns.at(-1);
  if (!turn) return undefined;
  const event = turn.events.at(-1);
  const sessionState = turn.status === 'running' || turn.status === 'queued'
    ? 'Session 进行中'
    : turn.status === 'waiting'
      ? 'Session 等待你'
      : turn.status === 'failed'
        ? 'Session 本轮失败'
        : turn.status === 'aborted'
          ? 'Session 已停止'
          : 'Session 已同步';
  return {
    label: `${sessionState}${event ? ` · 最后事件 ${event.title}` : ''}`,
    status: turn.status,
    atMs: event?.atMs ?? turn.updatedAtMs,
  };
}

function orderTraceEvents(events: ProjectedTraceEvent[]): ProjectedTraceEvent[] {
  const byTime = (left: ProjectedTraceEvent, right: ProjectedTraceEvent) => left.atMs - right.atMs || left.id.localeCompare(right.id);
  const sequenced = events.filter((event) => Number.isFinite(event.sequence))
    .sort((left, right) => left.sequence! - right.sequence! || byTime(left, right));
  const captured = events.filter((event) => !Number.isFinite(event.sequence)).sort(byTime);
  const ordered: ProjectedTraceEvent[] = [];
  let index = 0;
  // Persisted user messages may lack event sequence numbers. Place those by
  // capture time between ordered events; never compare epoch milliseconds
  // with sequence IDs or disturb the order of authoritative event receipts.
  for (const event of sequenced) {
    while (index < captured.length && captured[index].atMs <= event.atMs) ordered.push(captured[index++]);
    ordered.push(event);
  }
  return [...ordered, ...captured.slice(index)];
}

export function projectionTraceTurns(projection: AgentProjectionState | undefined): ProjectedTraceTurn[] {
  if (!projection) return [];
  return (projection.turnOrder ?? []).map((turnId, index) => {
    const turn = projection.turnsById[turnId];
    if (!turn) return undefined;
    const messages = turn.messageIds
      .map((messageId) => projection.messagesById[messageId])
      .filter((message): message is AgentMessageProjection => Boolean(message));
    const activities = turn.activityIds
      .map((activityId) => projection.activitiesById[activityId])
      .filter((activity): activity is AgentActivityProjection => Boolean(activity));
    const events = orderTraceEvents([
      ...messages.map(projectMessageTraceEvent),
      ...activities.map(projectActivityTraceEvent),
    ]);
    const prompt = messages.find((message) => message.role === 'user');
    return {
      id: turn.id,
      ordinal: index + 1,
      status: turn.status,
      title: prompt ? userInputTraceTitle(prompt) : `Session 回合 ${index + 1}`,
      createdAtMs: turn.createdAtMs,
      updatedAtMs: turn.updatedAtMs,
      events,
    };
  }).filter((turn): turn is ProjectedTraceTurn => Boolean(turn));
}

function projectionDebugTurnSummaries(turns: ProjectedTraceTurn[]): DebugTurnSummary[] {
  return turns.map((turn) => {
    const toolActivities = uniqueToolActivities(turn.events
      .filter((event) => event.category === 'tool' && event.evidence.kind === 'activity')
      .map((event) => event.evidence.kind === 'activity' ? event.evidence.activity : undefined)
      .filter((activity): activity is AgentActivityProjection => Boolean(activity)));
    return {
      turnId: turn.id,
      clientMessageId: '',
      capturedAtMs: turn.createdAtMs,
      updatedAtMs: turn.updatedAtMs,
      /* The persisted projection has no model-call boundary. Keep these
         counters honest instead of inferring provider work from UI events. */
      modelCallCount: 0,
      providerRequestCount: 0,
      toolCallCount: toolActivities.length,
      runningToolCount: toolActivities.filter((activity) => activity.status === 'running').length,
      turnOrdinal: turn.ordinal,
      summary: turn.title,
    };
  });
}

function projectMessageTraceEvent(message: AgentMessageProjection): ProjectedTraceEvent {
  const category: ProjectedTraceEvent['category'] = message.role === 'tool' ? 'tool' : 'msg';
  const title = message.role === 'user'
    ? '用户输入'
    : message.role === 'tool'
      ? 'Tool 返回内容'
      : 'Agent 回复';
  return {
    id: `message:${message.id}`,
    atMs: message.createdAtMs,
    sequence: message.timelineSequence,
    category,
    type: message.role === 'user' ? 'user.prompt' : message.role === 'tool' ? 'tool.result' : 'assistant.msg',
    title,
    summary: message.role === 'assistant' ? `${message.blocks.length} 个内容块` : '',
    status: message.status,
    evidence: { kind: 'message', message },
  };
}

function projectActivityTraceEvent(activity: AgentActivityProjection): ProjectedTraceEvent {
  const category = activityTraceCategory(activity);
  const toolName = text(activity.payload.toolName) || text(activity.payload.toolId) || text(activity.payload.label);
  const title = category === 'sub'
    ? activity.summary || '子 Agent'
    : category === 'tool'
    ? toolName || activity.summary || 'Tool'
    : activity.summary || traceActivityLabel(activity.kind);
  return {
    id: `activity:${activity.id}`,
    atMs: activity.createdAtMs,
    sequence: activity.timelineSequence,
    category,
    type: category === 'sub'
      ? activity.status === 'completed' ? 'sub.end' : 'sub.spawn'
      : traceActivityType(activity.kind),
    title,
    summary: category === 'tool' && toolName && activity.summary !== toolName ? activity.summary : '',
    status: activity.status,
    evidence: { kind: 'activity', activity },
  };
}

function activityTraceCategory(activity: AgentActivityProjection): ProjectedTraceEvent['category'] {
  const kind = activity.kind.toLowerCase();
  const toolId = `${text(activity.payload.toolName)} ${text(activity.payload.toolId)}`.toLowerCase();
  if (kind.includes('approval') || kind === 'user_input_required') return 'appr';
  if (toolId.includes('subagent') || kind.includes('subagent') || kind.includes('sub_agent') || kind.includes('delegate') || kind.includes('child')) return 'sub';
  if (kind.startsWith('tool_') || kind.includes('browser')) return 'tool';
  return 'state';
}

function traceActivityType(kind: string): string {
  const labels: Record<string, string> = {
    reasoning_summary: 'thinking',
    context_compaction: 'compaction',
    tool_started: 'tool.call',
    tool_progress: 'tool.progress',
    tool_finished: 'tool.end',
    approval_required: 'approval.req',
    approval_resolved: 'approval.res',
    user_input_required: 'input.req',
    turn_failed: 'turn.failed',
  };
  return labels[kind] ?? kind.replaceAll('_', '.');
}

function traceActivityLabel(kind: string): string {
  const labels: Record<string, string> = {
    reasoning_summary: '思考摘要',
    context_compaction: '上下文折叠',
    approval_required: '等待审批',
    approval_resolved: '审批已处理',
    user_input_required: '等待输入',
    turn_failed: '本轮失败',
  };
  return labels[kind] ?? kind;
}

function countTraceEvents(turns: ProjectedTraceTurn[]): Record<TraceFilter, number> {
  const counts: Record<TraceFilter, number> = { all: 0, msg: 0, tool: 0, appr: 0, sub: 0, state: 0 };
  for (const event of turns.flatMap((turn) => turn.events)) {
    counts.all += 1;
    counts[event.category] += 1;
  }
  return counts;
}

function traceStatusLabel(status: AgentTurnStatus): string {
  if (status === 'completed') return '完成';
  if (status === 'running') return '进行中';
  if (status === 'waiting') return '等待你';
  if (status === 'failed') return '失败';
  if (status === 'aborted') return '已停止';
  return '排队中';
}

function traceEventStatusLabel(status: string): string {
  if (status === 'completed') return '完成';
  if (status === 'running' || status === 'streaming') return '进行中';
  if (status === 'waiting') return '等待你';
  if (status === 'failed') return '失败';
  return '';
}

function formatTraceRange(createdAtMs: number, updatedAtMs: number, status: AgentTurnStatus): string {
  const start = formatTime(createdAtMs);
  if (status === 'running' || status === 'queued') return `${start} → 进行中`;
  const durationMs = Math.max(0, updatedAtMs - createdAtMs);
  const duration = durationMs >= 1_000 ? `${(durationMs / 1_000).toFixed(durationMs >= 10_000 ? 0 : 1)}s` : `${durationMs}ms`;
  return `${start} → ${formatTime(updatedAtMs)} · ${duration}`;
}

function formatDurationMs(durationMs: number): string {
  const safe = Math.max(0, durationMs);
  return safe >= 1_000
    ? `${(safe / 1_000).toFixed(safe >= 10_000 ? 0 : 1)} 秒`
    : `${safe} ms`;
}

/* ---------- 小工具 ---------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}
function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
function estimateTokens(content: string): number {
  return content ? Math.ceil(content.length / 4) : 0;
}
function countLines(content: string): number {
  return content ? content.split('\n').length : 0;
}
function findCache(cache: DebugCacheEvidence[], call: DebugModelCall): DebugCacheEvidence | undefined {
  return cache.find((item) => item.requestIndex === call.index);
}
function maxToken(nodes: AgentContextTraceV1['nodes']): number {
  return Math.max(1, ...nodes.map((node) => node.tokenEstimate));
}
function barWidth(value: number, max: number): number {
  return Math.max(3, Math.round((value / max) * 100));
}
function modelLabel(context: DebugContextRecord): string {
  return text(context.model.modelId) || text(context.model.reference) || text(context.model.model) || '—';
}
function turnOrdinalOf(turns: DebugTurnSummary[], turnId: string): number {
  const index = turns.findIndex((turn) => turn.turnId === turnId);
  const found = turns[index];
  return found?.turnOrdinal ?? (index >= 0 ? index + 1 : 0);
}

/** Debug captures may arrive newest-first. Share transcript ordinals with the
 * event view; only fall back to chronological order within retained captures. */
export function reconcileTraceTurnSummaries(
  captures: DebugTurnSummary[],
  projected: DebugTurnSummary[],
): DebugTurnSummary[] {
  if (!captures.length) return projected;
  const projectedById = new Map(projected.map((turn) => [turn.turnId, turn]));
  const retainedOrdinals = new Map([...captures]
    .sort((left, right) => left.capturedAtMs - right.capturedAtMs)
    .map((turn, index) => [turn.turnId, index + 1]));
  return captures.map((turn) => {
    const transcript = projectedById.get(turn.turnId);
    return {
      ...turn,
      turnOrdinal: turn.turnOrdinal ?? transcript?.turnOrdinal ?? retainedOrdinals.get(turn.turnId),
      summary: turn.summary || transcript?.summary,
    };
  });
}
function phaseLabel(phase: DebugTurnSummary['assemblyPhase']): string {
  if (phase === 'initial') return '首轮装配';
  if (phase === 'compaction_recovery') return '压缩恢复';
  return '增量装配';
}
function userInputTraceTitle(message: AgentMessageProjection): string {
  const charCount = message.blocks.reduce((total, block) => {
    if (block.type !== 'text') return total;
    return total + (text(block.data.text) || text(block.data.content)).length;
  }, 0);
  const parts = ['用户输入', charCount ? `${charCount} 字` : '无文本'];
  if (message.attachments.length) parts.push(`含 ${message.attachments.length} 个附件`);
  return parts.join(' · ');
}
function summarizeArgs(args: unknown): string {
  if (!isRecord(args)) return '';
  const metadata: string[] = [];
  for (const key of ['count', 'limit', 'offset', 'page', 'depth'] as const) {
    const value = args[key];
    if (typeof value === 'number' && Number.isFinite(value)) metadata.push(`${key} ${value}`);
  }
  for (const key of ['dryRun', 'recursive'] as const) {
    const value = args[key];
    if (typeof value === 'boolean') metadata.push(`${key} ${value ? '是' : '否'}`);
  }
  return metadata.join(' · ') || (Object.keys(args).length ? '参数已记录' : '无参数');
}
function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN');
}
function formatTime(atMs: number): string {
  if (!atMs) return '—';
  const date = new Date(atMs);
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}
function shortId(value: string): string {
  return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-3)}` : value || '—';
}
function shortHash(value: string): string {
  if (!value) return '—';
  const clean = value.replace(/^sha256:/, '');
  return `sha256:${clean.slice(0, 4)}…${clean.slice(-4)}`;
}
function errorText(reason: unknown): string {
  const raw = reason instanceof Error && reason.message
    ? reason.message
    : typeof reason === 'string' && reason
      ? reason
      : '读取失败，请重试。';
  return publicAgentErrorText(reason, raw);
}

function traceUnavailableState(reason: string): TraceUnavailable {
  if (reason === 'session_not_resident') {
    return {
      message: '这段 Session 当前未驻留 Pi Runtime。重新打开或发送一条消息后，再查看 Agent 轨迹。',
      retryable: false,
    };
  }
  if (reason === 'runtime_unresponsive') {
    return {
      message: '轨迹快照读取超时，请重新读取。',
      retryable: true,
    };
  }
  return {
    message: '当前 Session 还没有可查看的上下文装配记录。',
    retryable: false,
  };
}

function traceErrorText(reason: unknown): string {
  const message = errorText(reason);
  if (message === 'runtime_unresponsive' || message === 'session_not_resident') {
    return traceUnavailableState(message).message;
  }
  return '读取 Agent 轨迹失败，请重试。';
}
