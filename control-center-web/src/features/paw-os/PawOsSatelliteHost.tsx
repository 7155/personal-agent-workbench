import { useQuery } from '@tanstack/react-query';
import { Activity, BrainCircuit, CalendarDays, CheckCircle2, ChevronDown, CircleAlert, CircleStop, ExternalLink, FileText, FolderKanban, ListChecks, LoaderCircle, MessageSquare, Network, PackageCheck, PackageOpen, PackageX, Power, RefreshCw, RotateCcw, ShieldCheck } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, EmptyState, Skeleton } from '@/components/primitives';
import type { WorkDocumentDetailV1 } from '@/contracts/work-documents';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import { roleItems, sessionItems, sessionPermissionLabel } from '@/features/agent/types';
import { isSubagentRun, subagentRuns } from '@/features/agent/status/subagent-data';
import { arrayRecords, asRecord, formatTime, publicErrorText, StatusBadge, stringValue } from '@/features/overview/management-ui';
import { usePlanningDashboard } from '@/features/planning/api';
import { RoomStatusPanel } from '@/features/rooms/RoomStatusPanel';
import type { RoomSummary } from '@/features/rooms/room-types';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { useRoomLiveSession } from '@/features/rooms/runtime/use-room-live-session';
import { openPawOsRoute, usePawOsDesktop } from './surface-context';
import { routePath } from './model/app-registry';
import type { PawOsWindowTarget } from './model/desktop';
import { roomPartnerSessionWindowRequest, roomPlanetObserverWindowRequest } from '@/paw-os/apps/room-satellite-auto-open';
import { PawRoomLiveFocusOverview } from '@/paw-os/apps/PawRoomLiveFocusOverview';
import { PawRoomGovernance } from '@/paw-os/apps/PawRoomWorkspace';
import { PawRoomConversation } from '@/paw-os/apps/PawRoomConversation';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { SmoothDisclosureReveal } from '@/features/agent/timeline/SmoothDisclosureReveal';
import { toggleDisclosurePreservingAnchor } from '@/features/agent/timeline/disclosure-anchor';
import { buildRoomFocusProjection, roomFocusCelestialName, roomFocusStateLabel, type RoomFocusState } from '@/paw-os/apps/room-focus-projection';
import { RoomActivityGlyph } from '@/paw-os/apps/room-tool-glyph';
import { usePageVisibility } from '@/platform/use-page-visibility';
import './paw-os-satellite.css';
import { SatelliteModelBadge } from './SatelliteModelBadge';

export function PawOsSatelliteHost({ target }: { target: PawOsWindowTarget }) {
  if (target.kind === 'work-document') return <WorkDocumentSatellite documentId={target.id} />;
  if (target.kind === 'project') return <ProjectSatellite target={target} />;
  if (target.kind === 'task') return <PlanningTaskSatellite target={target} />;
  if (target.kind === 'session') return <AgentSessionSatellite target={target} />;
  if (target.kind === 'room') return target.panel
    ? <RoomPanelSatellite target={{ ...target, panel: target.panel }} />
    : null;
  if (target.kind === 'participant') return <RoomParticipantSatellite target={target} />;
  if (target.kind === 'subagent') return <SubagentSatellite target={target} />;
  if (target.kind === 'process-terminal') return <ProcessTerminalSatellite target={target} />;
  if (target.kind === 'browser-target' && target.backgroundObserver) return <BackgroundBrowserSatellite target={target} />;
  if (target.kind === 'package') return <PackageAppSatellite target={target} />;
  if (target.kind === 'result') return null;
  return null;
}

function BackgroundBrowserSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'browser-target' }> }) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const visible = usePageVisibility();
  const snapshot = useQuery({
    queryKey: ['paw-background-browser', target.targetId, target.tabId],
    queryFn: ({ signal }) => transport.request<Record<string, unknown>>({
      pathId: 'browser.snapshot.latest', query: { tabId: target.tabId!, includeMarkdown: true }, signal,
    }),
    enabled: visible && Boolean(target.tabId), refetchInterval: visible ? 2_000 : false, retry: false,
  });
  const view = asRecord(snapshot.data);
  const imageId = stringValue(view.snapshotId);
  const image = imageId && view.hasScreenshot === true ? transport.browserSnapshotImageUrl?.(imageId) : undefined;
  return <section className="paw-os-satellite paw-background-browser">
    <header className="paw-process-terminal__meta">
      <span>后台浏览器</span>
      <Button size="small" variant="quiet" onClick={() => {
        desktop?.setCollaborationFocusGroup?.(null);
        desktop?.openWindow({ appId: 'browser', target: { ...target, id: target.targetId, backgroundObserver: false } });
      }}>打开浏览器</Button>
    </header>
    <p className="paw-background-browser__url">{target.url}</p>
    {image ? <img alt="后台浏览器最近的页面快照" src={image} /> : <p>{stringValue(view.markdown, snapshot.isError ? '页面快照暂时不可用，浏览器继续运行。' : '等待页面快照…')}</p>}
    <small>收起这个窗口，浏览器仍会继续运行。</small>
  </section>;
}

function ProcessTerminalSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'process-terminal' }> }) {
  const transport = useControlTransport();
  const agentProjection = useAgentLiveStore((state) => state.projections[target.sessionId]);
  const roomProjection = useRoomLiveStore((state) => target.roomId ? state.projections[target.roomId] : undefined);
  const activity = useMemo(() => {
    const agentActivity = agentProjection?.activitiesById[target.toolCallId];
    if (agentActivity) return agentActivity;
    return Object.values(roomProjection?.activitiesById ?? {}).find((candidate) => (
      stringValue(candidate.payload.toolCallId) === target.toolCallId
    ));
  }, [agentProjection, roomProjection, target.toolCallId]);
  const job = target.runId ? agentProjection?.backgroundJobsById[target.runId] : undefined;
  const status = job?.status ?? target.runStatus ?? activity?.status ?? 'running';
  const exitCode = job?.exitCode ?? target.exitCode ?? processEvidence(activity?.payload).exitCode;
  const roomBound = job?.causalMetadata.roomBound ?? target.roomBound ?? false;
  const cancellable = Boolean(
    target.sessionId
    && target.runId?.startsWith('bg_')
    && (status === 'queued' || status === 'running')
    && (!roomBound || target.roomTurnId),
  );
  const [confirmingStop, setConfirmingStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState('');
  const [stopNotice, setStopNotice] = useState('');
  const logs = useQuery({
    queryKey: ['paw-os', 'process-terminal', target.sessionId, target.runId],
    queryFn: ({ signal }) => transport.request<Record<string, unknown>>({
      pathId: 'agent.session.backgroundJob.logs',
      params: { sessionId: target.sessionId, jobId: target.runId! },
      query: { cursor: Math.max(0, (job?.outputBytes ?? 0) - 131_072), limitBytes: 131_072 },
      signal,
    }),
    enabled: Boolean(target.runId?.startsWith('bg_')),
    refetchInterval: ['queued', 'running', 'cancelling'].includes(status) ? 700 : false,
    retry: false,
  });
  const evidence = processEvidence(activity?.payload);
  const liveOutput = stringValue(logs.data?.text);

  useEffect(() => {
    if (!cancellable) setConfirmingStop(false);
  }, [cancellable]);

  async function stopJob(): Promise<void> {
    if (!cancellable || !target.runId || stopping) return;
    if (!confirmingStop) {
      setConfirmingStop(true);
      return;
    }
    setStopping(true);
    setStopError('');
    setStopNotice('');
    try {
      const receipt = await transport.request<unknown>({
        pathId: 'agent.session.backgroundJob.cancel',
        params: { sessionId: target.sessionId, jobId: target.runId },
        body: {
          reason: 'control_center_requested',
          ...(target.roomTurnId ? { roomTurnId: target.roomTurnId } : {}),
        },
      });
      useAgentLiveStore.getState().applyBackgroundJobReceipt(target.sessionId, receipt);
      const authoritative = useAgentLiveStore.getState()
        .projections[target.sessionId]?.backgroundJobsById[target.runId];
      if (!authoritative || (authoritative.status !== 'cancelling' && authoritative.status !== 'cancelled')) {
        throw new Error('后台任务停止回执未更新当前运行');
      }
      setStopNotice(stringValue(asRecord(receipt).summary, '已发送停止请求'));
      setConfirmingStop(false);
    } catch (error) {
      setStopError(publicErrorText(error, '暂时无法停止这个后台任务。'));
    } finally {
      setStopping(false);
    }
  }
  return (
    <section className="paw-os-satellite paw-os-satellite--process" data-status={status}>
      <header className="paw-process-terminal__meta">
        <span data-status={status}>{processStatusLabel(status, exitCode)}</span>
        {target.cwd ? <code title={target.cwd}>{target.cwd}</code> : null}
        {cancellable ? <Button disabled={stopping} leadingIcon={stopping ? <LoaderCircle className="ui-spin" size={13} /> : <CircleStop size={13} />} onClick={() => void stopJob()} size="small" variant="danger">{confirmingStop ? '确认停止后台任务' : '停止后台任务'}</Button> : null}
      </header>
      {stopError ? <div className="paw-process-terminal__feedback" data-error role="alert">{stopError}</div> : null}
      {stopNotice ? <div className="paw-process-terminal__feedback" role="status">{stopNotice}</div> : null}
      <pre className="paw-process-terminal__command"><code>$ {target.command}</code></pre>
      <div aria-live="polite" className="paw-process-terminal__output" role="log">
        {liveOutput || evidence.stdout ? <pre data-stream="stdout">{liveOutput || evidence.stdout}</pre> : null}
        {evidence.stderr ? <pre data-stream="stderr">{evidence.stderr}</pre> : null}
        {!liveOutput && !evidence.stdout && !evidence.stderr ? <span>{logs.error ? '真实运行日志暂时不可用。' : status === 'running' ? '等待运行输出…' : '运行没有公开输出。'}</span> : null}
      </div>
    </section>
  );
}

function processEvidence(payload?: Record<string, unknown>): { stdout: string; stderr: string; exitCode?: number } {
  const stdout = deepText(payload, new Set(['stdout', 'output', 'outputPreview', 'text']));
  const stderr = deepText(payload, new Set(['stderr', 'error']));
  const exitCode = deepNumber(payload, new Set(['exitCode', 'exit_code', 'code']));
  return { stdout, stderr, ...(exitCode !== undefined ? { exitCode } : {}) };
}

function deepText(value: unknown, keys: Set<string>, depth = 0): string {
  if (!value || typeof value !== 'object' || depth > 6) return '';
  const source = value as Record<string, unknown>;
  for (const [key, candidate] of Object.entries(source)) {
    if (keys.has(key) && typeof candidate === 'string' && candidate.trim()) return candidate;
  }
  for (const candidate of Object.values(source)) {
    const nested = deepText(candidate, keys, depth + 1);
    if (nested) return nested;
  }
  return '';
}

function deepNumber(value: unknown, keys: Set<string>, depth = 0): number | undefined {
  if (!value || typeof value !== 'object' || depth > 6) return undefined;
  const source = value as Record<string, unknown>;
  for (const [key, candidate] of Object.entries(source)) {
    if (keys.has(key) && typeof candidate === 'number' && Number.isFinite(candidate)) return candidate;
  }
  for (const candidate of Object.values(source)) {
    const nested = deepNumber(candidate, keys, depth + 1);
    if (nested !== undefined) return nested;
  }
  return undefined;
}

function processStatusLabel(status: string, exitCode?: number): string {
  if (status === 'running' || status === 'queued' || status === 'waiting') return '运行中';
  if (status === 'cancelling') return '正在停止';
  if (status === 'aborted' || status === 'cancelled' || status === 'stopped') return '已停止';
  if (status === 'orphaned') return '宿主已断开';
  if (status === 'failed') return exitCode === undefined ? '失败' : `失败 · exit ${exitCode}`;
  return exitCode === undefined ? '已完成' : `已完成 · exit ${exitCode}`;
}

function PackageAppSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'package' }> }) {
  const desktop = usePawOsDesktop();
  const transport = useControlTransport();
  const [pending, setPending] = useState<Record<string, unknown>>({});
  const [operation, setOperation] = useState('');
  const [operationError, setOperationError] = useState('');
  const [operationResult, setOperationResult] = useState('');
  const [applying, setApplying] = useState(false);
  const packages = useQuery({
    queryKey: ['agent', 'package-satellite', target.id],
    queryFn: async ({ signal }) => {
      const [installed, catalog] = await Promise.all([
        transport.request({ pathId: 'agent.extensions.list', signal }),
        transport.request({ pathId: 'agent.extensions.catalog', signal }),
      ]);
      return { installed: asRecord(installed), catalog: asRecord(catalog) };
    },
    staleTime: 5_000,
  });
  const installed = arrayRecords(packages.data?.installed.items).find((item) => stringValue(item.id) === target.id);
  const catalog = arrayRecords(packages.data?.catalog.items).find((item) => stringValue(item.id) === target.id);
  const enabled = installed?.enabled === true;
  const updateAvailable = catalog?.updateAvailable === true;
  const rollbackAvailable = installed?.rollbackAvailable === true;
  const version = stringValue(installed?.version, stringValue(catalog?.latestVersion, target.version || '未声明'));
  const resourceCount = packageResourceCount(installed) || target.resourceCount;

  async function previewOperation(nextOperation: 'install' | 'update' | 'enable' | 'disable' | 'rollback' | 'uninstall'): Promise<void> {
    setOperation(nextOperation);
    setOperationError('');
    setOperationResult('');
    setApplying(true);
    try {
      let validationToken = '';
      if (nextOperation === 'install' || nextOperation === 'update') {
        const validation = asRecord(await transport.request({
          pathId: 'agent.extensions.validate',
          body: {
            catalogId: target.id,
            catalogVersion: stringValue(catalog?.latestVersion, target.version),
          },
        }));
        validationToken = stringValue(validation.validationToken);
      }
      const preview = asRecord(await transport.request({
        pathId: 'agent.extensions.preview',
        body: nextOperation === 'install' || nextOperation === 'update'
          ? { action: nextOperation, validationToken, enable: installed ? enabled : true }
          : { action: nextOperation, pluginId: target.id },
      }));
      setPending(preview);
    } catch (error) {
      setOperationError(publicErrorText(error, '没有生成 Package 更改预览，请重试。'));
    } finally {
      setApplying(false);
    }
  }

  async function applyOperation(): Promise<void> {
    setOperationError('');
    setApplying(true);
    try {
      await transport.request({
        pathId: 'agent.extensions.apply',
        body: {
          previewToken: stringValue(pending.previewToken),
          payloadSha256: stringValue(pending.payloadSha256),
          confirmText: 'apply',
        },
      });
      setPending({});
      setOperationResult(`${packageOperationLabel(operation)}已完成。`);
      await packages.refetch();
    } catch (error) {
      setOperationError(publicErrorText(error, 'Package 更改没有完成，请重试。'));
    } finally {
      setApplying(false);
    }
  }

  return (
    <section className="paw-os-satellite paw-os-satellite--package">
      {packages.isPending ? <div className="paw-os-satellite__loading" role="status"><Skeleton /><Skeleton /><Skeleton /></div> : null}
      {packages.error ? <SatelliteLoadError error={packages.error} icon={PackageOpen} onRetry={() => void packages.refetch()} title="Package 状态没有打开" /> : null}
      {!packages.isPending && !packages.error ? <div className="paw-os-satellite__entity-body">
        <section>
          <span>说明</span>
          <p>{target.subtitle || '暂无说明'}</p>
        </section>
        <dl>
          <Fact label="Package" value={target.id} wide />
          <Fact label="版本" value={version} />
          <Fact label="资源" value={`${resourceCount} 项`} />
        </dl>
        <div className="paw-os-satellite__entity-actions">
          {!installed ? <Button disabled={catalog?.actionable === false || applying} leadingIcon={<PackageCheck size={15} />} loading={applying && operation === 'install'} onClick={() => void previewOperation('install')}>安装</Button> : null}
          {installed && updateAvailable ? <Button disabled={applying} leadingIcon={<RefreshCw size={15} />} loading={applying && operation === 'update'} onClick={() => void previewOperation('update')}>更新</Button> : null}
          {installed ? <Button disabled={applying} leadingIcon={<Power size={15} />} loading={applying && operation === (enabled ? 'disable' : 'enable')} onClick={() => void previewOperation(enabled ? 'disable' : 'enable')} variant="quiet">{enabled ? '停用' : '启用'}</Button> : null}
          {installed ? <Button disabled={!rollbackAvailable || applying} leadingIcon={<RotateCcw size={15} />} loading={applying && operation === 'rollback'} onClick={() => void previewOperation('rollback')} variant="quiet">恢复上一版本</Button> : null}
          {installed ? <Button disabled={applying} leadingIcon={<PackageX size={15} />} loading={applying && operation === 'uninstall'} onClick={() => void previewOperation('uninstall')} variant="quiet">卸载</Button> : null}
          <Button leadingIcon={<ExternalLink size={15} />} onClick={() => desktop?.openApp?.('app-center', '/plugins')} variant="quiet">App Center</Button>
        </div>
        {pending.previewToken ? <section className="paw-os-satellite__package-confirm" role="status"><ShieldCheck size={17} /><div><strong>确认{packageOperationLabel(operation)}</strong><p>{packagePreviewSummary(pending, target.title)}</p></div><footer><Button disabled={applying} onClick={() => { setPending({}); setOperation(''); }} size="small" variant="quiet">取消</Button><Button disabled={applying} loading={applying} onClick={() => void applyOperation()} size="small">确认更改</Button></footer></section> : null}
        {operationError ? <section className="paw-os-satellite__package-feedback" data-error role="alert"><CircleAlert size={16} /><span>{operationError}</span><Button disabled={applying} onClick={() => void previewOperation(operation as 'install' | 'update' | 'enable' | 'disable' | 'rollback' | 'uninstall')} size="small" variant="quiet">重试</Button></section> : null}
        {operationResult ? <section className="paw-os-satellite__package-feedback" role="status"><ShieldCheck size={16} /><span>{operationResult}</span></section> : null}
      </div> : null}
    </section>
  );
}

