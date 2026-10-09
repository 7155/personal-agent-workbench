import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  ArrowLeft,
  Braces,
  Check,
  Clock3,
  Database,
  Download,
  FileJson2,
  Layers3,
  MessageSquareText,
  Play,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useControlTransport } from '@/app/control-transport';
import {
  Button,
  Disclosure,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  EmptyState,
  Select,
  Switch,
} from '@/components/primitives';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { MarkdownBody } from '@/features/agent/timeline/MarkdownRenderer';
import { publicErrorText } from '@/features/overview/management-ui';
import { usePawOsAppCompact, usePawOsAppIdentity } from '@/features/paw-os/surface-context';
import {
  formatJson,
  describeDebugTurn,
  messagePreview,
  normalizeDebugContextResponse,
  record,
  type DebugContextRecord,
  type DebugModelCall,
  type DebugToolBatch,
  type DebugToolExecution,
  type DebugTurnSummary,
} from './model';
import { buildContextDebugHtml } from './html-export';
import './context-debug.css';

export function ContextDebugFeature() {
  const transport = useControlTransport();
  const appSurface = usePawOsAppIdentity();
  const compact = usePawOsAppCompact();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSessionId = searchParams.get('sessionId') ?? '';
  const requestedTurnId = searchParams.get('turnId') ?? '';
  const [live, setLive] = useState(true);
  const [htmlPreviewUrls, setHtmlPreviewUrls] = useState<{ download: string; preview: string } | null>(null);
  const [refreshState, setRefreshState] = useState<'idle' | 'pending' | 'succeeded' | 'failed'>('idle');
  const htmlReportButtonRef = useRef<HTMLButtonElement>(null);
  const htmlReportDialogRef = useRef<HTMLDivElement>(null);
  const htmlReportFrameRef = useRef<HTMLIFrameElement>(null);
  const sessionsQuery = useQuery({
    queryKey: ['context-debug', 'sessions'],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.sessions.list',
      query: { includeArchived: true, includeInternal: true, limit: 500 },
      signal,
    }),
    staleTime: 5_000,
    retry: false,
  });
  const sessions = useMemo(() => sessionItems(sessionsQuery.data), [sessionsQuery.data]);
  const sessionId = requestedSessionId || sessions[0]?.id || '';
  const sessionOptions = useMemo(
    () => debugSessionOptions(sessions, requestedSessionId),
    [requestedSessionId, sessions],
  );

  useEffect(() => {
    if (requestedSessionId || !sessionId) return;
    const next = new URLSearchParams(searchParams);
    next.set('sessionId', sessionId);
    setSearchParams(next, { replace: true });
  }, [requestedSessionId, searchParams, sessionId, setSearchParams]);

  const contextQuery = useQuery({
    queryKey: ['context-debug', sessionId, requestedTurnId || 'latest'],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.session.debugContext.get',
      params: { sessionId },
      query: requestedTurnId ? { turnId: requestedTurnId } : {},
      signal,
    }),
    enabled: Boolean(sessionId),
    refetchInterval: live ? 1_000 : false,
    retry: false,
  });
  const response = useMemo(
    () => normalizeDebugContextResponse(contextQuery.data),
    [contextQuery.data],
  );
  const context = response.context;
  const unavailableContext = !contextQuery.isPending && sessionId && !response.available
    ? unavailableContextCopy(response.error)
    : null;

  useEffect(() => () => {
    if (!htmlPreviewUrls) return;
    URL.revokeObjectURL(htmlPreviewUrls.preview);
    URL.revokeObjectURL(htmlPreviewUrls.download);
  }, [htmlPreviewUrls]);

  useEffect(() => {
    if (!htmlPreviewUrls) return;
    function closeFromReport(event: MessageEvent): void {
      if (event.source !== htmlReportFrameRef.current?.contentWindow
        || !event.data || typeof event.data !== 'object'
        || event.data.type !== 'paw:context-report:close') return;
      setHtmlPreviewUrls(null);
    }
    window.addEventListener('message', closeFromReport);
    return () => window.removeEventListener('message', closeFromReport);
  }, [htmlPreviewUrls]);

  const selectedSessionTitle = sessions.find((session) => session.id === sessionId)?.title ?? '';

  function selectSession(nextSessionId: string): void {
    const next = new URLSearchParams(searchParams);
    next.set('sessionId', nextSessionId);
    next.delete('turnId');
    setSearchParams(next, { replace: true });
  }

  function selectTurn(nextTurnId: string): void {
    const next = new URLSearchParams(searchParams);
    if (nextTurnId) next.set('turnId', nextTurnId);
    else next.delete('turnId');
    setSearchParams(next, { replace: true });
  }

  async function refreshContext(): Promise<void> {
    setRefreshState('pending');
    try {
      const result = await contextQuery.refetch();
      setRefreshState(result.isError ? 'failed' : 'succeeded');
    } catch {
      setRefreshState('failed');
    }
  }

  function showHtmlPreview(): void {
    const generatedAtMs = Date.now();
    const options = {
      generatedAtMs,
      response,
      sessionTitle: selectedSessionTitle,
    };
    const downloadHtml = buildContextDebugHtml(options);
    const previewHtml = buildContextDebugHtml({
      ...options,
      reportScriptSrc: new URL('context-debug-report.js', document.baseURI).href,
    });
    setHtmlPreviewUrls({
      download: URL.createObjectURL(new Blob([downloadHtml], { type: 'text/html;charset=utf-8' })),
      preview: URL.createObjectURL(new Blob([previewHtml], { type: 'text/html;charset=utf-8' })),
    });
  }

  const Surface = appSurface ? 'section' : 'main';
  return (
    <>
    <Surface aria-label={appSurface ? '上下文检查' : undefined} className="context-debug-feature" data-paw-os-app={appSurface?.appId} data-paw-os-compact={compact || undefined} data-route-id="context-debug" role={appSurface ? 'region' : undefined}>
      <header className="context-debug-header" data-native-actions={appSurface ? true : undefined}>
        {appSurface ? <h1 className="mgmt-sr-only">上下文检查</h1> : (
          <div className="context-debug-heading">
            <span className="context-debug-heading__icon"><Braces size={18} /></span>
            <span>
              <h1>上下文检查</h1>
              <small><ShieldCheck size={12} />逐轮查看模型实际收到的上下文；技术载荷按需展开</small>
            </span>
          </div>
        )}
        <div className="context-debug-controls">
          <Select
            aria-label="选择对话"
            className="context-debug-select context-debug-select--session"
            disabled={!sessionOptions.length}
            onValueChange={selectSession}
            options={sessionOptions}
            placeholder="选择会话"
            value={sessionId}
          />
          <Switch
            checked={live}
            className="context-debug-live-switch"
            label={live ? '实时' : '暂停'}
            onCheckedChange={setLive}
          />
          <Button
            aria-label="刷新上下文"
            aria-live="polite"
            leadingIcon={<RefreshCw size={15} />}
            loading={refreshState === 'pending' || contextQuery.isFetching}
            onClick={() => void refreshContext()}
            size="small"
          >
            {refreshState === 'succeeded' ? '已刷新' : refreshState === 'failed' ? '刷新失败' : '刷新'}
          </Button>
          <Button
            aria-label="生成 HTML 报告"
            disabled={!context}
            leadingIcon={<FileJson2 size={15} />}
            onClick={showHtmlPreview}
            ref={htmlReportButtonRef}
            size="small"
            variant="quiet"
          >
            生成报告
          </Button>
          <a className="context-debug-back" href={sessionId ? `#/agent?sessionId=${encodeURIComponent(sessionId)}` : '#/agent'}>
            <ArrowLeft size={14} />对话
          </a>
        </div>
      </header>

      {sessionsQuery.error ? (
        <div className="context-debug-notice-bar">
          <DebugNotice tone="danger">{errorText(sessionsQuery.error)}</DebugNotice>
          <Button onClick={() => void sessionsQuery.refetch()} size="small">重新检查</Button>
        </div>
      ) : null}
      {contextQuery.error ? (
        <div className="context-debug-notice-bar">
          <DebugNotice tone="danger">{errorText(contextQuery.error)}</DebugNotice>
          <Button onClick={() => void refreshContext()} size="small">重新检查</Button>
        </div>
      ) : null}
      {!contextQuery.isPending && !contextQuery.error && sessionId && !response.available ? (
        <div className="context-debug-notice-bar">
          <DebugNotice tone="warning">
            {unavailableContext?.notice || ''}
          </DebugNotice>
          <a className="context-debug-notice-bar__link" href="#/configuration">打开设置</a>
        </div>
      ) : null}

      <ContextDebugDocument
        availableTurns={response.availableTurns}
        context={context}
        loading={contextQuery.isPending}
        onSelectTurn={selectTurn}
        returnToConversationHref={sessionId ? `#/agent?sessionId=${encodeURIComponent(sessionId)}` : '#/agent'}
        selectedTurnId={requestedTurnId || response.turnId || context?.turnId || ''}
        telemetry={response.telemetry}
        unavailable={unavailableContext}
      />
    </Surface>
    <Dialog
      onOpenChange={(open) => {
        if (!open) setHtmlPreviewUrls(null);
      }}
      open={Boolean(htmlPreviewUrls)}
    >
      <DialogContent
        className="context-debug-html-dialog"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          htmlReportButtonRef.current?.focus({ preventScroll: true });
        }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          htmlReportDialogRef.current?.focus({ preventScroll: true });
        }}
        ref={htmlReportDialogRef}
      >
        <DialogHeader>
          <DialogTitle>逐次上下文装配</DialogTitle>
          <DialogDescription>
            这是打开按钮时冻结的本机快照。每次模型调用会分别展示当时的上下文、消息、工具和请求结果；原始内容只在本页按需展开。
          </DialogDescription>
        </DialogHeader>
        {htmlPreviewUrls ? (
          <iframe
            ref={htmlReportFrameRef}
            sandbox="allow-scripts"
            src={htmlPreviewUrls.preview}
            title="逐次上下文报告"
          />
        ) : null}
        <footer className="context-debug-html-dialog__footer">
          <span>报告只在本机临时生成；关闭窗口后即释放。</span>
          {htmlPreviewUrls ? (
            <a download="agent-context-debug.html" href={htmlPreviewUrls.download}>
              <Download size={14} />
              下载报告
            </a>
          ) : null}
        </footer>
      </DialogContent>
    </Dialog>
    </>
  );
}

