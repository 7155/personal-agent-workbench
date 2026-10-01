import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { GlobalFeedbackProvider } from '@/components/feedback';
import { MockControlTransport } from '@/test/mock-transport';
import { PawDesktopProvider, usePawDesktopStore } from '../runtime/desktop-context';
import { maintenanceFailureDetail, PawBackgroundActivity } from './PawBackgroundActivity';
import { PawNotificationCenter } from './PawNotificationCenter';
import { pawMemoryMaintenanceActivity, PawWorkDirectoryProvider, usePawWorkDirectory } from './PawWorkDirectory';
import statusCss from './paw-shell-status.css?raw';

afterEach(() => cleanup());

describe('PawBackgroundActivity', () => {
  it('does not surface the raw memory bootstrap budget exception', () => {
    expect(maintenanceFailureDetail('memory bootstrap exceeded its strict character budget'))
      .toBe('记忆召回本轮已跳过，消息仍可继续；下次会重新尝试。');
    expect(maintenanceFailureDetail('Codex error: The usage limit has been reached'))
      .toContain('模型服务额度暂时用尽');
    expect(maintenanceFailureDetail('provider down')).toBe('provider down');
  });

  it('renders its portal above the fixed PAWOS desktop stacking context', () => {
    expect(statusCss).toMatch(/\.ui-popover\.paw-background-activity__popover\s*\{[\s\S]*?z-index:\s*1200/);
  });

  it('recognizes the real maintenance projection without inventing a conversation Session', () => {
    expect(pawMemoryMaintenanceActivity({
      job: {
        jobId: 'memory-maintenance:active',
        state: 'running',
        progress: { summary: '正在整理 4 个来源' },
        updatedAtMs: 42,
      },
    })).toEqual({
      id: 'memory-maintenance:active',
      title: '自动记忆整理',
      detail: '正在整理 4 个来源',
      updatedAtMs: 42,
    });
    expect(pawMemoryMaintenanceActivity({ projection: { running: true, lastRunAtMs: 42 } })).toBeNull();
    expect(pawMemoryMaintenanceActivity({ projection: { running: false } })).toBeNull();
  });

  it('lists only real running conversations and returns directly to their canonical windows', async () => {
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [
        session('memory-maintenance', '自动记忆整理', 'busy'),
        session('conversation', '整理 PAWOS 桌面', 'busy'),
        session('room-partner', '桌面协作 · Agent 1', 'busy', { roomId: 'room-running' }),
        session('idle', '已经空闲', 'idle'),
      ] },
      'agent.rooms.list': { ok: true, items: [{
        id: 'room-running',
        title: '桌面协作',
        status: 'active',
        updatedAtMs: 30,
        workspaceRoots: ['/work/paw'],
        participants: [],
        workItems: [],
      }] },
    } }));

    const trigger = await screen.findByRole('button', { name: '3 个后台工作正在运行' });
    fireEvent.click(trigger);

    const panel = await screen.findByRole('region', { name: '后台运行' });
    expect(within(panel).queryByText('自动记忆整理', { selector: 'small' })).not.toBeInTheDocument();
    expect(within(panel).getAllByText('对话 Agent')).toHaveLength(2);
    expect(within(panel).getByText('Room 协作')).toBeInTheDocument();
    expect(within(panel).queryByText('已经空闲')).not.toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: /整理 PAWOS 桌面/ }));
    await waitFor(() => expect(screen.getByTestId('open-window-ids')).toHaveTextContent('agent:conversation'));
  });

  it('stays absent when the Runtime reports no running work', async () => {
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [session('idle', '已经空闲', 'idle')] },
      'agent.rooms.list': { ok: true, items: [] },
    } }));

    await waitFor(() => expect(screen.queryByRole('button', { name: /后台工作正在运行/ })).not.toBeInTheDocument());
  });

  it('publishes a real notification when a canonical running Session exits busy state', async () => {
    let status = 'busy';
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': () => ({ ok: true, items: [session('conversation', '整理 PAWOS 桌面', status)] }),
      'agent.rooms.list': { ok: true, items: [] },
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });

    status = 'idle';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    const notificationTrigger = await screen.findByRole('button', { name: '通知中心，1 条通知' });
    fireEvent.click(notificationTrigger);
    expect(await screen.findByText('整理 PAWOS 桌面 已结束运行')).toBeInTheDocument();
  });

  it('keeps one terminal Session notification when stale polling replays the same run transition', async () => {
    let status = 'busy';
    let updatedAtMs = 20;
    const lastTerminalTurnId = 'turn:trace-session:1';
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': () => ({
        ok: true,
        items: [session('trace-session', 'Trace 诊断', status, { lastTerminalTurnId, updatedAtMs })],
      }),
      'agent.rooms.list': { ok: true, items: [] },
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });

    status = 'idle';
    updatedAtMs = 30;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '1 个后台工作正在运行' })).not.toBeInTheDocument());
    await screen.findByRole('button', { name: '通知中心，1 条通知' });

    status = 'busy';
    updatedAtMs = 20;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });
    status = 'idle';
    updatedAtMs = 35;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '1 个后台工作正在运行' })).not.toBeInTheDocument());

    const trigger = await screen.findByRole('button', { name: '通知中心，1 条通知' });
    fireEvent.click(trigger);
    const panel = screen.getByRole('region', { name: '通知中心' });
    expect(within(panel).getAllByText('Trace 诊断 已结束运行')).toHaveLength(1);
  });

  it('retains separate terminal notifications for two real Session runs', async () => {
    let status = 'busy';
    let updatedAtMs = 20;
    let lastTerminalTurnId = 'turn:conversation:1';
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': () => ({
        ok: true,
        items: [session('conversation', '持续优化', status, { lastTerminalTurnId, updatedAtMs })],
      }),
      'agent.rooms.list': { ok: true, items: [] },
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });

    status = 'idle';
    updatedAtMs = 30;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '1 个后台工作正在运行' })).not.toBeInTheDocument());
    await screen.findByRole('button', { name: '通知中心，1 条通知' });
    status = 'busy';
    updatedAtMs = 40;
    lastTerminalTurnId = 'turn:conversation:2';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });
    status = 'idle';
    updatedAtMs = 30;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '1 个后台工作正在运行' })).not.toBeInTheDocument());

    expect(await screen.findByRole('button', { name: '通知中心，2 条通知' })).toBeInTheDocument();
  });

  it('keys Room notifications by the partner terminal turn, independent of title and later timestamps', async () => {
    let status = 'busy';
    let updatedAtMs = 20;
    let lastTerminalTurnId = 'root:desktop-room:1';
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': () => ({
        ok: true,
        items: [session('room-partner', '桌面协作 · Agent 1', status, {
          roomId: 'room-running',
          lastTerminalTurnId,
          updatedAtMs,
        })],
      }),
      'agent.rooms.list': { ok: true, items: [{
        id: 'room-running',
        title: '桌面协作',
        status: 'active',
        updatedAtMs: 30,
        workspaceRoots: ['/work/paw'],
        participants: [],
        workItems: [],
      }] },
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });

    status = 'idle';
    updatedAtMs = 30;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await screen.findByRole('button', { name: '通知中心，1 条通知' });
    status = 'busy';
    updatedAtMs = 20;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });
    status = 'idle';
    updatedAtMs = 35;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '1 个后台工作正在运行' })).not.toBeInTheDocument());
    await screen.findByRole('button', { name: '通知中心，1 条通知' });

    status = 'busy';
    lastTerminalTurnId = 'root:desktop-room:2';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });
    status = 'idle';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));

    expect(await screen.findByRole('button', { name: '通知中心，2 条通知' })).toBeInTheDocument();
  });

  it.each(['queued', 'active', 'review'])('waits for %s Room work across idle partner handoffs before notifying completion', async (initialWorkState) => {
    let status = 'busy';
    let workState = initialWorkState;
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': () => ({ ok: true, items: [
        session('room-partner', '并行协作 · Agent 1', status, { roomId: 'room-running' }),
      ] }),
      'agent.rooms.list': () => ({ ok: true, items: [{
        id: 'room-running', title: '并行协作', status: 'active', updatedAtMs: 30,
        workspaceRoots: ['/work/paw'], participants: [],
        workItems: [{ state: workState, objective: '集成交付', updatedAtMs: 30 }],
      }] }),
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });
    status = 'idle';
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' })); });
    await waitFor(() => expect(screen.queryByRole('button', { name: '1 个后台工作正在运行' })).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: '通知中心，1 条通知' })).not.toBeInTheDocument();
    workState = 'done';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    const trigger = await screen.findByRole('button', { name: '通知中心，1 条通知' });
    fireEvent.click(trigger);
    expect(await screen.findByText('并行协作 已结束运行')).toBeInTheDocument();
  });

  it('warns when a running Room participant faults instead of reporting a normal finish', async () => {
    let status = 'busy';
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': () => ({
        ok: true,
        items: [session('room-partner', '桌面协作 · Agent 1', status, { roomId: 'room-running' })],
      }),
      'agent.rooms.list': { ok: true, items: [{
        id: 'room-running',
        title: '桌面协作',
        status: 'active',
        updatedAtMs: 30,
        workspaceRoots: ['/work/paw'],
        participants: [],
        workItems: [],
      }] },
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });

    status = 'faulted';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    const notificationTrigger = await screen.findByRole('button', { name: '通知中心，1 条通知' });
    fireEvent.click(notificationTrigger);
    expect(await screen.findByText('桌面协作 需要处理')).toBeInTheDocument();
  });

  it('warns when the real Memory maintenance job fails', async () => {
    let state = 'running';
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.rooms.list': { ok: true, items: [] },
      'agent.memoryMaintenance.run': () => ({
        ok: state !== 'failed',
        job: {
          jobId: 'memory-maintenance:active',
          state,
          error: state === 'failed' ? 'provider down' : '',
          updatedAtMs: 42,
        },
      }),
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });

    state = 'failed';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    const notificationTrigger = await screen.findByRole('button', { name: '通知中心，1 条通知' });
    fireEvent.click(notificationTrigger);
    expect(await screen.findByText('自动记忆整理 需要处理')).toBeInTheDocument();
    expect(screen.getByText(/provider down/)).toBeInTheDocument();
  });

  it('keeps one actionable warning when the same Memory timeout is replayed', async () => {
    let state = 'running';
    let updatedAtMs = 42;
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.rooms.list': { ok: true, items: [] },
      'agent.memoryMaintenance.run': () => ({
        ok: state === 'running',
        job: {
          jobId: 'memory-maintenance:timeout-1',
          state,
          error: state === 'expired' ? 'settlement timeout' : '',
          updatedAtMs,
        },
      }),
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });

    state = 'expired';
    updatedAtMs = 80;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '1 个后台工作正在运行' })).not.toBeInTheDocument());
    await screen.findByRole('button', { name: '通知中心，1 条通知' });
    state = 'running';
    updatedAtMs = 42;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });
    state = 'expired';
    updatedAtMs = 80;
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '1 个后台工作正在运行' })).not.toBeInTheDocument());

    const trigger = await screen.findByRole('button', { name: '通知中心，1 条通知' });
    fireEvent.click(trigger);
    const panel = screen.getByRole('region', { name: '通知中心' });
    expect(within(panel).getAllByText('自动记忆整理 需要处理')).toHaveLength(1);
    expect(within(panel).getByText(/settlement timeout/)).toBeInTheDocument();
  });

  it('retains separate Memory notifications for different run ids with identical copy', async () => {
    let state = 'running';
    let jobId = 'memory-maintenance:run-1';
    renderActivity(new MockControlTransport({ routes: {
      'agent.sessions.list': { ok: true, items: [] },
      'agent.rooms.list': { ok: true, items: [] },
      'agent.memoryMaintenance.run': () => ({
        ok: true,
        job: { jobId, state, updatedAtMs: 42 },
      }),
    } }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });

    state = 'completed';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await screen.findByRole('button', { name: '通知中心，1 条通知' });
    jobId = 'memory-maintenance:run-2';
    state = 'running';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));
    await screen.findByRole('button', { name: '1 个后台工作正在运行' });
    state = 'completed';
    fireEvent.click(screen.getByRole('button', { name: '刷新后台目录' }));

    expect(await screen.findByRole('button', { name: '通知中心，2 条通知' })).toBeInTheDocument();
  });
});