function packageOperationLabel(operation: string): string {
  return ({ install: '安装', update: '更新', enable: '启用', disable: '停用', rollback: '恢复上一版本', uninstall: '卸载' } as Record<string, string>)[operation] ?? '更改';
}

function packageResourceCount(value?: Record<string, unknown>): number {
  const resources = asRecord(value?.resources);
  return ['extensions', 'skills', 'prompts', 'themes']
    .reduce((count, key) => count + (Array.isArray(resources[key]) ? resources[key].length : 0), 0);
}

function packagePreviewSummary(preview: Record<string, unknown>, fallbackTitle: string): string {
  const summary = asRecord(preview.summary);
  const name = stringValue(summary.displayName, fallbackTitle);
  const version = stringValue(summary.version);
  return `${name}${version ? ` · v${version}` : ''}。更改只会在确认后应用。`;
}

function AgentSessionSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'session' }> }) {
  const desktop = usePawOsDesktop();
  const transport = useControlTransport();
  const sessions = useQuery({
    queryKey: ['agent', 'sessions', 'satellite'],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.sessions.list',
      query: { limit: 100, includeArchived: true },
      signal,
    }),
    staleTime: 15_000,
  });
  const session = sessionItems(sessions.data).find((candidate) => candidate.id === target.id);
  return (
    <section className="paw-os-satellite paw-os-satellite--session">
      {sessions.isPending ? <div className="paw-os-satellite__loading" role="status"><Skeleton /><Skeleton /><Skeleton /></div> : null}
      {sessions.error ? <SatelliteLoadError error={sessions.error} icon={MessageSquare} onRetry={() => void sessions.refetch()} title="Session 没有打开" /> : null}
      {!sessions.isPending && !sessions.error && !session ? <SatelliteMissing copy="这段 Session 已不在 Pi 的 Session 目录中。" icon={MessageSquare} route="agent" title="找不到这段 Session" /> : null}
      {session ? (
        <div className="paw-os-satellite__entity-body">
          <section><span>最近内容</span><p>{session.lastMessagePreview || '还没有公开消息摘要。'}</p></section>
          <dl>
            <Fact label="权限" value={sessionPermissionLabel(session)} />
            <Fact label="模式" value={session.mode} />
            <Fact label="消息" value={String(session.messageCount ?? 0)} />
            <Fact label="最近更新" value={formatTime(session.updatedAtMs)} />
            <Fact label="工作目录" value={session.workspaceRoots.join(' · ') || '未绑定'} wide />
          </dl>
          <Button leadingIcon={<ExternalLink size={15} />} onClick={() => openPawOsRoute(desktop, `${routePath('agent')}?session=${encodeURIComponent(target.id)}`)}>
            在 Agent 中继续对话
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function RoomPanelSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'room' }> & { panel: NonNullable<Extract<PawOsWindowTarget, { kind: 'room' }>['panel']> } }) {
  const desktop = usePawOsDesktop();
  const transport = useControlTransport();
  const roomQuery = useRoomDetail(target.id);
  const room = roomFromResponse(roomQuery.data, target.id);
  const projection = useRoomLiveStore((state) => state.projections[target.id]);
  const focus = useMemo(
    () => room && target.panel === 'focus' ? buildRoomFocusProjection(room, projection) : undefined,
    [projection, room, target.panel],
  );
  const rolesQuery = useQuery({
    queryKey: ['agent', 'roles', 'room-panel'],
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.roles.list', signal }),
    staleTime: 30_000,
    enabled: target.panel === 'governance',
  });
  const personas = useMemo(() => roleItems(rolesQuery.data), [rolesQuery.data]);
  const [error, setError] = useState('');
  const refresh = async () => { await roomQuery.refetch(); };
  const openParticipant = (participantId: string) => {
    const participant = room?.participants.find((candidate) => candidate.id === participantId);
    if (!participant) return;
    desktop?.openWindow(roomPlanetObserverWindowRequest(participant, target.id));
  };
  const openParticipantSession = (participantId: string) => {
    const participant = room?.participants.find((candidate) => candidate.id === participantId);
    if (!participant) return;
    desktop?.openWindow(roomPartnerSessionWindowRequest(participant));
  };
  return (
    <section className="paw-os-satellite paw-os-satellite--room-panel" data-panel={target.panel}>
      {roomQuery.isPending ? <div className="paw-os-satellite__loading" role="status"><Skeleton /><Skeleton /></div> : null}
      {roomQuery.error ? <SatelliteLoadError error={roomQuery.error} icon={Network} onRetry={() => void roomQuery.refetch()} title="Room 面板没有打开" /> : null}
      {error ? <div className="paw-os-satellite__feedback" role="alert">{error}</div> : null}
      {!roomQuery.isPending && !roomQuery.error && !room ? <SatelliteMissing actionLabel="回到 Room" copy="这个 Room 已不在当前 Room 清单中，可能已归档或删除。" icon={Network} route="rooms" title="找不到这个 Room" /> : null}
      {room ? (
        <div className="paw-os-satellite__room-panel-body">
          {target.panel === 'focus' && focus ? <PawRoomLiveFocusOverview roomId={target.id} focus={focus} onOpenParticipant={openParticipant} /> : null}
          {target.panel === 'progress' ? <RoomStatusPanel room={room} roomId={target.id} projection={projection} open /> : null}
          {target.panel === 'governance' ? <PawRoomGovernance personas={personas} room={room} onError={setError} onOpenSession={openParticipantSession} onRefresh={refresh} onRoomUpdated={() => { void roomQuery.refetch(); }} /> : null}
        </div>
      ) : null}
    </section>
  );
}

function RoomParticipantSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'participant' }> }) {
  const desktop = usePawOsDesktop();
  const transport = useControlTransport();
  const pageVisible = usePageVisibility();
  const roomQuery = useRoomDetail(target.roomId);
  const [liveRoomResponse, setLiveRoomResponse] = useState<unknown>();
  const [liveError, setLiveError] = useState<unknown>();
  const [liveState, setLiveState] = useState<'recovering' | 'failed' | 'synced'>('recovering');
  const room = roomFromResponse(liveRoomResponse, target.roomId)
    ?? roomFromResponse(roomQuery.data, target.roomId);
  const participant = room?.participants.find((candidate) => candidate.id === target.id);
  const projection = useRoomLiveStore((state) => state.projections[target.roomId]);
  const retryLive = useRoomLiveSession({
    active: pageVisible,
    roomId: target.roomId,
    transport,
    onLoadingChange: () => undefined,
    onSnapshot: (_roomId, snapshot) => setLiveRoomResponse({ room: snapshot.room }),
    onMetadata: (_roomId, response) => setLiveRoomResponse(response),
    onConnectionRestored: () => setLiveError(undefined),
    onConnectionError: (_roomId, error) => setLiveError(error),
    onRecoveryState: (_roomId, state) => setLiveState(state),
    onEvents: () => undefined,
  });
  const focusPartner = useMemo(() => (
    room ? buildRoomFocusProjection(room, projection).partners.find((partner) => partner.participantId === target.id) : undefined
  ), [projection, room, target.id]);
  const retry = () => {
    setLiveError(undefined);
    retryLive();
    void roomQuery.refetch();
  };
  const loadError = !room && !projection ? roomQuery.error ?? liveError : undefined;
  const loading = !room && !loadError && (roomQuery.isPending || liveState === 'recovering');
  return (
    <section className="paw-os-satellite paw-os-satellite--participant-chat" data-presentation="planet-observer">
      <SatelliteModelBadge sessionId={participant?.sessionId || target.sessionId} />
      {loading ? <div className="paw-os-satellite__loading" role="status"><Skeleton /><Skeleton /><Skeleton /></div> : null}
      {loadError ? <SatelliteLoadError error={loadError} icon={MessageSquare} onRetry={retry} title="行星窗口没有打开" /> : null}
      {liveState === 'failed' && (room || projection) ? (
        <div className="paw-os-satellite__feedback" role="alert">
          <span>{publicErrorText(liveError, '实时同步已暂停，已加载的记录仍然可读。')}</span>
          <Button onClick={retry} size="small">恢复实时同步</Button>
        </div>
      ) : null}
      {liveState === 'recovering' && projection ? (
        <div className="paw-os-satellite__feedback" role="status">正在恢复实时同步；已加载的记录仍然可读。</div>
      ) : null}
      {!loading && !loadError && room && !participant ? <SatelliteMissing copy="这颗行星已经不在当前 Room 中。" icon={MessageSquare} route="rooms" title="找不到这颗行星" /> : null}
      {participant && room ? (
        <>
          {/* UR-056：窗口标题栏已标识行星身份，内容区只保留该行星的真实公开
              对话与运行轨迹；WorkItem/责任摘要留在 Room 主窗。The transcript is
              the same shared conversation surface the Room reads, scoped to
              this partner's public lane, so a long history stays virtualized
              instead of windowed behind a「加载更早」boundary. */}
          {projection ? <PawRoomConversation
            empty={<div className="paw-participant-chat__empty"><MessageSquare size={17} /><span>还没有消息或执行轨迹</span></div>}
            participantId={participant.id}
            projection={projection}
            readOnly
            room={room}
          /> : null}
          {/* PF-CM-013 / UR-170：行星窗只补一条极薄状态行。它没有第二个
              composer；Trace 与完整 Session 入口都保持为导航，不在观察窗改状态。 */}
          <SatelliteStatusline
            action={{
              label: 'Session',
              ariaLabel: `在 Agent 中打开 ${focusPartner?.celestialName ?? roomFocusCelestialName(participant.ordinal)} 的完整 Session`,
              route: `${routePath('agent')}?session=${encodeURIComponent(participant.sessionId)}`,
            }}
            currentWork={conciseParticipantEntry(focusPartner?.currentAction ?? '', satelliteStatuslineFallback(focusPartner?.state ?? 'idle'))}
            state={focusPartner?.state ?? 'idle'}
            traceAction={{
              ariaLabel: `查看 ${focusPartner?.celestialName ?? roomFocusCelestialName(participant.ordinal)} 的 Trace`,
              route: `${routePath('observability')}?sessionId=${encodeURIComponent(participant.sessionId)}`,
            }}
          />
        </>
      ) : null}
    </section>
  );
}

function SatelliteStatusline({ action, currentWork, state, traceAction }: {
  action?: { ariaLabel: string; label: string; route: string };
  currentWork: string;
  state: RoomFocusState;
  traceAction?: { ariaLabel: string; route: string };
}) {
  const desktop = usePawOsDesktop();
  return (
    <footer aria-label="当前工作与状态" className="paw-participant-chat__statusline" data-state={state}>
      <span className="paw-participant-chat__statusline-state"><i aria-hidden="true" />{roomFocusStateLabel(state)}</span>
      <p title={currentWork}>{currentWork}</p>
      {traceAction ? (
        <button
          aria-label={traceAction.ariaLabel}
          onClick={() => openPawOsRoute(desktop, traceAction.route)}
          type="button"
        >
          <Activity aria-hidden="true" size={12} />
          <span>Trace</span>
        </button>
      ) : null}
      {action ? (
        <button
          aria-label={action.ariaLabel}
          onClick={() => openPawOsRoute(desktop, action.route)}
          type="button"
        >
          <ExternalLink aria-hidden="true" size={12} />
          <span>{action.label}</span>
        </button>
      ) : null}
    </footer>
  );
}

/** 工具/运行事件压成一行：状态 · 类别 logo · 消息（可截断）· 时间弱化在行尾。
 *  类别用 logo 替代文字（框本来就小，图4）；完整含义留在 aria-label/title。
 *  失败沿用红色警示图标；超出摘要的公开原文折在同一披露里。 */