type ContextTreeFilter = 'all' | 'default' | 'no-tools' | 'user-only';

interface UnavailableContextCopy {
  title: string;
  notice: string;
  description: string;
}

function unavailableContextCopy(error: string): UnavailableContextCopy {
  if (error === 'session_not_resident') {
    return {
      title: '旧上下文未保留',
      notice: '这段对话已经结束或当前未驻留。',
      description: '原始上下文不会为查看而重新启动；若当时未开启本机快照，旧请求无法事后还原。',
    };
  }
  if (error === 'runtime_unresponsive') {
    return {
      title: '暂时无法读取快照',
      notice: '运行时没有在诊断等待时间内返回快照。',
      description: '对话本身不受影响；可以稍后刷新，或开启本机快照保留后续调用。',
    };
  }
  return {
    title: '当前快照不可用',
    notice: /[\u3400-\u9fff]/u.test(error)
      ? error
      : '当前回合没有可用的上下文快照。',
    description: '这里不会把未保留的旧请求误显示成一段空白对话。',
  };
}

interface ContextTreeEntry {
  depth: 0 | 1;
  id: string;
  kind: 'assistant' | 'call' | 'context' | 'tool' | 'user';
  label: string;
  preview: string;
}

function ContextDebugDocument({
  availableTurns,
  context,
  loading,
  onSelectTurn,
  returnToConversationHref,
  selectedTurnId,
  telemetry,
  unavailable,
}: {
  availableTurns: DebugTurnSummary[];
  context?: DebugContextRecord;
  loading: boolean;
  onSelectTurn: (turnId: string) => void;
  returnToConversationHref: string;
  selectedTurnId: string;
  telemetry: Record<string, unknown>;
  unavailable: UnavailableContextCopy | null;
}) {
  const [activeEntryId, setActiveEntryId] = useState('');
  const [treeFilter, setTreeFilter] = useState<ContextTreeFilter>('default');
  const [treeQuery, setTreeQuery] = useState('');
  const treeEntries = useMemo(() => context ? buildContextTree(context) : [], [context]);
  const visibleTreeEntries = useMemo(
    () => treeEntries.filter((entry) => contextTreeEntryVisible(entry, treeFilter, treeQuery)),
    [treeEntries, treeFilter, treeQuery],
  );
  const orderedTurns = useMemo(
    () => [...availableTurns].sort((left, right) => left.capturedAtMs - right.capturedAtMs),
    [availableTurns],
  );

  useEffect(() => {
    setActiveEntryId('');
  }, [context?.turnId]);

  if (!context?.modelCalls.length) {
    return (
      <div className="context-debug-reader__empty">
        <EmptyState
          action={!loading ? <a className="context-debug-empty-action" href={returnToConversationHref}>前往对话</a> : undefined}
          description={loading
            ? '正在读取运行上下文。'
            : unavailable?.description || '发送一条消息后，这里会按模型调用恢复实际上下文。'}
          icon={Database}
          title={loading ? '正在读取' : unavailable?.title || '暂无模型调用'}
        />
      </div>
    );
  }

  function revealEntry(id: string): void {
    setActiveEntryId(id);
    const target = document.getElementById(id);
    if (!target) return;
    // Directory entries can jump across nested scroll regions. Complete the
    // jump before moving focus so a second selection cannot strand focus offscreen.
    target.scrollIntoView?.({ behavior: 'auto', block: 'start' });
    target.focus({ preventScroll: true });
  }

  return (
    <section className="context-debug-session-viewer" aria-label="逐次上下文阅读">
      <aside className="context-debug-session-tree" aria-label="上下文目录">
        <TurnJourney
          currentContext={context}
          onSelectTurn={onSelectTurn}
          selectedTurnId={selectedTurnId || context.turnId}
          turns={orderedTurns}
        />
        <header>
          <label>
            <Search aria-hidden="true" size={13} />
            <input
              aria-label="搜索上下文条目"
              onChange={(event) => setTreeQuery(event.target.value)}
              placeholder="搜索消息或工具…"
              spellCheck={false}
              type="search"
              value={treeQuery}
            />
          </label>
          <div aria-label="上下文目录筛选">
            {([
              ['default', '默认'],
              ['no-tools', '无工具'],
              ['user-only', '用户'],
              ['all', '全部'],
            ] as const).map(([value, label]) => (
              <button
                aria-pressed={treeFilter === value}
                key={value}
                onClick={() => setTreeFilter(value)}
                type="button"
              >
                {label}
              </button>
            ))}
          </div>
        </header>
        <ol>
          {visibleTreeEntries.map((entry) => (
            <li data-depth={entry.depth} data-kind={entry.kind} key={entry.id}>
              <button
                aria-current={activeEntryId === entry.id ? 'location' : undefined}
                onClick={() => revealEntry(entry.id)}
                type="button"
              >
                <span aria-hidden="true">{treeMarker(entry)}</span>
                <strong>{entry.label}</strong>
                <small>{entry.preview}</small>
              </button>
            </li>
          ))}
        </ol>
        <footer>{visibleTreeEntries.length} / {treeEntries.length} 条 · {context.modelCalls.length} 次调用</footer>
      </aside>

      <article className="context-debug-session-content">
        <ContextAssemblyOverview context={context} telemetry={telemetry} turns={orderedTurns} />

        <div className="context-debug-session-preamble">
          <ContextDisclosure
            label="系统指令"
            meta={`${context.systemPrompt.length.toLocaleString('zh-CN')} 字符`}
            tone="system"
          >
            <pre className="context-debug-reader__code context-debug-reader__code--text">
              {context.systemPrompt || '没有捕获系统指令'}
            </pre>
          </ContextDisclosure>
          <ContextDisclosure
            label={`可用工具（${context.activeTools.length}）`}
            meta={context.activeTools.slice(0, 6).join(' · ') || '没有启用工具'}
            tone="tools"
          >
            <div className="context-debug-session-tool-chips">
              {context.activeTools.map((tool) => <span key={tool}>{tool}</span>)}
            </div>
            <pre className="context-debug-reader__code">{formatJson(context.toolSchemas)}</pre>
          </ContextDisclosure>
        </div>

        <div className="context-debug-session-messages">
          {context.modelCalls.map((call) => (
            <ContextCallDocument call={call} context={context} key={call.index} />
          ))}
        </div>

        <footer className="context-debug-session-footer">
          <ContextDisclosure
            label="本轮原始输入"
            meta={`${context.prompt.length.toLocaleString('zh-CN')} 字符`}
            tone="messages"
          >
            <pre className="context-debug-reader__code context-debug-reader__code--text">
              {context.prompt || '没有捕获本轮原始输入'}
            </pre>
          </ContextDisclosure>
          <ContextDisclosure label="本回合原始记录" meta="逐字段核对" tone="raw">
            <pre className="context-debug-reader__code">{formatJson(context.raw)}</pre>
          </ContextDisclosure>
        </footer>
      </article>
    </section>
  );
}

