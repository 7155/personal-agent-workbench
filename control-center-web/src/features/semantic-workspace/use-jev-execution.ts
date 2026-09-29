import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { commandJevAssignment, pendingJevAssignment, readJevAssignment, type JevTaskControls } from './jev-task-assignment';
import { commandJevRevision, pendingJevRevision, readJevRevision } from './jev-task-revision';
import type { ControlTransport } from '@/platform/transport';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { acknowledgeJevAdmission, acknowledgeJevPlan, commandJevPlan, createJevWork, jevAbstention, jevAwaitingPlan, jevIsBusy, jevRecord, pendingJevInput, uncertainJevInput, uncertainJevPlan, parseJevList, parseJevSnapshot, type JevGraphItem, type JevSnapshot, type JevStrategy, type JevModelRouting, type JevToolApproval, type JevVerificationMode, type JevPlanAction, type JevPlanCommand } from './jev-execution';

export function useJevExecution({ roomId, enabled, active, transport }: {
  roomId: string; enabled: boolean; active: boolean; transport: ControlTransport;
}) {
  const [items, setItems] = useState<JevGraphItem[]>([]);
  const [snapshot, setSnapshot] = useState<JevSnapshot | null>(null);
  const [liveSnapshot, setLiveSnapshot] = useState<JevSnapshot | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [stopping, setStopping] = useState(false);
  const [routeRetrying, setRouteRetrying] = useState(false);
  const [creating, setCreating] = useState(false);
  const [planSending, setPlanSending] = useState<JevPlanAction | ''>('');
  const [, projectCommandReceipt] = useState(0);
  const [recoveredAdmission, setRecoveredAdmission] = useState<ReturnType<typeof pendingJevInput>>(undefined);
  const [strategy, setStrategy] = useState<JevStrategy>(() => pendingJevInput(transport, roomId)?.strategy ?? 'auto');
  const [modelRouting, setModelRouting] = useState<JevModelRouting>(() => pendingJevInput(transport, roomId)?.modelRouting ?? 'balanced');
  const [toolApprovalMode, setToolApprovalMode] = useState<JevToolApproval>(() => pendingJevInput(transport, roomId)?.toolApprovalMode ?? 'dispatch');
  const [verificationMode, setVerificationMode] = useState<JevVerificationMode>(() => pendingJevInput(transport, roomId)?.verificationMode ?? 'auto');
  const [scope, setScope] = useState({ roomId, transport });
  const context = useRef({ roomId, enabled, active, transport });
  context.current = { roomId, enabled, active, transport };
  const selected = useRef('');
  const historySelected = useRef(false);
  const requestId = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const commandPending = useRef<object | null>(null);
  const inFlight = useRef<{ controller: AbortController; dirty: boolean; promise: Promise<void>; successor?: Promise<void> } | null>(null);
  const matchesScope = useCallback(() => context.current.roomId === roomId && context.current.transport === transport, [roomId, transport]);
  const matches = useCallback(() => matchesScope() && context.current.enabled && context.current.active, [matchesScope]);

  const refresh = useCallback(async (graphId?: string, requireFresh = false): Promise<void> => {
    if (!matches()) return;
    const existing = inFlight.current;
    if (graphId === undefined && existing && !existing.controller.signal.aborted) {
      // Reconnect shares the first usable read. Events/command receipts need
      // one subsequent read, without starving that first result by aborting it.
      existing.dirty ||= requireFresh;
      // Commands wait for at most one successor, never for the entire stream.
      return requireFresh ? existing.promise.then(() => existing.successor) : existing.promise;
    }
    const request = ++requestId.current;
    existing?.controller.abort();
    const controller = new AbortController(); abort.current = controller;
    const current = () => requestId.current === request && !controller.signal.aborted && matches();
    const flight: NonNullable<typeof inFlight.current> = { controller, dirty: false, promise: Promise.resolve() };
    inFlight.current = flight;
    setLoading(true);
    flight.promise = (async () => {
      try {
        const nextItems = parseJevList(await transport.request({ pathId: 'agent.jev.get', params: { roomId }, signal: controller.signal }), roomId);
        if (!current()) return;
        const pendingInput = pendingJevInput(transport, roomId);
        if (acknowledgeJevAdmission(transport, roomId, nextItems)) setRecoveredAdmission(pendingInput);
        const liveId = nextItems.find(item => !item.stopped && item.phase !== 'final')?.id ?? nextItems[0]?.id ?? '';
        // Automatic selection follows the current Root across windows. Only
        // an explicit history choice keeps the progress panels on an old run.
        const wanted = graphId ?? (historySelected.current ? selected.current : liveId);
        const id = nextItems.some(item => item.id === wanted) ? wanted : liveId;
        const read = async (target: string) => target ? parseJevSnapshot(await transport.request({
          pathId: 'agent.jev.get', params: { roomId }, query: { graphId: target }, signal: controller.signal,
        }), target) : null;
        const [next, live] = await Promise.all([read(id), liveId === id ? Promise.resolve(undefined) : read(liveId)]);
        if (!current()) return;
        const planReceipt = acknowledgeJevPlan(transport, roomId, live === undefined ? next : live);
        if (planReceipt?.action === 'adjust_plan' && planReceipt.message) setRecoveredAdmission({ message: planReceipt.message, attachmentIds: planReceipt.attachmentIds });
        selected.current = id; setSelectedId(id); setItems(nextItems); setSnapshot(next); setLiveSnapshot(live === undefined ? next : live); setError('');
      } catch (reason) {
        if (current()) setError(publicAgentErrorText(reason, '任务状态暂时无法读取，重新同步即可继续查看。'));
      } finally {
        if (inFlight.current === flight) inFlight.current = null;
        if (current()) {
          setLoading(false);
          // Publish this read before an independent successor. Sustained SSE
          // must not extend a command's promise and keep its button locked.
          if (flight.dirty) flight.successor = refresh();
        }
      }
    })();
    return flight.promise;
  }, [roomId, transport, matches]);

  useEffect(() => {
    selected.current = ''; historySelected.current = false; setSelectedId(''); setItems([]); setSnapshot(null); setLiveSnapshot(null); setError('');
    commandPending.current = null; setStopping(false); setRouteRetrying(false); setCreating(false); setPlanSending(''); setRecoveredAdmission(undefined); setVerificationMode(pendingJevInput(transport, roomId)?.verificationMode ?? 'auto'); setScope({ roomId, transport });
    return () => { requestId.current++; abort.current?.abort(); };
  }, [roomId, transport]);
  useEffect(() => {
    if (enabled && active) void refresh();
    else { requestId.current++; abort.current?.abort(); setLoading(false); }
    return () => { requestId.current++; abort.current?.abort(); };
  }, [enabled, active, refresh]);
  // SSE owns prompt refresh; a bounded read-only poll covers an interrupted
  // stream or a missed terminal event. Hidden documents never poll.
  useEffect(() => {
    if (!enabled || !active || loading || (!jevIsBusy(liveSnapshot) && !error)) return;
    const timer = window.setTimeout(() => { void refresh(); }, 6000);
    return () => window.clearTimeout(timer);
  }, [enabled, active, liveSnapshot, error, loading, refresh]);

  const onEvents = useCallback((events: readonly unknown[]) => {
    if (events.some(value => { const event = jevRecord(value); const payload = jevRecord(event.payload);
      return payload.status === 'jev_updated' || payload.mode === 'jev' || Boolean(payload.finalizationId);
    })) void refresh(undefined, true);
  }, [refresh]);
  const selectGraph = (id: string) => { historySelected.current = true; selected.current = id; setSelectedId(id); setSnapshot(null); void refresh(id); };
  const submit = async (input: NonNullable<ReturnType<typeof pendingJevInput>>) => {
    if (commandPending.current) return false;
    const command = {}; commandPending.current = command; setCreating(true); setRecoveredAdmission(undefined);
    try {
      const result = await createJevWork(transport, roomId, input);
      if (matchesScope()) { historySelected.current = false; selected.current = result.graphId; setSelectedId(result.graphId); if (matches()) await refresh(result.graphId); }
      return true;
    } finally { if (commandPending.current === command) commandPending.current = null; if (matchesScope()) { setCreating(false); projectCommandReceipt(version => version + 1); } }
  };
  const send = (message: string, attachmentIds: string[] = []) => jevAwaitingPlan(liveSnapshot)
    ? decidePlan('adjust_plan', message, attachmentIds)
    : submit({ message, strategy, attachmentIds, modelRouting, toolApprovalMode, verificationMode, executionApproval: true,
      ...(liveSnapshot?.rootId ? { previousRootId: liveSnapshot.rootId } : {}),
    });
  const retryPending = async () => {
    const input = uncertainJevInput(transport, roomId);
    return input ? submit(input) : false;
  };
  const submitPlan = async (input: JevPlanCommand) => {
    if (commandPending.current) return false;
    const command = {}; commandPending.current = command; setPlanSending(input.action); setRecoveredAdmission(undefined);
    try {
      await commandJevPlan(transport, roomId, input);
      if (matchesScope()) { historySelected.current = false; selected.current = input.graphId; setSelectedId(input.graphId); if (matches()) await refresh(input.graphId); }
      return true;
    } finally { if (commandPending.current === command) commandPending.current = null; if (matchesScope()) { setPlanSending(''); projectCommandReceipt(version => version + 1); } }
  };
  const decidePlan = (action: JevPlanAction, message?: string, attachmentIds?: string[]) => {
    if (!liveSnapshot?.planApproval || !jevAwaitingPlan(liveSnapshot)) return Promise.resolve(false);
    return submitPlan({ action, graphId: liveSnapshot.graphId, rootId: liveSnapshot.rootId, planHash: liveSnapshot.planApproval.planHash,
      ...(message ? { message } : {}), ...(attachmentIds?.length ? { attachmentIds } : {}),
    });
  };
  const retryPendingPlan = async () => {
    const input = uncertainJevPlan(transport, roomId); return input ? submitPlan(input) : false;
  };
  const stop = async () => {
    if (!liveSnapshot || commandPending.current || !jevIsBusy(liveSnapshot)) return;
    const command = {}; commandPending.current = command; setStopping(true); setError('');
    try {
      const result = jevRecord(await transport.request({ pathId: 'agent.jev.command', params: { roomId }, body: {
        action: 'stop', graphId: liveSnapshot.graphId, clientMessageId: `paw-jev-stop-${crypto.randomUUID()}`,
      } }));
      if (result.ok === false) throw new Error('停止尚未确认，请重新同步。');
      if (matches()) await refresh(undefined, true);
    } catch (reason) { if (matches()) setError(publicAgentErrorText(reason, '停止请求尚未确认，请重新同步后核实。')); }
    finally { if (commandPending.current === command) commandPending.current = null; if (matchesScope()) setStopping(false); }
  };
  const retryRoute = async () => {
    if (!liveSnapshot || liveSnapshot.phase !== 'route' || !jevAbstention(liveSnapshot) || commandPending.current) return;
    const command = {}; commandPending.current = command; setRouteRetrying(true); setError('');
    try {
      const result = jevRecord(await transport.request({ pathId: 'agent.jev.command', params: { roomId }, body: {
        action: 'retry_route', graphId: liveSnapshot.graphId,
        clientMessageId: `paw-jev-route-retry:${liveSnapshot.graphId}`,
      } }));
      if (result.ok !== true || result.accepted !== true) throw new Error('原任务尚未重新进入路径判断。');
      if (matches()) await refresh(undefined, true);
    } catch (reason) { if (matches()) setError(publicAgentErrorText(reason, '继续请求尚未确认，请同步状态后核实。')); }
    finally { if (commandPending.current === command) commandPending.current = null; if (matchesScope()) setRouteRetrying(false); }
  };
  const taskControls = useMemo<JevTaskControls>(() => ({
    revision: {
      load: (graphId, taskId, signal) => readJevRevision(transport, roomId, graphId, taskId, signal),
      pending: (graphId, taskId) => pendingJevRevision(transport, roomId, graphId, taskId),
      submit: async input => {
        if (!matchesScope() || commandPending.current) throw new Error('当前操作尚在提交，请稍后再试。');
        const command = {}; commandPending.current = command;
        try {
          const receipt = await commandJevRevision(transport, roomId, input);
          if (matches()) await refresh(undefined, true);
          return receipt;
        } finally {
          if (commandPending.current === command) commandPending.current = null;
          if (matchesScope()) projectCommandReceipt(version => version + 1);
        }
      },
    },
    load: (graphId, taskId, signal) => readJevAssignment(transport, roomId, graphId, taskId, signal),
    pending: (graphId, taskId) => pendingJevAssignment(transport, roomId, graphId, taskId),
    submit: async input => {
      if (!matchesScope() || commandPending.current) throw new Error('当前操作尚在提交，请稍后再试。');
      const command = {}; commandPending.current = command;
      try {
        const receipt = await commandJevAssignment(transport, roomId, input);
        if (matches()) await refresh(undefined, true);
        return receipt;
      } finally {
        if (commandPending.current === command) commandPending.current = null;
        if (matchesScope()) projectCommandReceipt(version => version + 1);
      }
    },
  }), [transport, roomId, matchesScope, matches, refresh]);
  const bound = scope.roomId === roomId && scope.transport === transport;
  return { items: bound ? items : [], snapshot: bound ? snapshot : null, liveSnapshot: bound ? liveSnapshot : null,
    selectedId: bound ? selectedId : '', selectGraph, loading, error: bound ? error : '', stopping, creating, recoveredAdmission: bound ? recoveredAdmission : undefined, strategy, setStrategy, modelRouting, setModelRouting, toolApprovalMode, setToolApprovalMode, verificationMode, setVerificationMode,
    pendingInput: bound ? uncertainJevInput(transport, roomId) : undefined, retryPending,
    awaitingPlan: bound && jevAwaitingPlan(liveSnapshot), planSending, decidePlan, retryPendingPlan, pendingPlan: bound ? uncertainJevPlan(transport, roomId) : undefined,
    busy: bound && jevIsBusy(liveSnapshot), routeRetrying, retryRoute, refresh: () => { void refresh(); }, onEvents, send, stop, taskControls };
}

export type JevExecution = ReturnType<typeof useJevExecution>;