function SatelliteActivityRow({ direction, eventType, message, rawContentId, rawLabel, rawText, status, time }: {
  direction: 'in' | 'out';
  eventType: string;
  message: string;
  rawContentId: string;
  rawLabel: string;
  rawText: string;
  status: string;
  time: number;
}) {
  const hasRaw = rawText.trim() !== message.trim();
  return <article data-direction={direction} data-event-type={eventType} data-kind="activity" data-status={status}>
    <span className="paw-participant-chat__activity-state"><SatelliteRunState eventType={eventType} status={status} /></span>
    <strong className="paw-participant-chat__activity-glyph"><RoomActivityGlyph eventType={eventType} /></strong>
    <span className="paw-participant-chat__activity-message" title={message}>{message}</span>
    <time>{time ? formatTime(time) : ''}</time>
    {hasRaw ? (
      <SatelliteDisclosure className="paw-participant-chat__raw-detail" contentId={rawContentId} summary={<span>{rawLabel}</span>}>
        <pre>{rawText}</pre>
      </SatelliteDisclosure>
    ) : null}
  </article>;
}

function SatelliteDisclosure({
  active = false,
  children,
  className,
  contentId,
  dataActive = false,
  summary,
}: {
  active?: boolean;
  children: ReactNode;
  className: string;
  contentId: string;
  dataActive?: boolean;
  summary: ReactNode;
}) {
  const [open, setOpen] = useState(active);
  const manuallyToggled = useRef(false);
  const wasActive = useRef(active);
  useEffect(() => {
    if (active && !wasActive.current && !manuallyToggled.current) setOpen(true);
    wasActive.current = active;
  }, [active]);
  return <section className={className} data-active={dataActive || undefined} data-open={open || undefined}>
    <button
      aria-controls={contentId}
      aria-expanded={open}
      className={`${className}__summary`}
      onClick={(event) => {
        manuallyToggled.current = true;
        toggleDisclosurePreservingAnchor(event, setOpen);
      }}
      type="button"
    >
      {summary}
      <ChevronDown aria-hidden="true" className="paw-participant-chat__disclosure-chevron" size={14} />
    </button>
    <SmoothDisclosureReveal className="paw-participant-chat__disclosure-reveal" id={contentId} innerClassName="paw-participant-chat__disclosure-inner" open={open}>
      {children}
    </SmoothDisclosureReveal>
  </section>;
}

function SatelliteRawDetail({ contentId, label, text }: { contentId: string; label: string; text: string }) {
  return <SatelliteDisclosure className="paw-participant-chat__raw-detail" contentId={contentId} summary={<span>{label}</span>}>
    <pre>{text}</pre>
  </SatelliteDisclosure>;
}