function renderActivity(transport: MockControlTransport) {
  return render(
    <GlobalFeedbackProvider>
      <ControlTransportProvider transport={transport}>
        <PawDesktopProvider>
          <PawWorkDirectoryProvider
            initialPollDelayMs={0}
            maintenancePollIntervalMs={60_000}
            pollIntervalMs={60_000}
          >
            <PawBackgroundActivity />
            <PawNotificationCenter />
            <RefreshProbe />
            <WindowProbe />
          </PawWorkDirectoryProvider>
        </PawDesktopProvider>
      </ControlTransportProvider>
    </GlobalFeedbackProvider>,
  );
}

function RefreshProbe() {
  const { refresh } = usePawWorkDirectory();
  return <button onClick={() => void refresh()} type="button">刷新后台目录</button>;
}

function WindowProbe() {
  const ids = usePawDesktopStore((state) => Object.keys(state.windows).sort().join(','));
  return <output data-testid="open-window-ids">{ids}</output>;
}

function session(
  id: string,
  title: string,
  status: string,
  options: { roomId?: string; lastTerminalTurnId?: string; updatedAtMs?: number } = {},
) {
  return {
    id,
    title,
    mode: 'assistant',
    status,
    roleId: 'default',
    roleVersion: '1',
    roleBookRevisionId: 'r1',
    updatedAtMs: options.updatedAtMs ?? 20,
    workspaceRoots: ['/work/paw'],
    ...(options.lastTerminalTurnId ? { lastTerminalTurnId: options.lastTerminalTurnId } : {}),
    ...(options.roomId ? { roomParticipant: { roomId: options.roomId } } : {}),
  };
}
