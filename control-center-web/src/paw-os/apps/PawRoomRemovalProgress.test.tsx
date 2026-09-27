import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import type { RoomSummary } from '@/features/rooms/room-types';
import { PawRoomRemovalProgress, useRoomRemovals, type ParticipantRemoval } from './PawRoomRemovalProgress';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
const transport = { request };
vi.mock('@/app/control-transport', () => ({ useControlTransport: () => transport }));
const room = previewRoomSnapshot('removal-ui').room as unknown as RoomSummary;
const removal: ParticipantRemoval = { participantId: room.participants[0].id, status: 'pending', stage: 'awaiting_stop', targetParticipantId: '' };
afterEach(() => { cleanup(); vi.useRealTimers(); request.mockReset(); });
function Observer({ refresh }: { refresh: () => Promise<void> }) {
  const state = useRoomRemovals(room, refresh);
  return <PawRoomRemovalProgress room={room} {...state} busy={false} onRemove={vi.fn()} />;
}
describe('managed participant removal UI', () => {
  it('keeps pending work visible until the durable projection clears, then refreshes membership', async () => {
    vi.useFakeTimers();
    request.mockResolvedValueOnce({ ok: true, participantRemovals: [removal] }).mockResolvedValue({ ok: true, participantRemovals: [] });
    const refresh = vi.fn().mockResolvedValue(undefined);
    await act(async () => { render(<Observer refresh={refresh} />); });
    expect(screen.getByText('等待执行真正停止')).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('等待执行真正停止')).not.toBeInTheDocument();
  });
  it('preserves pending state on a missing projection instead of treating it as completed', async () => {
    vi.useFakeTimers();
    request.mockResolvedValueOnce({ ok: true, participantRemovals: [removal] }).mockResolvedValue({ ok: true });
    const refresh = vi.fn().mockResolvedValue(undefined);
    await act(async () => { render(<Observer refresh={refresh} />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText('等待执行真正停止')).toBeInTheDocument();
    expect(screen.getByText('移交进度暂时未同步，正在重连。')).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });
  it('requires a distinct explicit stop action for an active controller', async () => {
    const remove = vi.fn();
    render(<PawRoomRemovalProgress room={room} items={[{ ...removal, status: 'blocked', stage: 'controller_requires_stop' }]} error="" busy={false} onRemove={remove} />);
    expect(remove).not.toHaveBeenCalled();
    await userEvent.setup().click(screen.getByRole('button', { name: '停止本轮并移出协调伙伴' }));
    expect(remove).toHaveBeenCalledWith(removal.participantId, { stopRoot: true });
  });
  it('offers automatic reassignment and excludes other departing partners', async () => {
    render(<PawRoomRemovalProgress room={room} items={[removal, { ...removal, participantId: room.participants[1].id }]} error="" busy={false} onRemove={vi.fn()} />);
    await userEvent.setup().click(screen.getByLabelText('Earth 的接手伙伴'));
    expect(screen.getByRole('option', { name: 'Jev 自动选择' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Mars' })).not.toBeInTheDocument();
  });
});
