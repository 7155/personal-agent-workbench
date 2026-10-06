import { StrictMode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest } from '@/platform/transport';
import type { RoomSummary } from '@/features/rooms/room-types';
import { roomSendJournal } from '@/features/rooms/runtime/room-send-journal';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { recoveryScope } from '@/features/semantic-workspace/workspace-recovery';
import { PawRoomWorkspace } from './PawRoomWorkspace';

const connectionPrefix = 'room-held-input-test:';

afterEach(() => {
  cleanup();
  useRoomLiveStore.getState().reset();
  for (const storage of [localStorage, sessionStorage]) {
    for (const key of Object.keys(storage)) if (key.includes(encodeURIComponent(connectionPrefix))) storage.removeItem(key);
  }
});

describe('Room held input recovery', () => {
  it.each(['accepted', 'uncertain'] as const)('keeps a second held input while the first send awaits its $outcome receipt', async (outcome) => {
    const roomId = `room-held-admission-${outcome}`;
    let releaseAdmission!: () => void;
    const admission = new Promise<void>(resolve => { releaseAdmission = resolve; });
    const original = roomRecoveryFixture(roomId, connectionPrefix + roomId, outcome, admission);
    render(original.tree());
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await screen.findByRole('button', { name: '立即干预当前回合' });
    fireEvent.change(composer, { target: { value: '先核对当前任务' } });
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后' }));
    fireEvent.change(composer, { target: { value: '仍未提交的后续输入' } });
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后（已排 1 条）' }));
    fireEvent.change(composer, { target: { value: '后来补充的可编辑草稿' } });
    fireEvent.click(screen.getByRole('button', { name: /2 条排队中/ }));
    const tray = screen.getByRole('region', { name: '排队中的消息' });
    fireEvent.click(within(within(tray).getAllByRole('listitem')[0]!).getByRole('button', { name: '立即发送' }));
    await waitFor(() => expect(original.commands()).toHaveLength(1));
    const submitted = structuredClone(original.commands()[0]!);
    expect(submitted).toMatchObject({ pathId: 'agent.room.participant.steer', body: {
      rootId: `${roomId}:turn-1`, participantId: 'participant-present', message: '先核对当前任务', clientActionId: expect.any(String),
    } });
    expect(roomSendJournal(original.transport, roomId).getSnapshot()).toMatchObject({ request: submitted, status: 'sending' });

    // The remaining row is a real, enabled UI action during transport admission.
    fireEvent.click(within(tray).getByRole('button', { name: '改为干预' }));
    expect(screen.queryByRole('region', { name: '排队中的消息' })).toBeInTheDocument();
    expect(within(tray).getAllByRole('listitem')).toHaveLength(1);
    expect(tray).toHaveTextContent('仍未提交的后续输入');
    expect(tray).not.toHaveTextContent('先核对当前任务');
    expect(composer).toHaveValue('后来补充的可编辑草稿');
    expect(original.commands()).toEqual([submitted]);

    await act(async () => { releaseAdmission(); await admission; });
    await waitFor(() => expect(roomSendJournal(original.transport, roomId).getSnapshot()?.status).not.toBe('sending'));
    if (outcome === 'uncertain') {
      // Finishing the POST does not admit a new message while its ACK is unknown.
      fireEvent.click(within(tray).getByRole('button', { name: '立即发送' }));
      expect(tray).toHaveTextContent('仍未提交的后续输入');
      expect(original.commands()).toEqual([submitted]);
      expect(roomSendJournal(original.transport, roomId).getSnapshot()).toMatchObject({ request: submitted, status: 'uncertain' });
      fireEvent.click(screen.getByRole('button', { name: '核实上次发送' }));
      await waitFor(() => expect(original.commands()).toHaveLength(2));
      expect(original.commands()[1]).toEqual(submitted);
      await waitFor(() => expect(roomSendJournal(original.transport, roomId).getSnapshot()?.status).toBe('uncertain'));
    } else {
      expect(roomSendJournal(original.transport, roomId).getSnapshot()).toBeUndefined();
      expect(original.commands()).toEqual([submitted]);
    }
    act(() => { window.dispatchEvent(new PageTransitionEvent('pagehide')); });
    expect(composer).toHaveValue('后来补充的可编辑草稿\n\n仍未提交的后续输入');
    expect(screen.queryByRole('region', { name: '排队中的消息' })).not.toBeInTheDocument();
  });

  it('returns held input to its original connection when a mounted Room changes backends', async () => {
    const roomId = 'room-held-connection-switch';
    const original = roomRecoveryFixture(roomId, connectionPrefix + 'original');
    const other = roomRecoveryFixture(roomId, connectionPrefix + 'other');
    const view = render(original.tree());
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await screen.findByRole('button', { name: '立即干预当前回合' });
    fireEvent.change(composer, { target: { value: '只属于原连接的后续输入' } });
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后' }));
    expect(screen.getByRole('status', { name: '等待当前执行完成后发送的消息' })).toHaveTextContent('只属于原连接的后续输入');
    fireEvent.change(composer, { target: { value: '原连接的新草稿' } });

    view.rerender(other.tree());
    expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveValue('');
    expect(screen.queryByRole('status', { name: '等待当前执行完成后发送的消息' })).not.toBeInTheDocument();
    const recovered = '原连接的新草稿\n\n只属于原连接的后续输入';
    expect(JSON.parse(localStorage.getItem(recoveryScope(original.transport, `room:${roomId}`))!)).toMatchObject({ draft: recovered });
    view.rerender(original.tree());
    expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveValue(recovered);
    expect([...original.commands(), ...other.commands()]).toEqual([]);
  });

  it.each([
    { lifecycle: 'pagehide', persisted: false, outcome: 'accepted' },
    { lifecycle: 'pagehide', persisted: true, outcome: 'uncertain' },
    { lifecycle: 'unmount', persisted: false, outcome: 'uncertain' },
  ] as const)('recovers only unsent follow-ups on $lifecycle (bfcache: $persisted, send: $outcome)', async ({ lifecycle, persisted, outcome }) => {
    const roomId = `room-held-${lifecycle}-${persisted}`;
    const connectionIdentity = connectionPrefix + roomId;
    const original = roomRecoveryFixture(roomId, connectionIdentity, outcome);
    const view = render(original.tree());
    const composer = await screen.findByRole('textbox', { name: '协作消息' });
    await waitFor(() => expect(screen.getByRole('button', { name: '立即干预当前回合' })).toBeInTheDocument());
    expect(composer).toHaveValue('');

    // This input has crossed the admission boundary and must never be copied
    // into the held queue, including when the ACK is uncertain.
    fireEvent.change(composer, { target: { value: '已经提交的当前回合补充' } });
    fireEvent.click(screen.getByRole('button', { name: '立即干预当前回合' }));
    await waitFor(() => expect(composer).toHaveValue(outcome === 'accepted' ? '' : '已经提交的当前回合补充'));
    await waitFor(() => expect(roomSendJournal(original.transport, roomId).getSnapshot()?.status).not.toBe('sending'));
    expect(original.commands()).toHaveLength(1);
    const submitted = structuredClone(original.commands()[0]!);
    const rootId = `${roomId}:turn-1`;
    expect(submitted).toMatchObject({ pathId: 'agent.room.participant.steer', body: {
      rootId, participantId: 'participant-present', message: '已经提交的当前回合补充', clientActionId: expect.any(String),
    } });
    const journalKey = `paw.room.send.v1:${encodeURIComponent(connectionIdentity)}:${encodeURIComponent(roomId)}`;
    const savedJournal = sessionStorage.getItem(journalKey);
    if (outcome === 'uncertain') expect(JSON.parse(savedJournal!)).toMatchObject({ request: submitted, status: 'uncertain' });
    else expect(savedJournal).toBeNull();

    fireEvent.change(composer, { target: { value: '当前回合结束后检查依赖图' } });
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后' }));
    expect(screen.getByRole('status', { name: '等待当前执行完成后发送的消息' })).toHaveTextContent('当前回合结束后检查依赖图');
    expect(composer).toHaveValue('');
    fireEvent.change(composer, { target: { value: '再核对测试边界' } });
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后（已排 1 条）' }));
    expect(screen.getByRole('button', { name: /2 条排队中/ })).toBeInTheDocument();
    expect(composer).toHaveValue('');
    fireEvent.change(composer, { target: { value: '后来补充的可编辑草稿' } });
    const recovered = '后来补充的可编辑草稿\n\n当前回合结束后检查依赖图\n\n再核对测试边界';
    const storageKey = recoveryScope(original.transport, `room:${roomId}`);

    if (lifecycle === 'pagehide') {
      // Synthetic renderer navigation: pagehide runs before React cleanup.
      // This does not claim a real Electron reload or abrupt-crash recovery.
      act(() => { window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted })); });
      expect(JSON.parse(localStorage.getItem(storageKey)!)).toMatchObject({ draft: recovered, attachments: [] });
      expect(composer).toHaveValue(recovered);
      expect(screen.queryByRole('button', { name: /条排队中/ })).not.toBeInTheDocument();
      act(() => {
        window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted }));
        window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted }));
      });
      expect(composer).toHaveValue(recovered);
    }
    view.unmount();
    expect(JSON.parse(localStorage.getItem(storageKey)!)).toMatchObject({ draft: recovered, attachments: [] });
    expect(sessionStorage.getItem(journalKey)).toBe(savedJournal);
    expect(original.commands()).toEqual([submitted]);

    // Recreate the transport to exercise durable ownership, without an
    // initialDraft prop that would intentionally replace the saved input.
    useRoomLiveStore.getState().reset();
    const otherRoom = roomRecoveryFixture(`${roomId}-other`, connectionIdentity);
    const next = render(otherRoom.tree());
    expect(await screen.findByRole('textbox', { name: '协作消息' })).toHaveValue('');
    expect(roomSendJournal(otherRoom.transport, otherRoom.room.id).getSnapshot()).toBeUndefined();
    const otherConnection = roomRecoveryFixture(roomId, `${connectionIdentity}-other`);
    next.rerender(otherConnection.tree());
    expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveValue('');
    expect(roomSendJournal(otherConnection.transport, roomId).getSnapshot()).toBeUndefined();
    const reopened = roomRecoveryFixture(roomId, connectionIdentity);
    next.rerender(reopened.tree());
    const editable = screen.getByRole('textbox', { name: '协作消息' });
    expect(editable).toHaveValue(recovered);
    expect(screen.queryByRole('status', { name: '等待当前执行完成后发送的消息' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /条排队中/ })).not.toBeInTheDocument();
    await waitFor(() => expect(useRoomLiveStore.getState().projections[roomId]?.turnOrder).toEqual([rootId]));
    expect(sessionStorage.getItem(journalKey)).toBe(savedJournal);
    const restoredAttempt = roomSendJournal(reopened.transport, roomId).getSnapshot();
    if (outcome === 'uncertain') expect(restoredAttempt).toMatchObject({ request: submitted, status: 'uncertain', clientMessageId: (submitted.body as { clientActionId: string }).clientActionId });
    else expect(restoredAttempt).toBeUndefined();
    expect([...otherRoom.commands(), ...otherConnection.commands(), ...reopened.commands()]).toEqual([]);
    fireEvent.change(editable, { target: { value: `${recovered}\n继续编辑` } });
    expect(editable).toHaveValue(`${recovered}\n继续编辑`);
  });
});