function conciseParticipantEntry(detail: string, fallback: string): string {
  const source = detail.trim();
  if (!source || participantDetailIsRaw(source) || participantDetailIsMachineToken(source)) return fallback;
  const compact = source
    .replace(/```[\s\S]*?```/gu, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/gu, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gmu, '')
    .replace(/[*_`]/gu, '')
    .replace(/(?:^|\s)[>~-]+/gu, ' ')
    .replace(/\s+/gu, ' ')
    .trim();
  if (!compact) return fallback;
  return compact.length > 150 ? `${compact.slice(0, 147).trimEnd()}…` : compact;
}

function participantDetailIsRaw(source: string): boolean {
  return /```|(?:^|\s)[{[]\s*["']/u.test(source)
    || /\/(?:Users|Volumes|home|private|tmp|var)\//u.test(source)
    || /\b[a-f\d]{48,}\b/iu.test(source)
    || /["'](?:path|sha256|payload|metadata)["']\s*:/iu.test(source);
}

/** 形如 participant_activity 的事件枚举是机器串，不能作为给人看的摘要。 */
function participantDetailIsMachineToken(source: string): boolean {
  return /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/iu.test(source);
}

/** 页脚兜底跟随状态给一句用户可读的话，而不是机器事件名。 */
function satelliteStatuslineFallback(state: RoomFocusState): string {
  if (state === 'running') return '正在推进当前工作';
  if (state === 'completed') return '活动已完成';
  if (state === 'failed' || state === 'blocked') return '最近一项活动需要关注';
  if (state === 'waiting' || state === 'review') return '等待下一步安排';
  return '等待新的工作项';
}

function SubagentSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'subagent' }> }) {
  const transport = useControlTransport();
  const desktop = usePawOsDesktop();
  const timelineRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const runsQuery = useQuery({
    queryKey: ['agent', 'subagent-satellite', 'runs', target.sessionId],
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.subagents.list', query: { sessionId: target.sessionId, limit: 50 }, signal }),
    refetchInterval: 5_000,
    retry: false,
  });
  const runs = useMemo(() => subagentRuns(runsQuery.data), [runsQuery.data]);
  const listRun = useMemo(() => runs.find((candidate) => candidate.id === target.id), [runs, target.id]);
  const consoleQuery = useQuery({
    queryKey: ['agent', 'subagent-satellite', target.sessionId, target.id],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.subagent.console',
      params: { runId: target.id },
      query: { sessionId: target.sessionId },
      signal,
    }),
    enabled: Boolean(listRun),
    refetchInterval: (query) => {
      const projected = subagentRunProjection(listRun, query.state.data);
      return projected && subagentRunIsActive(projected.state) ? 1_000 : 5_000;
    },
    retry: false,
  });
  const run = useMemo(() => {
    const consoleRun = subagentConsoleRun(consoleQuery.data);
    if (!listRun) return consoleRun;
    return subagentRunProjection(listRun, consoleQuery.data);
  }, [consoleQuery.data, listRun]);
  const entries = useMemo(
    () => subagentTimeline(consoleQuery.data, run?.state),
    [consoleQuery.data, run?.state],
  );
  const timelineItems = useMemo(() => subagentTimelineItems(entries), [entries]);
  useEffect(() => {
    const timeline = timelineRef.current;
    if (timeline && followLatestRef.current) timeline.scrollTop = timeline.scrollHeight;
  }, [entries.at(-1)?.id]);
  const error = runsQuery.error || consoleQuery.error;
  return (
    <section className="paw-os-satellite paw-os-satellite--participant-chat paw-os-satellite--subagent-chat">
      <SatelliteModelBadge sessionId={run?.childSessionId} />
      {runsQuery.isPending || (run && !error && consoleQuery.isPending) ? <div className="paw-os-satellite__loading" role="status"><Skeleton /><Skeleton /><Skeleton /></div> : null}
      {error ? <SatelliteLoadError error={error} icon={MessageSquare} onRetry={() => { void runsQuery.refetch(); void consoleQuery.refetch(); }} title="子 Agent 窗口没有打开" /> : null}
      {!runsQuery.isPending && !runsQuery.error && !run ? <SatelliteMissing copy="这个子 Agent 已不在当前 Session 的运行图中。" icon={MessageSquare} route="agent" title="找不到这个子 Agent" /> : null}
      {run && !error && !consoleQuery.isPending ? (
        <>
          <div aria-label={`子 Agent ${target.title || run.task || target.id} 公开对话与运行事件`} aria-live="polite" className="paw-participant-chat__timeline" onScroll={(event) => { followLatestRef.current = timelineNearLatest(event.currentTarget); }} ref={timelineRef} role="log">
            <SubagentHistoryBoundary loadedRunCount={runs.length} onOpenAgent={() => openPawOsRoute(desktop, routePath('agent'))} />
            {subagentRunFailed(run) && run.error.trim() ? (
              <section className="paw-participant-chat__failure" data-state={run.state} role="alert">
                <div>
                  <CircleAlert aria-hidden="true" size={14} />
                  <span title={run.error}>失败原因：{subagentFailureSummary(run.error)}</span>
                </div>
                <SatelliteRawDetail
                  contentId={`subagent-failure-${run.id.replace(/[^a-zA-Z0-9_-]/gu, '-')}`}
                  label="查看完整失败原因"
                  text={run.error}
                />
              </section>
            ) : null}
            {entries.length ? <div className="paw-participant-chat__history-boundary" data-unknown-total role="status">控制台已加载 {entries.length} 条真实记录；更早历史此处暂不可加载。</div> : null}
            {timelineItems.map((item) => item.kind === 'activity-group' ? (
              <SatelliteDisclosure active={item.active} className="paw-participant-chat__activity-group" contentId={`subagent-activity-${item.id}`} dataActive={item.active} key={item.id} summary={(
                <>
                  <span><strong>运行记录</strong><small>{item.entries.length} 条真实事件 · {item.entries.at(-1)?.text}</small></span>
                  <SatelliteRunState eventType={item.entries.at(-1)?.eventType ?? 'run'} status={item.entries.at(-1)?.status ?? 'completed'} />
                </>
              )}>
                <div className="paw-participant-chat__activity-group-content">{item.entries.map((entry) => <SatelliteTimelineEntry entry={entry} key={entry.id} />)}</div>
              </SatelliteDisclosure>
            ) : <SatelliteTimelineEntry entry={item} key={item.id} />)}
            {!entries.length ? <div className="paw-participant-chat__empty"><MessageSquare size={17} /><span>{run.state === 'queued' ? '等待开始' : '还没有公开进度'}</span></div> : null}
          </div>
          <SatelliteStatusline
            action={{
              ariaLabel: '在 Agent 中打开所属 Session',
              label: 'Session',
              route: `${routePath('agent')}?session=${encodeURIComponent(target.sessionId)}`,
            }}
            currentWork={conciseParticipantEntry(run.task || run.todoTask, satelliteStatuslineFallback(subagentFocusState(run.state)))}
            state={subagentFocusState(run.state)}
            traceAction={{
              ariaLabel: '查看子 Agent Trace',
              route: `${routePath('observability')}?runId=${encodeURIComponent(target.id)}`,
            }}
          />
        </>
      ) : null}
    </section>
  );
}
const SUBAGENT_TERMINAL_STATES = ['completed', 'failed', 'aborted', 'timed_out'] as const;
type SubagentTerminalState = typeof SUBAGENT_TERMINAL_STATES[number];

function subagentRunIsTerminal(state: string): state is SubagentTerminalState {
  return SUBAGENT_TERMINAL_STATES.includes(state as SubagentTerminalState);
}

function subagentRunIsActive(state: AgentSubagentRunV1['state']): boolean {
  return state === 'queued' || state === 'running';
}

function subagentConsoleRun(value: unknown): AgentSubagentRunV1 | undefined {
  const candidate = asRecord(asRecord(value).run);
  return isSubagentRun(candidate) ? candidate : undefined;
}

function subagentRunFreshness(run: AgentSubagentRunV1): number {
  return Math.max(run.updatedAtMs, run.completedAtMs ?? 0);
}

function mergeSubagentRunData(primary: AgentSubagentRunV1, fallback: AgentSubagentRunV1): AgentSubagentRunV1 {
  if (primary.state !== fallback.state || !subagentRunIsTerminal(primary.state)) return primary;
  return {
    ...primary,
    result: Object.keys(primary.result).length ? primary.result : fallback.result,
    error: primary.error || fallback.error,
    completedAtMs: primary.completedAtMs ?? fallback.completedAtMs,
    resultContextScheduledAtMs: primary.resultContextScheduledAtMs ?? fallback.resultContextScheduledAtMs,
    ...(primary.structuredOutput !== undefined
      ? { structuredOutput: primary.structuredOutput }
      : fallback.structuredOutput !== undefined
        ? { structuredOutput: fallback.structuredOutput }
        : {}),
  };
}

function subagentRunProjection(
  listRun: AgentSubagentRunV1 | undefined,
  consoleSnapshot: unknown,
): AgentSubagentRunV1 | undefined {
  const consoleRun = subagentConsoleRun(consoleSnapshot);
  if (!listRun) return consoleRun;
  if (!consoleRun) return listRun;

  const listTerminal = subagentRunIsTerminal(listRun.state);
  const consoleTerminal = subagentRunIsTerminal(consoleRun.state);
  if (listTerminal && !consoleTerminal) return listRun;
  if (!listTerminal && consoleTerminal) return mergeSubagentRunData(consoleRun, listRun);
  if (!listTerminal && !consoleTerminal) {
    if (listRun.state === 'running' && consoleRun.state === 'queued') return listRun;
    return mergeSubagentRunData(consoleRun, listRun);
  }
  const preferred = subagentRunFreshness(consoleRun) >= subagentRunFreshness(listRun) ? consoleRun : listRun;
  const fallback = preferred === consoleRun ? listRun : consoleRun;
  return mergeSubagentRunData(preferred, fallback);
}

function subagentRunFailed(run: AgentSubagentRunV1): boolean {
  return run.state === 'failed' || run.state === 'timed_out';
}

function subagentFailureSummary(error: string): string {
  const compact = error.replace(/\s+/gu, ' ').trim();
  return compact.length > 160 ? `${compact.slice(0, 157).trimEnd()}…` : compact;
}

function subagentFocusState(state: string): RoomFocusState {
  if (state === 'queued') return 'waiting';
  if (state === 'running') return 'running';
  if (state === 'completed') return 'completed';
  if (state === 'failed' || state === 'timed_out') return 'failed';
  if (state === 'aborted') return 'stopped';
  return 'idle';
}

type SubagentTimelineEntry = { id: string; actor: string; direction: 'in' | 'out'; kind: 'message' | 'activity' | 'inbox'; text: string; time: number; eventType: string; status: string };
type SubagentTimelineItem = SubagentTimelineEntry | { id: string; kind: 'activity-group'; active: boolean; entries: SubagentTimelineEntry[] };

function SubagentHistoryBoundary({ loadedRunCount, onOpenAgent }: { loadedRunCount: number; onOpenAgent: () => void }) {
  return <div className="paw-participant-chat__history-boundary" data-unknown-total role="status">
    <span>子 Agent 目录当前加载 {loadedRunCount} 条；接口未提供总数，较早运行可能未加载（每次最多 50 条）。</span>
    <button onClick={onOpenAgent} type="button">在 Agent 中查看</button>
  </div>;
}

function timelineNearLatest(timeline: HTMLDivElement): boolean {
  return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight <= 48;
}

function SatelliteTimelineEntry({ entry }: { entry: SubagentTimelineEntry }) {
  const summary = conciseParticipantEntry(entry.text, entry.kind === 'activity' ? '运行状态已更新' : '公开消息已更新');
  if (entry.kind === 'activity') {
    return <SatelliteActivityRow
      direction={entry.direction}
      eventType={entry.eventType}
      message={summary}
      rawContentId={`subagent-raw-${entry.id}`}
      rawLabel="查看完整原文"
      rawText={entry.text}
      status={entry.status}
      time={entry.time}
    />;
  }
  return <article data-direction={entry.direction} data-kind={entry.kind} data-status={entry.status}>
    <header><strong>{entry.actor}</strong><time>{entry.time ? formatTime(entry.time) : ''}</time><SatelliteRunState eventType={entry.eventType} status={entry.status} /></header>
    <p>{summary}</p>
    {entry.text.trim() !== summary.trim() ? <SatelliteRawDetail contentId={`subagent-raw-${entry.id}`} label="查看完整原文" text={entry.text} /> : null}
  </article>;
}

function subagentTimelineItems(entries: SubagentTimelineEntry[]): SubagentTimelineItem[] {
  const items: SubagentTimelineItem[] = [];
  for (const entry of entries) {
    const previous = items.at(-1);
    if (entry.kind === 'activity' && previous?.kind === 'activity-group') {
      previous.entries.push(entry);
      previous.active = subagentEntryActive(entry);
    } else if (entry.kind === 'activity') {
      items.push({ id: `activity-group:${entry.id}`, kind: 'activity-group', active: subagentEntryActive(entry), entries: [entry] });
    } else {
      items.push(entry);
    }
  }
  return items;
}

function subagentEntryActive(entry: SubagentTimelineEntry): boolean {
  return ['queued', 'running', 'waiting', 'pending', 'streaming', 'claimed'].includes(entry.status.toLowerCase());
}

function subagentTimeline(value: unknown, terminalState?: AgentSubagentRunV1['state']): SubagentTimelineEntry[] {
  const snapshot = asRecord(value);
  const conversation = asRecord(snapshot.conversation);
  const messages = arrayRecords(conversation.items).map((item, index): SubagentTimelineEntry => ({
    id: stringValue(item.id, `message:${index}`),
    actor: stringValue(item.role) === 'user' ? '主 Session' : '子 Agent',
    direction: stringValue(item.role) === 'user' ? 'in' : 'out',
    kind: 'message',
    eventType: stringValue(item.status) === 'streaming' ? 'message_streaming' : 'message',
    status: stringValue(item.status, 'completed'),
    text: satelliteMessageText(item) || '该消息没有可公开展示的正文',
    time: Number(item.createdAtMs) || 0,
  }));
  const activity = arrayRecords(snapshot.activity).map((item, index): SubagentTimelineEntry => {
    const eventType = stringValue(item.eventType);
    const payload = asRecord(item.payload);
    const summary = stringValue(item.summary) || stringValue(payload.summary) || subagentActivityLabel(eventType, payload);
    const toolError = ['tool_failed', 'tool_error'].includes(eventType)
      ? deepText(payload, new Set(['error', 'errorMessage', 'stderr']))
      : '';
    return {
      id: stringValue(item.id, `activity:${index}`),
      actor: '运行进度',
      direction: 'out',
      kind: 'activity',
      eventType,
      status: subagentEventStatus(
        eventType,
        payload,
        stringValue(item.status, stringValue(item.state)),
      ),
      text: toolError && !summary.includes(toolError) ? `${summary}：${toolError}` : summary,
      time: Number(item.createdAtMs) || 0,
    };
  });
  const inbox = arrayRecords(snapshot.inbox).map((item, index): SubagentTimelineEntry => ({
    id: stringValue(item.id, `inbox:${index}`),
    actor: stringValue(item.kind) === 'need_decision' ? '需要决定' : '子 Agent',
    direction: 'out',
    kind: 'inbox',
    eventType: stringValue(item.kind, 'inbox'),
    status: stringValue(item.status, 'completed'),
    text: stringValue(item.message) || stringValue(item.title) || '子 Agent 发来了一条公开进度',
    time: Number(item.createdAtMs) || 0,
  }));
  const entries = [...messages, ...activity, ...inbox]
    .sort((left, right) => left.time - right.time);
  return subagentTimelineSettled(entries, terminalState);
}

function subagentTimelineSettled(
  entries: SubagentTimelineEntry[],
  terminalState?: AgentSubagentRunV1['state'],
): SubagentTimelineEntry[] {
  if (!terminalState || !subagentRunIsTerminal(terminalState)) return entries;
  const settledStatus = terminalState === 'failed' || terminalState === 'timed_out'
    ? 'failed'
    : terminalState === 'aborted'
      ? 'aborted'
      : 'completed';
  return entries.map((entry) => subagentEntryActive(entry) ? { ...entry, status: settledStatus } : entry);
}

function satelliteMessageText(message: Record<string, unknown>): string {
  const nested = asRecord(message.message);
  const blocks = arrayRecords(message.blocks)
    .map((block) => {
      const data = asRecord(block.data);
      return stringValue(data.text) || stringValue(data.markdown) || stringValue(data.summary);
    })
    .filter(Boolean)
    .join('\n');
  return stringValue(message.text) || stringValue(message.content) || stringValue(nested.text) || stringValue(nested.content) || blocks;
}

function subagentEventStatus(eventType: string, payload: Record<string, unknown>, recordStatus = ''): string {
  const explicit = recordStatus || stringValue(payload.status, stringValue(payload.state));
  if (explicit) return explicit;
  const normalizedEventType = eventType.trim().toLowerCase();
  if (normalizedEventType === 'turn_completed') return payload.aborted === true ? 'aborted' : 'completed';
  if (normalizedEventType === 'turn_failed') return 'failed';
  if (normalizedEventType === 'tool_finished') return payload.isError === true ? 'failed' : 'completed';
  if (['failed', 'timed_out', 'tool_failed'].includes(normalizedEventType)) return 'failed';
  if (['aborted', 'cancelled', 'stopped'].includes(normalizedEventType)) return 'aborted';
  if (['completed', 'tool_completed', 'tool_result', 'structured_output'].includes(normalizedEventType)) return 'completed';
  if (['reasoning_summary', 'tool_started', 'tool_progress', 'started'].includes(normalizedEventType)) return 'running';
  return 'unknown';
}

function SatelliteRunState({ eventType, status }: { eventType: string; status: string }) {
  const normalized = status.toLowerCase();
  const active = ['queued', 'running', 'streaming', 'waiting', 'pending', 'claimed'].includes(normalized);
  const failed = ['failed', 'timed_out', 'orphaned'].includes(normalized);
  const stopped = ['aborted', 'cancelled', 'stopped'].includes(normalized);
  const event = eventType.trim().toLowerCase();
  const thinking = event.includes('reasoning') || event.includes('thinking');
  const activeThinking = active && thinking;
  const tool = event === 'tool' || event.startsWith('tool_');
  const terminal = normalized === 'completed' && (
    thinking
    || tool
    || ['completed', 'structured_output', 'tool_completed', 'tool_result', 'turn_completed'].includes(event)
  );
  if (!active && !failed && !stopped && !terminal) return null;
  const Icon = failed
    ? CircleAlert
    : stopped
      ? CircleStop
      : activeThinking
        ? BrainCircuit
        : active
          ? LoaderCircle
          : CheckCircle2;
  const label = failed ? '执行失败' : stopped ? '已停止' : activeThinking ? '正在思考' : active ? '执行中' : '已完成';
  return <i aria-label={label} className="paw-satellite-run-state" data-active={active || undefined} data-kind={activeThinking ? 'thinking' : tool ? 'tool' : 'run'} data-state={normalized || 'completed'} role="img"><Icon aria-hidden="true" className={active && !activeThinking ? 'ui-spin' : undefined} size={12} /></i>;
}

function subagentActivityLabel(eventType: string, payload: Record<string, unknown>): string {
  const tool = stringValue(payload.displayName, stringValue(payload.toolName, stringValue(payload.toolId, '工具')));
  const normalizedEventType = eventType.trim().toLowerCase();
  if (normalizedEventType === 'tool_finished' && payload.isError === true) return `${tool} 调用失败`;
  return ({
    reasoning_summary: '思考摘要已更新',
    tool_started: `开始调用 ${tool}`,
    tool_progress: `${tool} 执行中`,
    tool_completed: `${tool} 已返回`,
    tool_finished: `${tool} 已完成`,
    turn_completed: '运行已完成',
    turn_failed: '运行失败',
    completed: '已完成并返回结果',
    failed: '运行失败',
    aborted: '运行已停止',
    timed_out: '运行超时',
    started: '开始执行',
  } as Record<string, string>)[normalizedEventType] || '运行状态已更新';
}

function useRoomDetail(roomId: string) {
  const transport = useControlTransport();
  return useQuery({
    queryKey: ['rooms', 'detail', roomId],
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.room.get', params: { roomId }, signal }),
    staleTime: 10_000,
  });
}

function roomFromResponse(value: unknown, roomId: string): RoomSummary | undefined {
  const response = asRecord(value);
  const candidate = asRecord(response.room);
  return stringValue(candidate.id) === roomId && typeof candidate.title === 'string' && Array.isArray(candidate.participants)
    ? candidate as unknown as RoomSummary
    : undefined;
}

function SatelliteLoadError({ error, icon, onRetry, title }: { error: unknown; icon: typeof MessageSquare; onRetry: () => void; title: string }) {
  return <EmptyState action={<Button onClick={onRetry}>重试</Button>} description={publicErrorText(error, '暂时无法读取这项内容。')} icon={icon} title={title} />;
}

function SatelliteMissing({ actionLabel = '打开主 App', copy, icon, route, title }: { actionLabel?: string; copy: string; icon: typeof MessageSquare; route: 'agent' | 'rooms'; title: string }) {
  const desktop = usePawOsDesktop();
  return <EmptyState action={<Button onClick={() => openPawOsRoute(desktop, routePath(route))}>{actionLabel}</Button>} description={copy} icon={icon} title={title} />;
}

function ProjectSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'project' }> }) {
  return (
    <section className="paw-os-satellite paw-os-satellite--project">
      <div className="paw-os-satellite__project-body">
        <nav aria-label={`${target.title} 项目工具`} className="paw-os-satellite__project-links">
          <ProjectLink description="查看项目运行面与当前能力" label="项目概览" route="overview" />
          <ProjectLink description="安排、编辑并验收 WorkItem" label="任务" route="planning" />
          <ProjectLink description="查看与当前工作绑定的文档" label="工作文档" route="work-documents" />
        </nav>
      </div>
    </section>
  );
}

