import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { PawOsAppearanceProvider } from '@/design/paw-os-themes';
import { MockControlTransport } from '@/test/mock-transport';
import { PawAppBody } from './PawAppsRuntime';

vi.mock('./PawBrowserApp', () => ({
  PawBrowserApp: () => <main>PAW 直连 Browser</main>,
}));

vi.mock('./PawAgentApp', () => ({
  PawAgentApp: ({ target }: { target?: { kind?: string; id?: string } }) => (
    <main data-testid="agent-app">{target ? `${target.kind}:${target.id}` : 'agent-home'}</main>
  ),
}));

vi.mock('./PawAgentCapsuleApp', () => ({
  PawAgentCapsuleApp: () => <main data-testid="agent-capsule-app">capsule-home</main>,
}));

vi.mock('@/features/paw-os/PawOsSatelliteHost', () => ({
  PawOsSatelliteHost: ({ target }: { target: { kind: string; panel?: string } }) => (
    <aside data-testid="satellite-host">{`${target.kind}:${target.panel ?? 'none'}`}</aside>
  ),
}));

afterEach(cleanup);

describe('PAWOS App runtime', () => {
  it('mounts Agent Capsule as a managed App entry', async () => {
    render(<PawAppBody appId="agent-capsule" />);
    expect(await screen.findByTestId('agent-capsule-app')).toHaveTextContent('capsule-home');
  });

  it('mounts Trace Agent as an independent App with its own task and library navigation', async () => {
    const transport = new MockControlTransport({ routes: {
      'observability.traceDiagnosticReports.list': { schemaVersion: 'rag-ime.trace-diagnostic-report-list.v1', total: 0, truncated: false, items: [] },
    } });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<ControlTransportProvider transport={transport}><PawOsAppearanceProvider><QueryClientProvider client={client}><PawAppBody appId="trace-agent" initialRoute="/trace-agent" /></QueryClientProvider></PawOsAppearanceProvider></ControlTransportProvider>);
    expect(await screen.findByRole('heading', { name: '工作台' })).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Trace Agent 应用导航' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '经验库' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '能力库' })).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'System Monitor页面' })).not.toBeInTheDocument();
  });

  it('mounts the direct PAW Browser surface', async () => {
    render(<PawAppBody appId="browser" />);

    expect(await screen.findByText('PAW 直连 Browser')).toBeInTheDocument();
  });

  it('mounts an isolated Agent result window for authored HTML', async () => {
    render(
      <PawAppBody
        appId="agent"
        target={{
          kind: 'result',
          id: 'result-42',
          title: '音乐律动预览',
          resultKind: 'html',
          content: '<main><h2>Composition 8</h2></main>',
        }}
      />,
    );

    expect(await screen.findByRole('heading', { name: '音乐律动预览' })).toBeInTheDocument();
    expect(await screen.findByTitle('音乐律动预览')).toHaveAttribute('sandbox', expect.stringContaining('allow-scripts'));
  });

  it('keeps an extension-owned HTML result in the isolated result surface', async () => {
    render(
      <PawAppBody
        appId={'extension:earth-research' as never}
        target={{ kind: 'result', id: 'earth-report-42', title: '2025 植被面积变化', resultKind: 'html', content: '<h1>报告</h1>' }}
      />,
    );

    expect(await screen.findByRole('heading', { name: '2025 植被面积变化' })).toBeInTheDocument();
    expect(await screen.findByTitle('2025 植被面积变化')).toHaveAttribute('sandbox', expect.stringContaining('allow-scripts'));
    expect(screen.queryByRole('heading', { name: 'Google Earth Agent' })).not.toBeInTheDocument();
  });

  it('keeps the Room conversation in Agent while routing only named Room panels to satellites', async () => {
    const { rerender } = render(
      <PawAppBody
        appId="agent"
        target={{ kind: 'room', id: 'room-42', title: '产品协作室' }}
      />,
    );

    expect(await screen.findByTestId('agent-app')).toHaveTextContent('room:room-42');
    expect(screen.queryByTestId('satellite-host')).not.toBeInTheDocument();

    rerender(
      <PawAppBody
        appId="agent"
        target={{ kind: 'room', id: 'room-42', title: '产品协作室', panel: 'focus' }}
      />,
    );

    expect(await screen.findByTestId('satellite-host')).toHaveTextContent('room:focus');
    expect(screen.queryByTestId('agent-app')).not.toBeInTheDocument();
  });

  it('renders a Room planet in the compact observation host, not the full Session workspace', async () => {
    render(
      <PawAppBody
        appId="agent"
        target={{
          kind: 'participant',
          id: 'participant-earth',
          roomId: 'room-sol',
          sessionId: 'session-earth',
          title: 'Earth',
          subtitle: '最终汇合与回复',
        }}
      />,
    );

    expect(await screen.findByTestId('satellite-host')).toHaveTextContent('participant:none');
    expect(screen.queryByTestId('agent-app')).not.toBeInTheDocument();
  });

  it('routes non-conversation Apps through the PAWOS-native, data-backed surface', async () => {
    const transport = new MockControlTransport({ routes: {
      'overview.get': { ok: true, project: { name: 'PAWOS' } },
      'planning.dashboard': { ok: true, tasks: [] },
      'workDocuments.list': { ok: true, items: [] },
    } });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <ControlTransportProvider transport={transport}>
        <PawOsAppearanceProvider>
          <QueryClientProvider client={queryClient}>
            <PawAppBody appId="project-workbench" />
          </QueryClientProvider>
        </PawOsAppearanceProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('heading', { name: '项目概览' }, { timeout: 5_000 })).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.map(({ request }) => request.pathId)).toEqual(
      expect.arrayContaining(['overview.get', 'planning.dashboard', 'workDocuments.list']),
    ));
  });

  it('mounts a source-isolated Extension App through the generic host', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
    } });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <ControlTransportProvider transport={transport}>
        <PawOsAppearanceProvider>
          <QueryClientProvider client={queryClient}>
            <PawAppBody appId={'extension:zhanggui-wenshu' as never} />
          </QueryClientProvider>
        </PawOsAppearanceProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('heading', { name: '掌柜问数' }, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '问数' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '对账' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '解释' })).toBeInTheDocument();
  });

  it('keeps Extension Apps in their own Host even when a generic satellite target is present', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
    } });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <ControlTransportProvider transport={transport}>
        <PawOsAppearanceProvider>
          <QueryClientProvider client={queryClient}>
            <PawAppBody
              appId={'extension:zhanggui-wenshu' as never}
              target={{
                kind: 'package',
                id: '@paw/zhanggui-wenshu',
                title: '掌柜问数',
                version: '0.2.0',
                resourceCount: 1,
              }}
            />
          </QueryClientProvider>
        </PawOsAppearanceProvider>
      </ControlTransportProvider>,
    );

    expect(await screen.findByRole('heading', { name: '掌柜问数' }, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.queryByTestId('satellite-host')).not.toBeInTheDocument();
  });
});