function roomRecoveryFixture(roomId: string, connectionIdentity: string, outcome: 'accepted' | 'uncertain' = 'accepted', admission?: Promise<void>) {
  const source = previewRoomSnapshot(roomId);
  const snapshot = { ...source, room: { ...source.room, workItems: [], lastEventSequence: 4 },
    events: source.events.slice(0, 4), lastSequence: 4, resumeToken: `${roomId}:4` };
  const room = snapshot.room as unknown as RoomSummary;
  const transport = createPreviewTransport();
  Object.defineProperty(transport, 'connectionIdentity', { value: connectionIdentity });
  const requests: ControlRequest[] = [];
  const requestPreview = transport.request.bind(transport);
  transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
    requests.push(request);
    if (request.pathId === 'agent.room.snapshot') return snapshot as Response;
    if (request.pathId === 'agent.room.get') return { ok: true, room } as Response;
    if (request.pathId === 'agent.room.participant.steer') {
      if (admission) await admission;
      if (outcome === 'uncertain') throw new TypeError('synthetic lost ACK after admission');
      return { ok: true, accepted: true } as Response;
    }
    return requestPreview<Response>(request);
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    room, transport,
    commands: () => requests.filter(request => ['agent.room.message', 'agent.room.participant.steer', 'agent.jev.command'].includes(request.pathId)),
    tree: () => <StrictMode><QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider>
      <PawRoomWorkspace personas={[]} record={room} recordId={roomId} onRoomUpdated={vi.fn()} />
    </TooltipProvider></ControlTransportProvider></QueryClientProvider></StrictMode>,
  };
}
