import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ControlTransportProvider } from '@/app/control-transport';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, TooltipProvider } from '@/components/primitives';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import { roleItems, type SessionSummary } from '@/features/agent/types';
import { PawOsDesktopProvider, type PawOsWindowRequest } from '@/features/paw-os/surface-context';
import type { RoomSummary } from '@/features/rooms/room-types';
import { PawRoomWorkspace } from '@/paw-os/apps/PawRoomWorkspace';
import { PawSessionWorkspace } from '@/paw-os/apps/PawSessionWorkspace';
import { HttpControlTransport } from '@/platform/http-transport';
import '@/design/tokens.css';
import '@/design/typography.css';
import '@/components/primitives/primitives.css';
import '@/features/conversation-ui/conversation-ui.css';
import '@/paw-os/styles/paw-os.css';
import '@/paw-os/styles/paw-os-motion.css';
import '@/paw-os/apps/paw-apps.css';
import '@/paw-os/styles/paw-os-room.css';
import '@/paw-os/styles/paw-os-agent.css';
import '@/paw-os/styles/paw-os-agent-fx.css';
import '@/paw-os/styles/paw-os-shell.css';
import '@/paw-os/styles/paw-os-controls.css';
import '@/paw-os/styles/paw-os-stellar.css';
import '@/paw-os/styles/paw-os-stellar-dark.css';
import './jev-live.css';

// This entry has no preview transport or fixture data. Vite's /api proxy
// forwards both ordinary requests and SSE to the selected loopback backend.
const transport = new HttpControlTransport({ baseUrl: window.location.origin });
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const params = new URLSearchParams(window.location.search);
const roomId = params.get('roomId')?.trim() || '';
const LiveFiles = lazy(async () => ({ default: (await import('@/features/files/PawOsFilesApp')).PawOsFilesApp }));

function LiveWorkspace() {
  const [room, setRoom] = useState<RoomSummary>();
  const [personas, setPersonas] = useState<AgentPersonaV1[]>([]);
  const [sessionId, setSessionId] = useState(params.get('sessionId') || '');
  const [session, setSession] = useState<SessionSummary>();
  const [dark, setDark] = useState(false);
  const [fileRoute, setFileRoute] = useState('');
  useEffect(() => {
    const abort = new AbortController();
    void transport.request({ pathId: 'agent.roles.list', signal: abort.signal })
      .then(value => { if (!abort.signal.aborted) setPersonas(roleItems(value)); })
      .catch(() => undefined); // Existing Room snapshots remain usable if this optional directory fails.
    return () => abort.abort();
  }, []);
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; }, [dark]);

  const showSession = useCallback((id: string) => {
    setSessionId(id);
    setSession(undefined);
    const next = new URL(window.location.href);
    if (id) next.searchParams.set('sessionId', id);
    else next.searchParams.delete('sessionId');
    history.replaceState(null, '', next);
  }, []);
  const openWindow = useCallback(({ target }: PawOsWindowRequest) => {
    if (target.kind === 'session') showSession(target.id);
    else if (target.kind === 'participant' && target.sessionId) showSession(target.sessionId);
  }, [showSession]);
  const openRoute = useCallback((route: string) => {
    if (route.startsWith('/files?')) setFileRoute(route);
  }, []);

  return <PawOsDesktopProvider openWindow={openWindow} openRoute={openRoute}>
    <div className="paw-desktop-root jev-live-shell" data-paw-visual="stellar">
      <header className="jev-live-toolbar"><strong>真实后端联调 · HTTP / SSE</strong><span>{room?.title || roomId}</span>{sessionId ? <button type="button" onClick={() => showSession('')}>返回 JEV 对话</button> : null}<button type="button" onClick={() => setDark(!dark)}>{dark ? '浅色主题' : '深色主题'}</button></header>
      <main className="jev-live-stage">{!roomId ? <form className="jev-live-empty" method="get"><h1>打开真实 Room</h1><label htmlFor="live-room-id">Room ID</label><input id="live-room-id" name="roomId" required autoComplete="off" /><button type="submit">连接工作区</button><p>请求会发送到当前 Vite 服务配置的后端。</p></form>
        : sessionId ? <PawSessionWorkspace key={sessionId} recordId={sessionId} record={session} onSessionUpdated={setSession} onNewWork={() => showSession('')} onSessionCreated={value => { showSession(value.id); setSession(value); }} />
        : <PawRoomWorkspace key={roomId} interfaceMode="jev" recordId={roomId} record={room} personas={personas} onRoomUpdated={setRoom} />}</main>
    </div>
    <Dialog open={Boolean(fileRoute)} onOpenChange={open => { if (!open) setFileRoute(''); }}>
      <DialogContent className="jev-live-file-window">
        <DialogHeader><DialogTitle>文件预览</DialogTitle><DialogDescription>伙伴工作区中的当前文件。</DialogDescription></DialogHeader>
        <div className="jev-live-file-window__body"><Suspense fallback={<p role="status">正在打开文件…</p>}>
          <LiveFiles initialRoute={fileRoute} />
        </Suspense></div>
      </DialogContent>
    </Dialog>
  </PawOsDesktopProvider>;
}

createRoot(document.getElementById('root')!).render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider><LiveWorkspace /></TooltipProvider></ControlTransportProvider></QueryClientProvider>);
