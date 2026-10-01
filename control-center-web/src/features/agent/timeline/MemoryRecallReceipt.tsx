import { BrainCircuit, ExternalLink, Settings2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

import { useOptionalControlTransport } from '@/app/control-transport';
import { Disclosure } from '@/components/primitives';
import type { AgentContextTraceV1 } from '@/contracts/generated/agent-context-trace.v1';
import {
  evidenceEchoNodeEntities,
  evidenceEchoAppLabel,
  evidenceEchoSessionRoute,
  openEvidenceEchoEntity,
  type EvidenceEchoEntity,
} from '@/features/evidence-echo/evidence-echo';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import type { ControlTransport } from '@/platform/transport';

export type MemoryRecallReceiptView = {
  sessionId: string;
  traceId: string;
  turnId: string;
  nodeId: string;
  count?: number;
  durationMs?: number;
  status: 'included' | 'empty' | 'failed' | 'disabled' | 'reused' | 'unavailable';
  trigger: string;
  sourceTitles: string[];
  entities: EvidenceEchoEntity[];
};

export function MemoryRecallReceipt({ receipt }: { receipt: MemoryRecallReceiptView }) {
  const desktop = usePawOsDesktop();
  if (receipt.status === 'disabled' || receipt.status === 'unavailable') return null;
  return (
    <Disclosure
      className="agent-memory-recall-receipt"
      summary={(
        <>
          <BrainCircuit aria-hidden="true" size={14} />
          <strong>{memoryReceiptLabel(receipt)}</strong>
          <span aria-hidden="true">›</span>
        </>
      )}
    >
      <div className="agent-memory-recall-receipt__body">
        <p>{memoryReceiptExplanation(receipt)}</p>
        {receipt.sourceTitles.length ? (
          <ul aria-label="记忆召回来源标题">
            {receipt.sourceTitles.map((title) => <li key={title}>{title}</li>)}
          </ul>
        ) : null}
        <div className="agent-memory-recall-receipt__actions">
          {receipt.entities.map((entity) => (
            <button
              aria-label={`在${evidenceEchoAppLabel(entity.appId)}中打开 ${entity.label}`}
              key={`${entity.appId}:${entity.entityId}`}
              onClick={() => openEvidenceEchoEntity(desktop, entity)}
              type="button"
            >
              打开{evidenceEchoAppLabel(entity.appId)}来源 <ExternalLink aria-hidden="true" size={12} />
            </button>
          ))}
          <button
            aria-label="查看本轮上下文轨迹"
            onClick={() => openPawOsRoute(desktop, evidenceEchoSessionRoute(receipt))}
            type="button"
          >
            查看上下文轨迹 <ExternalLink aria-hidden="true" size={12} />
          </button>
          <button
            aria-label="打开本对话记忆开关"
            onClick={() => openPawOsRoute(desktop, `/agent?session=${encodeURIComponent(receipt.sessionId)}&tools=memory&toolsRequest=${Date.now()}`)}
            type="button"
          >
            本对话记忆开关 <Settings2 aria-hidden="true" size={12} />
          </button>
        </div>
      </div>
    </Disclosure>
  );
}

export function memoryRecallReceiptFromTrace(
  trace: AgentContextTraceV1,
): MemoryRecallReceiptView | undefined {
  const node = trace.nodes.find((candidate) => (
    candidate.stage === 'memory_recall'
  ));
  if (!node) return undefined;
  const count = finiteInteger(node.metadata.hitCount);
  const recordedStatus = text(node.metadata.recallStatus);
  const status: MemoryRecallReceiptView['status'] = (
    ['included', 'empty', 'failed', 'disabled', 'reused', 'unavailable'].includes(recordedStatus)
      ? recordedStatus as MemoryRecallReceiptView['status']
      : node.disposition === 'failed' ? 'failed'
        : node.disposition === 'included' ? count === 0 ? 'empty' : 'included'
          : 'unavailable'
  );
  if (status === 'disabled' || status === 'unavailable') return undefined;
  const duration = finiteInteger(node.durationMs);
  return {
    sessionId: trace.sessionId,
    traceId: trace.traceId,
    turnId: trace.turnId,
    nodeId: node.nodeId,
    count: count !== undefined && count >= 0 ? count : undefined,
    // Historical traces used 0 when no recall timing was measured.
    durationMs: duration !== undefined && duration > 0 ? duration : undefined,
    status,
    trigger: text(node.metadata.recallTrigger),
    sourceTitles: commaSeparated(node.metadata.sourceTitles, 12),
    entities: evidenceEchoNodeEntities(node, { sessionId: trace.sessionId })
      .filter((entity) => entity.appId === 'memory' || entity.appId === 'knowledge'),
  };
}

export function useMemoryRecallReceipts(
  sessionId: string,
  turnIds: string[],
  hasActiveTurn: boolean,
  active = true,
): Record<string, MemoryRecallReceiptView> {
  const transport = useOptionalControlTransport();
  const [receipts, setReceipts] = useState<Record<string, MemoryRecallReceiptView>>({});
  const settledCache = useRef(new Map<string, MemoryRecallReceiptView | undefined>());
  const turnIdsKey = useMemo(() => turnIds.join('\u001f'), [turnIds]);

  useEffect(() => {
    settledCache.current.clear();
    setReceipts({});
  }, [sessionId]);

  useEffect(() => {
    if (!active || !transport || !sessionId || turnIds.length === 0) return undefined;
    const controller = new AbortController();
    let loading = false;
    const load = async () => {
      if (loading) return;
      loading = true;
      try {
        const next = await loadMemoryRecallReceipts(
          transport,
          sessionId,
          new Set(turnIds),
          settledCache.current,
          controller.signal,
        );
        if (!controller.signal.aborted) {
          setReceipts((current) => receiptMapsEqual(current, next) ? current : next);
        }
      } catch {
        // Context trace is an observability projection. Conversation remains
        // authoritative and usable when the projection is temporarily absent.
      } finally {
        loading = false;
      }
    };
    void load();
    const interval = active && hasActiveTurn ? window.setInterval(() => void load(), 3_000) : 0;
    return () => {
      controller.abort();
      if (interval) window.clearInterval(interval);
    };
  }, [active, hasActiveTurn, sessionId, transport, turnIdsKey]);
  return receipts;
}

async function loadMemoryRecallReceipts(
  transport: ControlTransport,
  sessionId: string,
  turnIds: Set<string>,
  settledCache: Map<string, MemoryRecallReceiptView | undefined>,
  signal: AbortSignal,
): Promise<Record<string, MemoryRecallReceiptView>> {
  const response = await transport.request({
    pathId: 'agent.session.contextTraces.list',
    params: { sessionId },
    query: { limit: 64 },
    signal,
  });
  const summaries = array(record(response).items)
    .map(record)
    .filter((item) => turnIds.has(text(item.turnId)) && Boolean(text(item.traceId)));
  const tracesByTurn = new Map<string, Record<string, unknown>[]>();
  for (const summary of summaries) {
    const turnId = text(summary.turnId);
    const entries = tracesByTurn.get(turnId) ?? [];
    entries.push(summary);
    tracesByTurn.set(turnId, entries);
  }
  const pairs = await Promise.all([...tracesByTurn.entries()].map(async ([turnId, entries]) => {
    let best: MemoryRecallReceiptView | undefined;
    for (const summary of entries) {
      if (signal.aborted) break;
      const traceId = text(summary.traceId);
      const settled = text(summary.status) !== 'building';
      let receipt = settled ? settledCache.get(traceId) : undefined;
      if (!settled || !settledCache.has(traceId)) {
        try {
          const trace = await transport.request<AgentContextTraceV1>({
            pathId: 'agent.session.contextTrace.get',
            params: { sessionId, traceId },
            signal,
          });
          receipt = memoryRecallReceiptFromTrace(trace);
          if (settled) settledCache.set(traceId, receipt);
        } catch {
          continue;
        }
      }
      if (receipt && (!best || receiptPriority(receipt) > receiptPriority(best))) best = receipt;
      // A later steer shares the turn but does not replace its actual recall.
      // Stop at the newest real outcome instead of loading every old trace.
      if (best && receiptPriority(best) === 3) break;
    }
    return [turnId, best] as const;
  }));
  return Object.fromEntries(pairs.filter((pair): pair is readonly [string, MemoryRecallReceiptView] => Boolean(pair[1])));
}

function receiptPriority(receipt: MemoryRecallReceiptView): number {
  return receipt.status === 'unavailable' ? 1 : receipt.status === 'reused' ? 2 : 3;
}

function memoryReceiptLabel(receipt: MemoryRecallReceiptView): string {
  const kind = receipt.trigger === 'compaction' ? '压缩后记忆' : '本轮记忆';
  const outcome = receipt.status === 'included'
    ? receipt.count === undefined ? '已载入' : `${receipt.count} 条`
    : {
      empty: '未找到相关记忆', failed: '本轮未能召回', disabled: '已关闭',
      reused: '复用已载入记忆', unavailable: '本轮未装载',
    }[receipt.status];
  const timing = receipt.durationMs !== undefined && ['included', 'empty'].includes(receipt.status)
    ? ` · ${receipt.durationMs} ms` : '';
  return `${kind} · ${outcome}${timing}`;
}

function memoryReceiptExplanation(receipt: MemoryRecallReceiptView): string {
  return {
    included: '这些记忆已加入本轮上下文。可打开来源核对；旧记录没有命中数时不推算数量。',
    empty: '这次自动检索没有找到可加入的相关记忆，当前对话可以继续。',
    failed: '本轮未能载入记忆，当前消息仍可继续。详情可在上下文轨迹中查看。',
    disabled: '该轮未启用记忆召回。这是当时的执行记录；当前开关可在本对话的记忆设置中查看。',
    reused: '该轮复用了对话先前已载入的记忆，没有再次检索。这是当时的执行记录，不代表当前开关状态。',
    unavailable: '这条投递记录没有附带新的记忆包，不能据此判断没有相关记忆。',
  }[receipt.status];
}

function receiptMapsEqual(
  left: Record<string, MemoryRecallReceiptView>,
  right: Record<string, MemoryRecallReceiptView>,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function commaSeparated(value: unknown, limit: number): string[] {
  if (typeof value !== 'string') return [];
  return value.split(',').map((item) => item.trim()).filter(Boolean).slice(0, limit);
}

function finiteInteger(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : undefined;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}