function TurnJourney({
  currentContext,
  onSelectTurn,
  selectedTurnId,
  turns,
}: {
  currentContext: DebugContextRecord;
  onSelectTurn: (turnId: string) => void;
  selectedTurnId: string;
  turns: DebugTurnSummary[];
}) {
  const selectedButtonRef = useRef<HTMLButtonElement>(null);
  const selectedDescription = describeDebugTurn(currentContext, turns);
  useEffect(() => {
    selectedButtonRef.current?.scrollIntoView?.({ block: 'nearest' });
  }, [selectedTurnId]);
  return (
    <nav className="context-debug-turns" aria-label="对话轮次">
      <header>
        <span><Clock3 aria-hidden="true" size={15} /><strong>轮次轨迹</strong></span>
        <small>本机保留 {turns.length} 轮</small>
      </header>
      <ol>
        {turns.map((turn, index) => {
          const label = turn.turnId === currentContext.turnId
            ? selectedDescription.label
            : turnSummaryLabel(turn, index);
          return (
            <li data-phase={turn.assemblyPhase ?? 'unknown'} key={turn.turnId}>
              <button
                aria-current={selectedTurnId === turn.turnId ? 'step' : undefined}
                onClick={() => onSelectTurn(turn.turnId)}
                ref={selectedTurnId === turn.turnId ? selectedButtonRef : undefined}
                type="button"
              >
                <span className="context-debug-turns__ordinal">
                  {String(turn.turnOrdinal ?? index + 1).padStart(2, '0')}
                </span>
                <span className="context-debug-turns__copy">
                  <span><strong>{label}</strong><time>{formatTimestamp(turn.capturedAtMs)}</time></span>
                  <small>{turn.summary || `${turn.modelCallCount} 次模型调用 · ${turn.toolCallCount} 次工具`}</small>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      <p>这里是可核对的保留窗口；轮换掉的旧轮次不会被伪装成仍然可读。</p>
    </nav>
  );
}

function ContextAssemblyOverview({
  context,
  telemetry,
  turns,
}: {
  context: DebugContextRecord;
  telemetry: Record<string, unknown>;
  turns: DebugTurnSummary[];
}) {
  const description = describeDebugTurn(context, turns);
  const currentIndex = Math.max(0, turns.findIndex((turn) => turn.turnId === context.turnId));
  const latestCall = context.modelCalls.at(-1);
  const messageChanges = context.modelCalls.reduce(
    (total, call) => ({
      added: total.added + call.contextDelta.addedMessageCount,
      removed: total.removed + call.contextDelta.removedMessageCount,
    }),
    { added: 0, removed: 0 },
  );
  const providerMessages = latestCall ? callProviderMessages(latestCall) : [];
  const messagesOmitted = latestCall ? callMessagesOmitted(latestCall) : false;
  const telemetryContext = record(telemetry.context);
  const tokenCount = projectedNumber(telemetryContext, ['tokens']);
  const layers = contextAssemblyLayers(context, providerMessages, description.phase, messagesOmitted);
  return (
    <header className="context-debug-assembly" data-phase={description.phase}>
      <div className="context-debug-assembly__title">
        <span className="context-debug-assembly__mark"><Sparkles aria-hidden="true" size={17} /></span>
        <span>
          <small>第 {String(description.ordinal ?? currentIndex + 1).padStart(2, '0')} 轮 · 上下文组成</small>
          <h2>{description.label}</h2>
          <p>{description.description}</p>
        </span>
        <em>{formatTimestamp(context.capturedAtMs)}</em>
      </div>
      <dl className="context-debug-assembly__metrics">
        <div><dt>模型</dt><dd>{modelLabel(context.model)}</dd></div>
        <div><dt>最终消息</dt><dd>{messagesOmitted ? '正文已省略' : `${providerMessages.length} 条`}</dd></div>
        <div><dt>本轮调用</dt><dd>{context.modelCalls.length} 次</dd></div>
        <div><dt>上下文用量</dt><dd>{tokenCount === undefined ? '未报告' : `${tokenCount.toLocaleString('zh-CN')} 词元`}</dd></div>
        <div><dt>累计变化</dt><dd>+{messageChanges.added} / -{messageChanges.removed}</dd></div>
      </dl>
      <ol className="context-debug-assembly__layers" aria-label="本轮上下文装配顺序">
        {layers.map((layer, index) => (
          <li data-channel={layer.channel} key={layer.label}>
            <span>{String(index + 1).padStart(2, '0')}</span>
            <div><strong>{layer.label}</strong><small>{layer.detail}</small></div>
            <em>{layer.meta}</em>
          </li>
        ))}
      </ol>
    </header>
  );
}

function contextAssemblyLayers(
  context: DebugContextRecord,
  providerMessages: unknown[],
  phase: ReturnType<typeof describeDebugTurn>['phase'],
  messagesOmitted = false,
): Array<{ label: string; detail: string; meta: string; channel: string }> {
  const options = record(context.systemPromptOptions);
  const skills = Array.isArray(options.skills) ? options.skills : [];
  const injectedMessages = providerMessages.filter((message) => Boolean(record(message).customType));
  const dialogueMessages = providerMessages.filter((message) => !record(message).customType);
  const injectedLabels = [...new Set(injectedMessages.map((message) => (
    customMessageLabel(String(record(message).customType ?? ''))
  )))].filter(Boolean);
  const prioritizedInjectedLabels = phase === 'compaction_recovery'
    ? [...injectedLabels].sort((left, right) => (
        Number(['压缩恢复', '协作空间恢复', 'Room 恢复'].includes(right))
        - Number(['压缩恢复', '协作空间恢复', 'Room 恢复'].includes(left))
      ))
    : injectedLabels;
  return [
    {
      channel: 'system',
      label: '系统与项目指令',
      detail: context.systemPrompt ? '角色、边界和项目级约束' : '本轮未捕获系统指令',
      meta: `${context.systemPrompt.length.toLocaleString('zh-CN')} 字符`,
    },
    {
      channel: 'tools',
      label: '能力与工具',
      detail: skills.length ? skills.map((skill) => String(record(skill).name ?? '未命名能力')).slice(0, 3).join(' · ') : '按本轮权限声明能力',
      meta: `${skills.length} 项能力 · ${context.activeTools.length} 个工具`,
    },
    {
      channel: 'runtime',
      label: phase === 'compaction_recovery' ? '压缩恢复与运行时' : '项目与运行时上下文',
      detail: messagesOmitted ? '消息正文已省略，无法核对补充内容' : prioritizedInjectedLabels.slice(0, 3).join(' · ') || '本轮没有额外补充',
      meta: messagesOmitted ? '已省略' : `${injectedMessages.length} 条补充内容`,
    },
    {
      channel: 'history',
      label: '模型服务消息',
      detail: messagesOmitted ? '消息正文已省略，不能据此认定没有对话消息' : dialogueMessages.length ? '历史与本轮消息按最终发送顺序保留' : '本轮没有对话消息',
      meta: messagesOmitted ? '已省略' : `${dialogueMessages.length} 条消息`,
    },
    {
      channel: 'input',
      label: '当前用户输入',
      detail: compactTreePreview(publicPromptSummary(context.prompt)),
      meta: `${context.prompt.length.toLocaleString('zh-CN')} 字符`,
    },
  ];
}

function turnSummaryLabel(turn: DebugTurnSummary, index: number): string {
  if (turn.assemblyPhase === 'initial' || turn.turnOrdinal === 1) return '首轮装配';
  if (turn.assemblyPhase === 'compaction_recovery') return '压缩后恢复';
  if (turn.assemblyPhase === 'incremental') return '增量装配';
  return index === 0 ? '最早保留轮次' : '对话轮次';
}

function ContextCallDocument({ call, context }: { call: DebugModelCall; context: DebugContextRecord }) {
  const tools = context.toolExecutions.filter((tool) => tool.modelCallIndex === call.index);
  const batches = context.toolBatches.filter((batch) => batch.modelCallIndex === call.index);
  const providerMessages = callProviderMessages(call);
  const messagesOmitted = callMessagesOmitted(call);
  const titleId = `context-debug-call-${call.index}-title`;
  return (
    <section
      aria-labelledby={titleId}
      className="context-debug-session-call"
      id={callEntryId(call.index)}
      tabIndex={-1}
    >
      <header className="context-debug-session-call__marker">
        <h3 id={titleId}>第 <b>{call.index}</b> 次模型调用</h3>
        <span>{call.completedAtMs ? '已完成' : '进行中'}</span>
        <time>{formatTimestamp(call.capturedAtMs)}</time>
        <small>
          {call.contextDelta.baseCallIndex ? `基于第 ${call.contextDelta.baseCallIndex} 次调用` : '首次上下文'} · +{call.contextDelta.addedMessageCount} / -{call.contextDelta.removedMessageCount}
        </small>
      </header>

      {!hasExactProviderContext(call) ? (
        <p className="context-debug-session-call__warning">
          历史快照：系统指令与工具定义来自整轮记录，并非本次请求的精确证据。
        </p>
      ) : null}

      {call.contextDelta.omitted ? (
        <p className="context-debug-session-call__warning">新增消息正文已省略；记录新增 {call.contextDelta.addedMessageCount} 条。</p>
      ) : <ReadableMessageList
        callIndex={call.index}
        emptyLabel="本次调用没有新增消息"
        messages={call.contextDelta.addedMessages}
        sessionId={context.sessionId}
      />}

      {bodyOmitted(call.assistantMessage) ? (
        <p className="context-debug-session-call__warning" id={assistantEntryId(call.index, 0)} tabIndex={-1}>模型回复正文已省略。</p>
      ) : call.assistantMessage !== undefined ? (
        <ReadableMessageList
          callIndex={call.index}
          emptyLabel="没有捕获模型回复"
          messageId={(index) => assistantEntryId(call.index, index)}
          messages={[call.assistantMessage]}
          sessionId={context.sessionId}
        />
      ) : null}

      {batches.length || tools.length ? <ContextCallTools batches={batches} tools={tools} /> : null}

      <div className="context-debug-session-call__request">
        <ContextDisclosure
          label="请求详情"
          meta={`${messagesOmitted ? '消息正文已省略' : `${providerMessages.length} 条消息`} · ${callProviderTools(call, context).length} 个工具 · ${call.providerExchanges.length} 次模型服务尝试`}
          tone="provider"
        >
          <dl className="context-debug-session-delta" aria-label={`模型调用 ${call.index} 上下文变化`}>
            <div><dt>共同前缀</dt><dd>{call.contextDelta.commonPrefixMessages}</dd></div>
            <div><dt>新增</dt><dd>+{call.contextDelta.addedMessageCount}</dd></div>
            <div><dt>移除</dt><dd>-{call.contextDelta.removedMessageCount}</dd></div>
            <div><dt>耗时</dt><dd>{durationLabel(call.capturedAtMs, call.completedAtMs)}</dd></div>
          </dl>
          <ContextDisclosure label="系统指令" meta={`${callProviderSystemPrompt(call, context).length} 字符`} tone="system">
            <pre className="context-debug-reader__code context-debug-reader__code--text">{callProviderSystemPrompt(call, context) || '没有捕获系统指令'}</pre>
          </ContextDisclosure>
          <ContextDisclosure label={messagesOmitted ? '消息正文（已省略）' : '完整消息'} meta={messagesOmitted ? '正文不可用' : `${providerMessages.length} 条`} tone="messages">
            {messagesOmitted ? <p className="context-debug-session-call__warning">本次调用的上下文正文已省略，不能据此认定为空。</p>
              : <ReadableMessageList callIndex={call.index} emptyLabel="当前调用上下文为空" messages={providerMessages} nested sessionId={context.sessionId} />}
          </ContextDisclosure>
          <ContextDisclosure label="工具定义" meta={`${callProviderTools(call, context).length} 个`} tone="tools">
            <pre className="context-debug-reader__code">{formatJson(callProviderTools(call, context))}</pre>
          </ContextDisclosure>
          <ContextDisclosure label="模型服务交互" meta={`${call.providerExchanges.length} 次`} tone="provider">
            {call.providerExchanges.some((exchange) => bodyOmitted(exchange.payload)) ? <p>模型服务请求正文已省略；尝试状态与回执仍保留。</p> : null}
            <pre className="context-debug-reader__code">{formatJson(call.providerExchanges)}</pre>
          </ContextDisclosure>
          <ContextDisclosure label="原始调用记录" meta="逐字段核对" tone="raw">
            <pre className="context-debug-reader__code">{formatJson(call)}</pre>
          </ContextDisclosure>
        </ContextDisclosure>
      </div>
    </section>
  );
}

function ReadableMessageList({
  callIndex,
  emptyLabel,
  messageId,
  messages,
  nested = false,
  sessionId,
}: {
  callIndex: number;
  emptyLabel: string;
  messageId?: (index: number) => string;
  messages: unknown[];
  nested?: boolean;
  sessionId: string;
}) {
  if (!messages.length) return <p className="context-debug-reader__empty-copy">{emptyLabel}</p>;
  return (
    <ol className="context-debug-session-message-list" data-nested={nested || undefined}>
      {messages.map((message, index) => {
        const item = record(message);
        const role = String(item.role ?? 'message').toLowerCase();
        const customType = String(item.customType ?? '');
        const body = publicMessageContent(message, customType);
        const id = messageId?.(index) ?? messageEntryId(callIndex, index);
        return (
          <li
            data-custom={customType || undefined}
            data-role={customType ? 'context' : role}
            id={nested ? undefined : id}
            key={`${role}:${customType}:${index}`}
            tabIndex={nested ? undefined : -1}
          >
            <header>
              <strong>{customType ? customMessageLabel(customType) : contextMessageRoleLabel(role)}</strong>
              <small>#{index + 1}</small>
            </header>
            <div className="context-debug-session-message__body">
              {customType ? body : <MarkdownBody sessionId={sessionId} text={body} />}
            </div>
            <ContextDisclosure label="原始消息" meta="逐字段核对" tone="raw">
              <pre className="context-debug-reader__code">{formatJson(message)}</pre>
            </ContextDisclosure>
          </li>
        );
      })}
    </ol>
  );
}

function ContextCallTools({ batches, tools }: { batches: DebugToolBatch[]; tools: DebugToolExecution[] }) {
  const toolsById = new Map(tools.map((tool) => [tool.toolCallId, tool]));
  const visibleBatches = batches.length ? batches : tools.map((tool, index) => ({
    id: `tool:${tool.toolCallId}`,
    modelCallIndex: tool.modelCallIndex,
    runtimeTurnIndex: tool.runtimeTurnIndex,
    stage: index + 1,
    executionMode: 'serial' as const,
    startedAtMs: tool.startedAtMs,
    endedAtMs: tool.endedAtMs,
    status: tool.status,
    toolCallIds: [tool.toolCallId],
  }));
  return (
    <div className="context-debug-session-tools">
      {visibleBatches.map((batch) => (
        <section data-mode={batch.executionMode} key={batch.id}>
          <header>
            <span>第 {batch.stage} 阶段</span>
            <strong>{batch.executionMode === 'parallel' ? `并行 · ${batch.toolCallIds.length} 个工具` : '串行'}</strong>
            <small>{durationLabel(batch.startedAtMs, batch.endedAtMs)}</small>
          </header>
          <div>
            {batch.toolCallIds.map((toolCallId) => {
              const tool = toolsById.get(toolCallId);
              return tool ? <ToolExecutionDetails entryId={toolEntryId(toolCallId)} key={toolCallId} tool={tool} /> : null;
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

function buildContextTree(context: DebugContextRecord): ContextTreeEntry[] {
  return context.modelCalls.flatMap((call) => {
    const entries: ContextTreeEntry[] = [{
      depth: 0,
      id: callEntryId(call.index),
      kind: 'call',
      label: `第 ${call.index} 次模型调用`,
      preview: `+${call.contextDelta.addedMessageCount} · ${formatTimestamp(call.capturedAtMs)}`,
    }];
    call.contextDelta.addedMessages.forEach((message, index) => {
      const item = record(message);
      const role = String(item.role ?? 'message').toLowerCase();
      const customType = String(item.customType ?? '');
      entries.push({
        depth: 1,
        id: messageEntryId(call.index, index),
        kind: customType ? 'context' : role === 'user' ? 'user' : role === 'assistant' ? 'assistant' : role.includes('tool') ? 'tool' : 'context',
        label: customType ? customMessageLabel(customType) : contextMessageRoleLabel(role),
        preview: compactTreePreview(publicMessageContent(message, customType)),
      });
    });
    if (call.assistantMessage !== undefined) {
      entries.push({
        depth: 1,
        id: assistantEntryId(call.index, 0),
        kind: 'assistant',
        label: '助手',
        preview: bodyOmitted(call.assistantMessage) ? '模型回复正文已省略' : compactTreePreview(readableMessageContent(call.assistantMessage)),
      });
    }
    context.toolExecutions
      .filter((tool) => tool.modelCallIndex === call.index)
      .forEach((tool) => entries.push({
        depth: 1,
        id: toolEntryId(tool.toolCallId),
        kind: 'tool',
        label: tool.toolName,
        preview: compactTreePreview(messagePreview(tool.args) || shortId(tool.toolCallId)),
      }));
    return entries;
  });
}

function contextTreeEntryVisible(entry: ContextTreeEntry, filter: ContextTreeFilter, query: string): boolean {
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN');
  if (normalizedQuery && !`${entry.label} ${entry.preview}`.toLocaleLowerCase('zh-CN').includes(normalizedQuery)) return false;
  if (entry.kind === 'call') return filter !== 'user-only' || Boolean(normalizedQuery);
  if (filter === 'default') return entry.kind !== 'context';
  if (filter === 'no-tools') return entry.kind !== 'context' && entry.kind !== 'tool';
  if (filter === 'user-only') return entry.kind === 'user';
  return true;
}

function treeMarker(entry: ContextTreeEntry): string {
  if (entry.kind === 'call') return '◆';
  if (entry.kind === 'user') return '›';
  if (entry.kind === 'assistant') return '•';
  if (entry.kind === 'tool') return '$';
  return '·';
}

function compactTreePreview(value: string): string {
  const compact = value.replace(/\s+/gu, ' ').trim();
  return compact.length > 54 ? `${compact.slice(0, 51)}…` : compact || '结构化消息';
}

function publicPromptSummary(value: string): string {
  return value
    .replace(/<agent-user-query>([\s\S]*?)<\/agent-user-query>/giu, '$1')
    .trim();
}

function callEntryId(callIndex: number): string {
  return `context-call-${callIndex}`;
}

function messageEntryId(callIndex: number, messageIndex: number): string {
  return `context-call-${callIndex}-message-${messageIndex + 1}`;
}

function assistantEntryId(callIndex: number, messageIndex: number): string {
  return `context-call-${callIndex}-assistant-${messageIndex + 1}`;
}

function toolEntryId(toolCallId: string): string {
  return `context-tool-${toolCallId.replace(/[^A-Za-z0-9_-]/gu, '-')}`;
}

function ContextDisclosure({
  children,
  label,
  meta,
  tone,
}: {
  children: React.ReactNode;
  label: string;
  meta: string;
  tone: 'messages' | 'provider' | 'raw' | 'system' | 'tools';
}) {
  return (
    <Disclosure
      className="context-debug-reader__disclosure"
      data-tone={tone}
      revealClassName="context-debug-reader__reveal"
      summary={<span><strong>{label}</strong><small>{meta}</small></span>}
    >
      {children}
    </Disclosure>
  );
}

function readableMessageContent(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(readableMessageContent).filter(Boolean).join('\n\n');
  const item = record(value);
  const content = item.content;
  if (content !== undefined && content !== value) {
    const rendered = readableMessageContent(content);
    if (rendered) return rendered;
  }
  const direct = [item.text, item.input_text, item.output_text, item.thinking]
    .find((candidate) => typeof candidate === 'string' && candidate.trim());
  if (typeof direct === 'string') return direct;
  const type = String(item.type ?? '').toLowerCase();
  const name = String(item.name ?? record(item.function).name ?? '').trim();
  if (type.includes('tool') || type.includes('function') || name) {
    const args = item.arguments ?? record(item.function).arguments;
    return [`调用工具：${name || '未命名工具'}`, args === undefined ? '' : `参数：${typeof args === 'string' ? args : formatJson(args)}`]
      .filter(Boolean)
      .join('\n');
  }
  return messagePreview(value) || formatJson(value);
}

function publicMessageContent(value: unknown, customType = ''): string {
  const content = readableMessageContent(value);
  const withoutMarkup = content.replace(
    /<rag-ime-context\b([^>]*)>([\s\S]*?)<\/rag-ime-context>/giu,
    (_match, attributes: string, inner: string) => {
      const type = attributes.match(/\btype\s*=\s*["']([^"']+)["']/iu)?.[1]?.toLowerCase() ?? '';
      const body = inner.trim();
      if (type === 'memory_recall' || customType === 'rag-ime-memory-recall') {
        const reference = body.replace(/^已召回\s*[：:]\s*/u, '').trim();
        return reference ? `记忆参考：${reference}` : '本轮没有可用的记忆参考。';
      }
      return body;
    },
  ).replace(
    /<(execution-mode|work-state|workflow-state|lifecycle-hook|turn-context|compaction-recovery|room-compaction-recovery)\b[^>]*>([\s\S]*?)<\/\1>/giu,
    (_match, wrapper: string, inner: string) => publicKnownContextWrapper(wrapper, inner),
  ).trim();
  if (customType === 'rag-ime-memory-recall') {
    return publicRuntimeTerms(withoutMarkup.replace(/^已召回\s*[：:]\s*/u, '记忆参考：'));
  }
  return publicRuntimeTerms(withoutMarkup);
}

function publicKnownContextWrapper(wrapper: string, value: string): string {
  const body = publicRuntimeTerms(value.trim());
  if (wrapper === 'execution-mode') {
    return /^已授权操作直接执行[；;]\s*硬安全边界继续生效[。.]?$/u.test(body)
      ? '已允许直接执行常规操作；涉及安全边界的操作仍会受保护。'
      : body;
  }
  if (wrapper === 'work-state') {
    return body.replace(/核对\s*模型服务\s*上下文顺序/u, '核对模型服务接收上下文的顺序');
  }
  if (wrapper === 'workflow-state') {
    return /^待办任务正在执行[；;]\s*完成后提交验证回执[。.]?$/u.test(body)
      ? '当前任务正在进行；完成后会记录验证结果。'
      : body;
  }
  if (wrapper === 'lifecycle-hook') {
    return /^对话\s*已启动[；;]\s*压缩后刷新一次上下文[。.]?$/u.test(body)
      ? '对话已开始；完成上下文整理后会刷新本轮内容。'
      : body;
  }
  if (wrapper === 'turn-context') return body;
  return body
    .replace(/原始愿景/gu, '原始目标')
    .replace(/已确认\s*项目\/协作空间\s*边界与当前实现进度/u, '项目与协作空间的范围和当前进度已确认')
    .replace(/继续核对\s*模型服务\s*装配证据/u, '接下来核对模型服务接收的内容');
}

function publicRuntimeTerms(value: string): string {
  return value
    .replace(/\s*\bProject\s*\/\s*Room\b\s*/giu, '项目/协作空间')
    .replace(/\s*\bProvider\b\s*/giu, '模型服务')
    .replace(/\s*\bSession\b\s*/giu, '对话')
    .replace(/\s*\bProject\b\s*/giu, '项目')
    .replace(/\s*\bRoom\b\s*/giu, '协作空间')
    .replace(/\s*\bAgent\b\s*/giu, '伙伴')
    .replace(/\s*\bTodo\b\s*/giu, '待办任务');
}

function customMessageLabel(value: string): string {
  const labels: Record<string, string> = {
    'rag-ime-execution-mode': '执行模式',
    'rag-ime-memory-recall': '记忆参考',
    'rag-ime-work-state': '工作状态',
    'rag-ime-compaction-recovery': '压缩恢复',
    'rag-ime-room-compaction-recovery': '协作空间恢复',
    'rag-ime-session-context': '对话上下文',
    'rag-ime-workflow': '工作流',
    'rag-ime-lifecycle': '生命周期',
    'rag-ime-turn-context': '本轮上下文',
  };
  return labels[value] ?? value;
}

function contextMessageRoleLabel(value: string): string {
  const labels: Record<string, string> = {
    assistant: '助手',
    developer: '系统',
    system: '系统',
    tool: '工具',
    toolresult: '工具结果',
    user: '用户',
  };
  return labels[value.toLowerCase()] ?? '消息';
}

function ContextXraySummary({ call, context, telemetry }: { call?: DebugModelCall; context?: DebugContextRecord; telemetry: Record<string, unknown> }) {
  const raw = record(context?.raw);
  const projection = record(raw.contextProjection ?? raw.contextAssembly ?? telemetry.contextProjection);
  const usage = record(telemetry.cumulativeUsage ?? telemetry.usage ?? raw.usage);
  const cacheEvidence = context?.cacheEvidence.at(-1);
  const stablePrefix = projectedNumber(projection, ['stablePrefixMessages', 'stablePrefixCount']) ?? call?.contextDelta.commonPrefixMessages;
  const dynamicTail = projectedNumber(projection, ['dynamicTailMessages', 'dynamicTailCount']) ?? call?.contextDelta.addedMessageCount;
  const sealed = projectedNumber(projection, ['sealedMessages', 'sealedCount']);
  const pending = projectedNumber(projection, ['pendingMessages', 'pendingCount']);
  const cacheRead = cacheEvidence?.capability === 'reported' ? cacheEvidence.cacheReadTokens : projectedNumber(usage, ['cacheRead', 'cache_read_input_tokens']);
  const cacheWrite = cacheEvidence?.capability === 'reported' ? cacheEvidence.cacheWriteTokens : projectedNumber(usage, ['cacheWrite', 'cache_creation_input_tokens']);
  const compaction = projectedText(projection, ['compactionState', 'compaction', 'lastCompactionStatus']);
  const recovery = projectedText(projection, ['recoveryState', 'recovery', 'bootstrapRecoveryState']);
  return <section className="context-xray-summary" aria-label="对话上下文结构">
    <header><span><Layers3 size={16} /><strong>上下文组成</strong></span><small>{context ? `回合 ${shortId(context.turnId)}` : '等待运行状态'}</small></header>
    <dl>
      <XrayMetric label="稳定前缀" value={stablePrefix} suffix="条" />
      <XrayMetric label="本轮新增" value={dynamicTail} suffix="条" />
      <XrayMetric label="已封存" value={sealed} suffix="条" />
      <XrayMetric label="待处理" value={pending} suffix="条" />
      <XrayMetric label="缓存读取" value={cacheRead} suffix="词元" />
      <XrayMetric label="缓存写入" value={cacheWrite} suffix="词元" />
    </dl>
    <div className="context-xray-summary__states"><span><strong>缓存状态</strong><small>{cacheEvidence ? cacheEvidence.capability === 'unsupported' ? '模型服务未报告' : cacheEvidence.cacheReadTokens > 0 ? '可用 · 已命中' : '可用 · 未命中' : '尚未收到状态'}</small></span><span><strong>压缩与恢复</strong><small>{compaction || '未报告'} / {recovery || '未报告'}</small></span><span><strong>本轮变化</strong><small>{call ? `共同前缀 ${call.contextDelta.commonPrefixMessages} 条${call.contextDelta.prefixBytes !== undefined ? ` / ${call.contextDelta.prefixBytes} 字节` : ''} · 新增 ${call.contextDelta.addedMessageCount} 条 · 移除 ${call.contextDelta.removedMessageCount} 条` : '暂无模型调用'}</small></span></div>
    <p>正文默认隐藏。打开下方逐次上下文后，可按发生顺序阅读；完整请求和原始结构仍需就地展开。</p>
  </section>;
}

function XrayMetric({ label, suffix, value }: { label: string; suffix: string; value?: number }) {
  return <div data-available={value !== undefined}><dt>{label}</dt><dd>{value === undefined ? '未投影' : `${value.toLocaleString('zh-CN')} ${suffix}`}</dd></div>;
}

function projectedNumber(source: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) if (typeof source[key] === 'number' && Number.isFinite(source[key])) return source[key] as number;
  return undefined;
}

function projectedText(source: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) if (typeof source[key] === 'string' && source[key]) return source[key] as string;
  return '';
}

function ToolExecutionDetails({ entryId, tool }: { entryId?: string; tool: DebugToolExecution }) {
  return (
    <Disclosure
      className="context-debug-tool"
      contentClassName="context-debug-tool__payloads"
      defaultOpen={tool.status !== 'completed'}
      id={entryId}
      revealClassName="context-debug-tool__reveal"
      summary={<>
        <span className="context-debug-tool__state" data-status={tool.status}>
          {tool.status === 'running' ? <Play size={12} /> : tool.status === 'failed' ? <Activity size={12} /> : <Check size={12} />}
        </span>
        <span><strong>{tool.toolName}</strong><small>{compactTreePreview(messagePreview(tool.args) || shortId(tool.toolCallId))}</small></span>
        <em>{durationLabel(tool.startedAtMs, tool.endedAtMs)}</em>
      </>}
      tabIndex={entryId ? -1 : undefined}
    >
      <section><h4>参数</h4>{bodyOmitted(tool.args) ? <p>工具参数正文已省略。</p> : null}<pre>{formatJson(tool.args)}</pre></section>
      <section><h4>结果</h4>{bodyOmitted(tool.result) ? <p>工具结果正文已省略。</p> : null}<pre>{formatJson(tool.result ?? null)}</pre></section>
      {tool.updates.length ? <section><h4>过程更新</h4>{tool.updates.some((update) => bodyOmitted(update.partialResult)) ? <p>工具过程正文已省略；更新记录仍保留。</p> : null}<pre>{formatJson(tool.updates)}</pre></section> : null}
    </Disclosure>
  );
}

function DebugNotice({ children, tone }: { children: string; tone: 'danger' | 'warning' }) {
  return <div className="context-debug-notice" data-tone={tone}>{children}</div>;
}

function debugSessionOptions(sessions: SessionSummary[], requestedSessionId: string) {
  const titleCounts = new Map<string, number>();
  sessions.forEach((session) => {
    const title = session.title || '未命名会话';
    titleCounts.set(title, (titleCounts.get(title) ?? 0) + 1);
  });
  const options = sessions.map((session) => {
    const title = session.title || '未命名会话';
    return {
      value: session.id,
      label: titleCounts.get(title) === 1 ? title : `${title} · ${formatTimestamp(session.updatedAtMs)}`,
    };
  });
  if (requestedSessionId && !sessions.some((session) => session.id === requestedSessionId)) {
    options.unshift({ value: requestedSessionId, label: '指定会话' });
  }
  return options;
}

function modelLabel(model: Record<string, unknown>): string {
  const provider = String(model.provider ?? '').trim();
  const name = String(model.name ?? model.id ?? '').trim();
  return [provider, name].filter(Boolean).join(' / ') || '待捕获';
}

function callCaption(call: DebugModelCall): string {
  const runtime = call.runtimeTurnIndex === undefined ? '' : `处理步骤 ${call.runtimeTurnIndex + 1}`;
  const attempts = `${call.providerExchanges.length} 次模型服务尝试`;
  return [runtime, attempts, `${callProviderMessages(call).length} 条消息`].filter(Boolean).join(' · ');
}

function callProviderMessages(call: DebugModelCall): unknown[] {
  if (!Object.hasOwn(call.providerContext, 'messages')) return call.contextMessages;
  return Array.isArray(call.providerContext.messages) ? call.providerContext.messages : [];
}

function bodyOmitted(value: unknown): boolean {
  return record(value).omitted === true;
}

function callMessagesOmitted(call: DebugModelCall): boolean {
  if (bodyOmitted(call.providerContext)) return true;
  return Object.hasOwn(call.providerContext, 'messages')
    ? bodyOmitted(call.providerContext.messages)
    : Boolean(call.contextMessagesOmission);
}

function callProviderSystemPrompt(call: DebugModelCall, context: DebugContextRecord): string {
  return typeof call.providerContext.systemPrompt === 'string'
    ? call.providerContext.systemPrompt
    : context.systemPrompt;
}

function callProviderTools(call: DebugModelCall, context: DebugContextRecord): unknown[] {
  if (!Object.hasOwn(call.providerContext, 'tools')) return context.toolSchemas;
  return Array.isArray(call.providerContext.tools) ? call.providerContext.tools : [];
}

function hasExactProviderContext(call: DebugModelCall): boolean {
  return !bodyOmitted(call.providerContext) && Object.keys(call.providerContext).length > 0;
}

function durationLabel(startedAtMs: number, endedAtMs?: number): string {
  if (!startedAtMs) return endedAtMs ? formatTimestamp(endedAtMs) : '时间未知';
  if (!endedAtMs) return '运行中';
  return `${Math.max(0, endedAtMs - startedAtMs)} ms`;
}

function formatTimestamp(value: number): string {
  if (!value) return '待捕获';
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value));
}

function shortId(value: string): string {
  if (value.length <= 22) return value;
  return `${value.slice(0, 9)}…${value.slice(-7)}`;
}

function errorText(error: unknown): string {
  return publicErrorText(error, '无法读取本机上下文记录');
}