function ProjectLink({
  description,
  label,
  route,
}: {
  description: string;
  label: string;
  route: 'overview' | 'planning' | 'work-documents';
}) {
  const desktop = usePawOsDesktop();
  return (
    <button onClick={() => openPawOsRoute(desktop, routePath(route))} type="button">
      <span><strong>{label}</strong><small>{description}</small></span>
      <ExternalLink aria-hidden="true" size={15} />
    </button>
  );
}

function PlanningTaskSatellite({ target }: { target: Extract<PawOsWindowTarget, { kind: 'task' }> }) {
  const desktop = usePawOsDesktop();
  const { dashboard } = usePlanningDashboard(target.date, target.project);
  const payload = asRecord(dashboard.data);
  const task = arrayRecords(payload.tasks).find((candidate) => stringValue(candidate.id) === target.id);
  const status = stringValue(task?.status, 'unknown');
  return (
    <section className="paw-os-satellite paw-os-satellite--task">
      {dashboard.isPending ? <div className="paw-os-satellite__loading" role="status"><Skeleton /><Skeleton /><Skeleton /></div> : null}
      {dashboard.error ? (
        <EmptyState
          action={<Button onClick={() => void dashboard.refetch()}>重试</Button>}
          description={publicErrorText(dashboard.error, '暂时无法读取这项任务。')}
          icon={ListChecks}
          title="WorkItem 没有打开"
        />
      ) : null}
      {!dashboard.isPending && !dashboard.error && !task ? (
        <EmptyState
          action={<Button onClick={() => openPawOsRoute(desktop, routePath('planning'))}>打开任务 App</Button>}
          description="这项任务已不在当前日期的权威任务清单中，可能已移动、归档或删除。"
          icon={ListChecks}
          title="找不到这项 WorkItem"
        />
      ) : null}
      {task ? (
        <div className="paw-os-satellite__task-body">
          <section>
            <span>任务说明</span>
            <p>{stringValue(task.detail, '没有补充说明。')}</p>
          </section>
          <dl>
            <Fact label="状态" value={taskStatusLabel(status)} />
            <Fact label="来源" value={stringValue(task.source, '未知')} />
            <Fact label="日期" value={target.date} />
            <Fact label="项目" value={target.project || '默认项目'} />
            {stringValue(task.dueAt) ? <Fact label="截止时间" value={stringValue(task.dueAt)} wide /> : null}
          </dl>
          <Button
            leadingIcon={<CalendarDays size={15} />}
            onClick={() => openPawOsRoute(desktop, routePath('planning'))}
          >
            在任务 App 中编辑
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function WorkDocumentSatellite({ documentId }: { documentId: string }) {
  const desktop = usePawOsDesktop();
  const transport = useControlTransport();
  const detail = useQuery({
    queryKey: ['work-documents', 'detail', documentId],
    queryFn: ({ signal }) => transport.request<WorkDocumentDetailV1>({
      pathId: 'workDocuments.get',
      params: { documentId },
      signal,
    }),
  });
  const document = detail.data?.document;
  return (
    <section className="paw-os-satellite paw-os-satellite--document">
      {detail.isPending ? <div className="paw-os-satellite__loading" role="status"><Skeleton /><Skeleton /><Skeleton /></div> : null}
      {detail.error ? (
        <EmptyState
          action={<Button onClick={() => void detail.refetch()}>重试</Button>}
          description={publicErrorText(detail.error, '暂时无法读取这份文档。')}
          icon={FileText}
          title="工作文档没有打开"
        />
      ) : null}
      {document ? (
        <div className="paw-os-satellite__document-body">
          <section><span>当前状态</span><strong>{documentStateLabel(document.state)}</strong><p>{document.error || '没有报告错误'}</p></section>
          <dl>
            <Fact label="最近更新" value={formatTime(document.updatedAtMs)} />
            <Fact label="来源" value={document.authorityKind} />
            <Fact label="文档修订" value={String(document.documentRevision)} />
            <Fact label="来源修订" value={String(document.authorityRevision)} />
            <Fact label="当前路径" value={document.path || '暂无'} wide />
            <Fact label="工作区" value={document.workspaceRoot || '暂无'} wide />
          </dl>
          <Button
            leadingIcon={<ExternalLink size={15} />}
            onClick={() => openPawOsRoute(desktop, `${routePath('work-documents')}?document=${encodeURIComponent(documentId)}`)}
          >
            在项目工作台中继续
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function Fact({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return <div data-wide={wide || undefined}><dt>{label}</dt><dd>{value}</dd></div>;
}

function targetLabel(kind: PawOsWindowTarget['kind']): string {
  return ({ project: 'PROJECT', task: 'WORK ITEM', session: 'SESSION', room: 'ROOM', participant: 'ROOM PARTNER', subagent: 'SUBAGENT', package: 'PI PACKAGE APP', result: 'RESULT', 'work-document': 'WORK DOCUMENT', 'process-terminal': 'PROCESS', 'browser-target': 'BROWSER' } as const)[kind];
}

function documentStateLabel(state: string): string {
  return ({ active: '活跃', archiving: '归档中', archived: '已归档', archive_failed: '归档失败', reopen_failed: '恢复失败', erase_failed: '清除失败' } as Record<string, string>)[state] ?? state;
}

function documentStateTone(state: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (state === 'active') return 'info';
  if (state === 'archived') return 'success';
  if (state.includes('failed')) return 'danger';
  if (state === 'archiving') return 'warning';
  return 'neutral';
}

function taskStatusLabel(status: string): string {
  return ({ todo: '待开始', in_progress: '进行中', done: '已完成', cancelled: '已取消', blocked: '已阻塞', unknown: '未知' } as Record<string, string>)[status] ?? status;
}

function taskStatusTone(status: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (status === 'done') return 'success';
  if (status === 'in_progress') return 'info';
  if (status === 'blocked') return 'danger';
  if (status === 'cancelled') return 'warning';
  return 'neutral';
}
