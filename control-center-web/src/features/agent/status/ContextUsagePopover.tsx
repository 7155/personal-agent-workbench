import { X } from 'lucide-react';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import {
  Popover,
  PopoverClose,
  PopoverContent,
  PopoverTrigger,
} from '@/components/primitives';
import { normalizeDebugContextResponse } from '@/features/context-debug/model';
import { buildContextXraySnapshot, type ContextXraySnapshot } from './ContextXrayPanel';
import {
  buildContextUsageView,
  formatContextTokenCount,
  type ContextUsageView,
} from './context-usage-model';
import './ContextUsagePopover.css';

export type ContextUsageTelemetry = {
  tokens: number | null;
  contextWindow: number;
  percent: number | null;
  compactionCount?: number;
  latestCompaction?: {
    status: string;
    tokensBefore?: number;
    estimatedTokensAfter?: number;
  };
};

export function ContextUsagePopover({
  sessionId,
  telemetry,
}: {
  sessionId?: string;
  telemetry?: ContextUsageTelemetry | null;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState('');
  const [snapshot, setSnapshot] = useState<ContextXraySnapshot | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const transport = useOptionalControlTransport();
  const view = useMemo(
    () => buildContextUsageView({ telemetry, snapshot }),
    [snapshot, telemetry],
  );

  useEffect(() => {
    if (!open || !sessionId || !transport) return;
    let cancelled = false;
    setLoading(true);
    setSnapshot(null);
    setReadError('');
    void transport.request({
      pathId: 'agent.session.debugContext.get',
      params: { sessionId },
    }).then((value) => {
      if (cancelled) return;
      const response = normalizeDebugContextResponse(value);
      setSnapshot(buildContextXraySnapshot(response));
      if (!response.available) setReadError(contextReadError(response.error));
    }, (error: unknown) => {
      if (!cancelled) {
        setSnapshot(null);
        setReadError(error instanceof Error ? contextReadError(error.message) : '上下文读取失败，请重新打开重试。');
      }
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [open, sessionId, transport]);

  if (!sessionId) return null;
  const filled = view.percent === null ? 0 : Math.min(100, Math.max(0, view.percent));
  const triggerLabel = view.percent === null
    ? '上下文用量'
    : `上下文已用 ${Math.round(view.percent)}%`;

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <div className="agent-context-usage" ref={rootRef}>
        <PopoverTrigger asChild>
          <button
            aria-label={triggerLabel}
            className="agent-context-usage__trigger"
            ref={triggerRef}
            type="button"
          >
            <span aria-hidden="true" className="agent-context-usage__spark">
              <i style={{ width: `${filled}%` }} />
            </span>
            <span className="agent-context-usage__label">
              {view.percent === null ? '上下文' : `${Math.round(view.percent)}%`}
            </span>
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="end"
          aria-label="上下文用量"
          className="agent-context-usage__popover"
          collisionBoundary={rootRef.current?.closest<HTMLElement>('.paw-window-body') ?? undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            triggerRef.current?.focus();
          }}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            closeRef.current?.focus();
          }}
          side="top"
          sideOffset={10}
        >
          <header className="agent-context-usage__header">
            <strong>上下文用量</strong>
            <PopoverClose asChild>
              <button aria-label="关闭上下文用量" ref={closeRef} type="button">
                <X size={15} />
              </button>
            </PopoverClose>
          </header>
          <ContextUsageBody layerDetailsAvailable={snapshot?.available === true} loading={loading} readError={readError} view={view} />
        </PopoverContent>
      </div>
    </Popover>
  );
}

