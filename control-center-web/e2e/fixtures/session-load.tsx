import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createRoot } from 'react-dom/client';
import { ControlTransportProvider } from '../../src/app/control-transport';
import { createPreviewTransport } from '../../src/app/preview-control-transport';
import { TooltipProvider } from '../../src/components/primitives';
import { MotionProvider } from '../../src/design/motion';
import { ThemeProvider } from '../../src/design/themes';
import { previewAgentSnapshot } from '../../src/features/agent/preview-data';
import { useAgentLiveStore } from '../../src/features/agent/state/live-store';
import type { SessionSummary } from '../../src/features/agent/types';
import { PawSessionWorkspace } from '../../src/paw-os/apps/PawSessionWorkspace';
import type { ControlRequest } from '../../src/platform/transport';
import type { UiAgentMessage } from '../../src/contracts/ui-events';
import '../../src/design/tokens.css';
import '../../src/design/typography.css';
import '../../src/design/workspace.css';
import '../../src/components/primitives/primitives.css';
import '../../src/paw-os/styles/paw-os.css';
import '../../src/paw-os/apps/paw-apps.css';
import '../../src/paw-os/styles/paw-os-agent.css';
import '../../src/paw-os/styles/paw-os-agent-fx.css';

// Controlled data through the real transport, reducer, workspace and Virtuoso.
// This fixture makes no Provider calls and is excluded from product builds.
const sessionId = 'session-load';
const now = Date.now();
const messages: UiAgentMessage[] = Array.from({ length: 1000 }, (_, index) => ({
  schemaVersion: 'rag-ime.agent-message.v1', id: `load-${index}`, sessionId,
  turnId: `load-turn-${Math.floor(index / 2)}`, role: index % 2 ? 'assistant' : 'user',
  status: 'completed', createdAtMs: now - 1000 + index, completedAtMs: now - 1000 + index,
  attachments: [], citations: [],
  blocks: [{ id: `load-block-${index}`, type: 'text', status: 'completed', presentationKind: 'text', data: {
    text: index === 999 ? '# 长 Markdown\n\n真实 Session 虚拟列表。\n\n|字段|状态|\n|---|---|\n|历史|1000|\n\n' + '自然换行并保留阅读位置。'.repeat(100)
      : `History message ${index} — bounded production transcript.`,
  } }],
}));
const transport = createPreviewTransport();
const request = transport.request.bind(transport);
transport.request = async <Response,>(input: ControlRequest): Promise<Response> => {
  if (input.pathId === 'agent.session.snapshot') {
    return { ...previewAgentSnapshot(sessionId), messages, liveEvents: [], lastSequence: 1000,
      resumeToken: `${sessionId}:1000`, status: 'idle', snapshotScope: 'full', partial: false } as Response;
  }
  return request<Response>(input);
};
const record: SessionSummary = { id: sessionId, title: 'Controlled 1000-message Session', mode: 'assistant', status: 'idle',
  roleId: 'companion-present-v1', roleVersion: '1', roleBookRevisionId: '', updatedAtMs: now,
  workspaceRoots: [], messageCount: 1000, modelProfile: 'openai-codex/gpt-6.1-sol' };
const metrics = { total: 1000, events: 0, commits: 0, complete: false, longTasks: [] as number[], typing: [] as number[] };
let sequence = 1000;
function emit(eventType: string, payload: Record<string, unknown>) {
  sequence += 1;
  transport.emit('agent.session.events', { schemaVersion: 'rag-ime.agent-event.v1', eventId: `${sessionId}:${sequence}`,
    sessionId, turnId: 'load-stream', sequence, createdAtMs: Date.now(), eventType, payload,
    resumeToken: `${sessionId}:${sequence}` });
}
function startStream() {
  metrics.events = 0; metrics.commits = 0; metrics.complete = false; metrics.longTasks = []; metrics.typing = [];
  const stop = useAgentLiveStore.subscribe(() => { metrics.commits += 1; });
  const observer = new PerformanceObserver(list => { metrics.longTasks.push(...list.getEntries().map(entry => entry.duration)); });
  observer.observe({ type: 'longtask', buffered: false });
  emit('turn_started', {});
  const timer = window.setInterval(() => {
    metrics.events += 1;
    emit('text_delta', { messageId: 'load-stream-assistant', blockId: 'load-stream-text', delta: `Δ${metrics.events} ` });
    if (metrics.events === 200) {
      window.clearInterval(timer);
      window.setTimeout(() => { stop(); observer.disconnect(); metrics.complete = true; }, 100);
    }
  }, 5);
}
Reflect.set(window, '__PAW_SESSION_LOAD__', { metrics, startStream, subscriptions: () => transport.activeSubscriptionCount() });
document.addEventListener('input', event => {
  if (!(event.target instanceof HTMLTextAreaElement)) return;
  const started = performance.now();
  requestAnimationFrame(() => metrics.typing.push(performance.now() - started));
});
createRoot(document.getElementById('root')!).render(
  <ThemeProvider><MotionProvider><TooltipProvider>
    <ControlTransportProvider transport={transport}><QueryClientProvider client={new QueryClient()}>
      <div className="paw-desktop-root"><PawSessionWorkspace record={record} recordId={sessionId}
        onNewWork={() => undefined} onSessionCreated={() => undefined} onSessionUpdated={() => undefined} /></div>
    </QueryClientProvider></ControlTransportProvider>
  </TooltipProvider></MotionProvider></ThemeProvider>,
);
