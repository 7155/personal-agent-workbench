import { act, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { AgentContextTraceV1 } from '@/contracts/generated/agent-context-trace.v1';
import type { ControlRequest } from '@/platform/transport';
import { ControlTransportProvider } from '@/app/control-transport';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { StubControlTransport } from '@/test/stub-control-transport';
import {
  MemoryRecallReceipt,
  memoryRecallReceiptFromTrace,
  useMemoryRecallReceipts,
} from './MemoryRecallReceipt';

describe('MemoryRecallReceipt', () => {
  it('pauses context-trace polling while inactive and resumes one poll on activation', async () => {
    vi.useFakeTimers();
    const transport = new StubControlTransport('mock', {
      'agent.session.contextTraces.list': { items: [] },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ControlTransportProvider transport={transport}>{children}</ControlTransportProvider>
    );
    const { rerender, unmount } = renderHook(
      ({ active }: { active: boolean }) => useMemoryRecallReceipts('session-memory', ['turn-memory'], true, active),
      { initialProps: { active: false }, wrapper },
    );
    try {
      await act(async () => {
        await Promise.resolve();
      });
      expect(transport.requests).toHaveLength(0);

      rerender({ active: true });
      await act(async () => {
        await Promise.resolve();
      });
      expect(transport.requests).toHaveLength(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_000);
      });
      expect(transport.requests).toHaveLength(2);

      rerender({ active: false });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6_000);
      });
      expect(transport.requests).toHaveLength(2);
    } finally {
      unmount();
      vi.useRealTimers();
    }
  });

  it('projects only an included per-turn memory trace and starts folded', async () => {
    const receipt = memoryRecallReceiptFromTrace(memoryTrace());
    expect(receipt).toMatchObject({
      count: 3,
      durationMs: 18,
      nodeId: 'node-memory',
      traceId: 'trace-memory',
      turnId: 'turn-memory',
    });

    const openRoute = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <PawOsDesktopProvider openRoute={openRoute} openWindow={() => {}}>
        <MemoryRecallReceipt receipt={receipt!} />
      </PawOsDesktopProvider>,
    );

    const disclosure = container.querySelector('details');
    expect(disclosure).not.toHaveAttribute('open');
    expect(screen.getByText('本轮记忆 · 3 条 · 18 ms')).toBeInTheDocument();
    expect(screen.queryByText('偏好：回答保持简洁')).not.toBeInTheDocument();

    await user.click(screen.getByText('本轮记忆 · 3 条 · 18 ms'));
    expect(screen.getByText('偏好：回答保持简洁')).toBeInTheDocument();
    expect(screen.getByText('PAW 项目约定')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '在记忆中打开 atom-1' }));
    expect(openRoute).toHaveBeenCalledWith('/memory?layer=atoms&id=atom-1');
    await user.click(screen.getByRole('button', { name: '查看本轮上下文轨迹' }));
    expect(openRoute).toHaveBeenCalledWith('/agent?session=session-memory&trace=trace-memory&node=node-memory');
    await user.click(screen.getByRole('button', { name: '打开本对话记忆开关' }));
    expect(openRoute).toHaveBeenCalledWith(expect.stringMatching(/^\/agent\?session=session-memory&tools=memory&toolsRequest=\d+$/));
  });

  it('keeps a real legacy trace visible without confusing pack count with hit count', () => {
    const trace = memoryTrace({ hitCount: undefined });
    trace.nodes[0].metadata.itemCount = 1;
    const receipt = memoryRecallReceiptFromTrace(trace);
    expect(receipt).toMatchObject({ status: 'included', count: undefined });
    render(<MemoryRecallReceipt receipt={receipt!} />);
    expect(screen.getByText(/本轮记忆 · 已载入/)).toBeInTheDocument();
    expect(screen.queryByText(/1 条/)).not.toBeInTheDocument();
  });

  it.each([
    ['empty', 0, '未找到相关记忆'],
    ['failed', undefined, '本轮未能召回'],
    ['reused', 3, '复用已载入记忆'],
  ] as const)('shows the recorded %s outcome without manufacturing a new search', (status, count, label) => {
    const trace = memoryTrace({ hitCount: count });
    trace.nodes[0].metadata.recallStatus = status;
    trace.nodes[0].durationMs = 0;
    if (status !== 'empty') trace.nodes[0].disposition = status === 'failed' ? 'failed' : 'omitted';
    const receipt = memoryRecallReceiptFromTrace(trace);
    expect(receipt?.status).toBe(status);
    render(<MemoryRecallReceipt receipt={receipt!} />);
    expect(screen.getByText(new RegExp(label))).toBeInTheDocument();
    expect(screen.queryByText(/0 ms/)).not.toBeInTheDocument();
  });

  it.each(['disabled', 'unavailable'] as const)('hides %s memory without a recall or use', (status) => {
    const trace = memoryTrace({ disposition: 'omitted', hitCount: undefined });
    trace.nodes[0].metadata.recallStatus = status;
    expect(memoryRecallReceiptFromTrace(trace)).toBeUndefined();
    const previous = memoryRecallReceiptFromTrace(memoryTrace())!;
    const { container } = render(<MemoryRecallReceipt receipt={{ ...previous, status }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('does not let an omitted steer trace erase the original recall in the same turn', async () => {
    const initial = memoryTrace();
    const steer = memoryTrace({ disposition: 'omitted', hitCount: undefined });
    steer.traceId = 'trace-steer';
    const transport = new StubControlTransport('mock', {
      'agent.session.contextTraces.list': { items: [
        { traceId: steer.traceId, turnId: steer.turnId, status: 'accepted' },
        { traceId: initial.traceId, turnId: initial.turnId, status: 'accepted' },
      ] },
      'agent.session.contextTrace.get': (request: ControlRequest) => request.params?.traceId === 'trace-steer' ? steer : initial,
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <ControlTransportProvider transport={transport}>{children}</ControlTransportProvider>
    );
    const { result } = renderHook(
      () => useMemoryRecallReceipts('session-memory', ['turn-memory'], false), { wrapper },
    );
    await waitFor(() => expect(result.current['turn-memory']).toMatchObject({ traceId: 'trace-memory', count: 3 }));
  });

  it('keeps a recalled Knowledge document as a clickable source', async () => {
    const receipt = memoryRecallReceiptFromTrace(memoryTrace({ knowledgeDocumentId: 'doc-1' }));
    expect(receipt?.entities).toEqual(expect.arrayContaining([
      expect.objectContaining({ appId: 'knowledge', entityId: 'doc-1' }),
    ]));

    const openRoute = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <PawOsDesktopProvider openRoute={openRoute} openWindow={() => {}}>
        <MemoryRecallReceipt receipt={receipt!} />
      </PawOsDesktopProvider>,
    );
    await user.click(within(container).getByText('本轮记忆 · 3 条 · 18 ms'));
    await user.click(within(container).getByRole('button', { name: '在知识库中打开 doc-1' }));
    expect(openRoute).toHaveBeenCalledWith('/knowledge?document=doc-1&tab=viewer');
  });
});

function memoryTrace(options: {
  disposition?: 'included' | 'omitted';
  hitCount?: number;
  knowledgeDocumentId?: string;
} = {}): AgentContextTraceV1 {
  const disposition = options.disposition ?? 'included';
  const hitCount = Object.hasOwn(options, 'hitCount') ? options.hitCount : 3;
  return {
    schemaVersion: 'rag-ime.agent-context-trace.v1',
    traceId: 'trace-memory',
    sessionId: 'session-memory',
    turnId: 'turn-memory',
    sourceKind: 'prompt',
    status: 'accepted',
    finalFingerprint: 'trace-fingerprint',
    nodes: [{
      nodeId: 'node-memory',
      ordinal: 2,
      stage: 'memory_recall',
      label: '当前回合个人记忆召回',
      sourceKind: 'memory_bootstrap',
      disposition,
      summary: '已装配个人记忆上下文',
      charCount: 120,
      tokenEstimate: 30,
      durationMs: 18,
      fingerprint: 'node-fingerprint',
      reason: '',
      metadata: {
        ...(hitCount === undefined ? {} : { hitCount }),
        memoryAtomIds: 'atom-1',
        memoryBookIds: 'book-2',
        sourceTitles: '偏好：回答保持简洁,PAW 项目约定',
        ...(options.knowledgeDocumentId ? { knowledgeDocumentIds: options.knowledgeDocumentId } : {}),
      },
      createdAtMs: 1_010,
    }],
    edges: [],
    createdAtMs: 1_000,
    updatedAtMs: 1_020,
  };
}