function ContextUsageBody({
  layerDetailsAvailable,
  loading,
  readError,
  view,
}: {
  layerDetailsAvailable: boolean;
  loading: boolean;
  readError: string;
  view: ContextUsageView;
}) {
  const hasReportedTotal = view.tokens !== null || view.contextWindow > 0 || view.percent !== null;
  const used = view.tokens ?? 0;
  const barTotal = Math.max(view.contextWindow, used, 1);
  // Runtime's prompt is also normally present as the latest user message in
  // contextMessages. Keep the dedicated row, but do not double-count it in
  // the illustrative character strip.
  const measurableLayers = (layerDetailsAvailable ? view.layers : []).filter(
    (layer) => layer.id !== 'currentInput' && layer.characters !== null && layer.characters > 0,
  );
  const characterTotal = Math.max(
    measurableLayers.reduce((sum, layer) => sum + (layer.characters ?? 0), 0),
    1,
  );

  return (
    <>
      {!layerDetailsAvailable || readError ? (
        <p className="agent-context-usage__empty" role="status">
          {loading ? '正在读取上下文占用…' : readError || '尚未收到分层上下文快照。'}
        </p>
      ) : null}
      {hasReportedTotal ? <div className="agent-context-usage__summary">
        <b>{view.percent === null ? '占用未知' : `已用 ${Math.round(view.percent)}%`}</b>
        <span>
          {view.tokens === null ? '未知' : `约 ${formatContextTokenCount(view.tokens)}`}
          {' / '}
          {view.contextWindow > 0 ? formatContextTokenCount(view.contextWindow) : '未知'}
          {' Tokens'}
        </span>
      </div> : null}
      {view.available ? <div aria-hidden="true" className="agent-context-usage__bar">
        {used > 0 ? <i data-used style={{ width: `${(used / barTotal) * 100}%` }} /> : null}
        {view.freeTokens !== null && view.freeTokens > 0 ? (
          <i data-free style={{ flex: view.freeTokens / barTotal }} />
        ) : null}
      </div> : null}
      {measurableLayers.length ? (
        <p className="agent-context-usage__composition-label">已捕获层字符示意（当前输入可能已在对话历史中，不重复计入）</p>
      ) : null}
      {measurableLayers.length ? (
        <div aria-hidden="true" className="agent-context-usage__composition">
          {measurableLayers.map((layer) => (
            <i
              key={layer.id}
              style={{
                background: layerColor(layer.id),
                width: `${Math.max(0.6, ((layer.characters ?? 0) / characterTotal) * 100)}%`,
              }}
            />
          ))}
        </div>
      ) : null}
      {layerDetailsAvailable ? <ContextUsageLayerList layers={view.layers} /> : (
        <p className="agent-context-usage__note">{unavailableContextNote(readError, loading)}</p>
      )}
      {layerDetailsAvailable && view.tokens !== null && view.layers.some((layer) => layer.characters !== null && layer.tokenQuality === 'unknown') ? (
        <p className="agent-context-usage__note">总 Token 仅有整轮统计，不能可靠分摊到各层。</p>
      ) : null}
      {view.compaction ? (
        <p className="agent-context-usage__note" data-compaction={view.compaction.status || undefined}>
          最近压缩
          {view.compaction.count > 0 ? ` · 第 ${view.compaction.count} 次` : ''}
          {view.compaction.tokensBefore !== null ? ` · ${formatContextTokenCount(view.compaction.tokensBefore)}` : ''}
          {view.compaction.tokensAfter !== null ? ` → 约 ${formatContextTokenCount(view.compaction.tokensAfter)}` : ''}
        </p>
      ) : null}
    </>
  );
}

function unavailableContextNote(readError: string, loading: boolean): string {
  if (loading) return '各层用量未知。';
  if (readError === '本机原始上下文调试尚未启用') {
    return '各层用量未知。可在设置 → 配置 → 隐私与安全开启“保存并查看本机上下文快照”。';
  }
  if (readError === '原对话尚未载入，暂时无法查看原始上下文。') {
    return '各层用量未知。重新打开原对话后，再查看上下文。';
  }
  return '各层用量未知。关闭后重新打开，可再次读取。';
}

function contextReadError(reason: string): string {
  if (reason === 'session_not_resident') return '原对话尚未载入，暂时无法查看原始上下文。';
  return reason.trim().slice(0, 300);
}

function ContextUsageLayerList({ layers }: { layers: ContextUsageView['layers'] }) {
  return (
    <ul className="agent-context-usage__list" aria-label="上下文分层占用" tabIndex={0}>
      {layers.map((layer) => (
        <li data-state={layer.state} key={layer.id} title={layer.source}>
          <span aria-hidden="true" className="agent-context-usage__swatch" style={{ background: layerColor(layer.id) }} />
          <span className="agent-context-usage__layer-copy">
            <strong>{layer.label}</strong>
            <small>{layer.note}</small>
          </span>
          <span className="agent-context-usage__metric">
            <b>{layerPrimaryValue(layer)}</b>
            <small>{layerQualityLabel(layer)}</small>
          </span>
        </li>
      ))}
    </ul>
  );
}

function layerPrimaryValue(layer: ContextUsageView['layers'][number]): string {
  if (layer.tokens !== null) return `${formatContextTokenCount(layer.tokens)} Tokens`;
  if (layer.characters !== null) return `${formatCharacterCount(layer.characters)} 字符`;
  return '未知';
}

function layerQualityLabel(layer: ContextUsageView['layers'][number]): string {
  if (layer.tokens !== null && layer.tokenQuality === 'exact') return '精确 Token';
  if (layer.tokens !== null && layer.tokenQuality === 'estimated') return '估算 Token';
  if (layer.characters !== null && layer.state === 'absent') return '明确未注入';
  if (layer.characters !== null) return 'Token 未单独统计';
  return '数据未知';
}

function layerColor(id: ContextUsageView['layers'][number]['id']): string {
  return ({
    systemPrompt: '#8b919a',
    skillsTools: '#7c5cbf',
    projectContext: '#2f9a5f',
    conversationHistory: '#d45a7a',
    memory: '#e07a3f',
    knowledge: '#c44d9a',
    currentInput: '#4a7fd4',
    cache: '#0e8270',
    compaction: '#a66a35',
  } as const)[id];
}

function formatCharacterCount(value: number): string {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(Math.max(0, value));
}
