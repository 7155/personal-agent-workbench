import { useEffect, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Select } from '@/components/primitives';
import { ToolStatusMark } from '@/features/conversation-ui/components/ToolStatusMark';
import { roomPlanetName } from '@/features/rooms/room-copy';
import { jevRecord } from '@/features/semantic-workspace/jev-execution';
import type { RoomSummary } from '@/features/rooms/room-types';

export interface ParticipantRemoval {
  participantId: string; status: string; stage: string; targetParticipantId: string; stopRoot?: boolean;
}
export function removalStatusLabel(stage: string): string {
  return ({ requested: '正在准备任务移交', transferring: '正在移交任务', awaiting_stop: '等待执行真正停止',
    awaiting_receipt: '正在核实执行回执', awaiting_review: '等待当前成果复核', requires_partner: '需要可接手的伙伴',
    revise_owner_lock: '任务指定了负责人，请先调整执行方案', change_moderator: '请先更换 Room 主持人',
    minimum_participants: '需要保留至少两位伙伴', controller_requires_stop: '协调伙伴仍负责本轮任务',
    retrying: '正在恢复移交，请稍候', removed: '已移出', invalid_stop_mode: '当前伙伴不负责协调本轮任务',
  } as Record<string, string>)[stage] || '正在核实移出状态';
}
const parseRemoval = (value: unknown): ParticipantRemoval | null => {
  const row = jevRecord(value);
  return typeof row.participantId === 'string' && typeof row.status === 'string' && typeof row.stage === 'string'
    ? { participantId: row.participantId, status: row.status, stage: row.stage, targetParticipantId: typeof row.targetParticipantId === 'string' ? row.targetParticipantId : '', stopRoot: row.stopRoot === true } : null;
};

/** Observe durable removal separately from the HTTP request; an accepted cancel
 * never removes a member optimistically. This observer lives only in settings. */
export function useRoomRemovals(room: RoomSummary, onRefresh: () => Promise<void>) {
  const transport = useControlTransport();
  const [items, setItems] = useState<ParticipantRemoval[]>([]);
  const [error, setError] = useState('');
  const current = useRef(items); current.current = items;
  const refresh = useRef(onRefresh); refresh.current = onRefresh;
  const generation = useRef(0);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const epoch = generation.current;
      try {
        const response = await transport.request<Record<string, unknown>>({ pathId: 'agent.jev.get', params: { roomId: room.id } });
        if (cancelled || epoch !== generation.current) return;
        if (response.ok === false || (!Array.isArray(response.participantRemovals) && current.current.some(row => row.status === 'pending'))) throw new Error('Missing removal projection');
        const removals = Array.isArray(response.participantRemovals) ? response.participantRemovals.map(parseRemoval).filter((row): row is ParticipantRemoval => Boolean(row)) : [];
        const completed = current.current.some(row => row.status === 'pending' && !removals.some(next => next.participantId === row.participantId));
        if (completed) await refresh.current();
        if (cancelled || epoch !== generation.current) return;
        setItems(previous => [...previous.filter(row => row.status === 'blocked' && !removals.some(next => next.participantId === row.participantId)), ...removals]);
        setError('');
      } catch { if (!cancelled) setError('移交进度暂时未同步，正在重连。'); }
      finally { if (!cancelled) timer = setTimeout(poll, current.current.some(row => row.status === 'pending') ? 1500 : 5000); }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [room.id, transport]);
  return { items, error, receive(value: unknown) {
    const row = parseRemoval(value);
    if (!row) return;
    generation.current += 1;
    setItems(previous => [...previous.filter(item => item.participantId !== row.participantId), ...(row.status === 'completed' ? [] : [row])]);
  } };
}

export function PawRoomRemovalProgress({ room, items, error, busy, onRemove }: {
  room: RoomSummary; items: ParticipantRemoval[]; error: string; busy: boolean;
  onRemove: (participantId: string, extra?: { replacementParticipantId?: string; stopRoot?: boolean }) => void;
}) {
  const departing = new Set(items.filter(item => item.status === 'pending').map(item => item.participantId));
  return <div className="paw-room-removals">
    {error ? <p role="status">{error}</p> : null}
    {items.map(item => {
      const participant = room.participants.find(member => member.id === item.participantId && member.status === 'active');
      if (!participant) return null;
      const waiting = item.status === 'blocked' || ['requires_partner', 'revise_owner_lock', 'change_moderator', 'minimum_participants'].includes(item.stage);
      return <section key={item.participantId} className="paw-room-removals__item" aria-label={`${roomPlanetName(participant.ordinal)} 的移交进度`}>
        <div role="status"><ToolStatusMark status={waiting || error ? 'pending' : 'running'} /><strong>{roomPlanetName(participant.ordinal)}</strong><span>{removalStatusLabel(item.stage)}</span></div>
        <small>任务移交和停止回执确认后，伙伴才会离开。</small>
        {item.status === 'pending' && !item.stopRoot ? <label><span>接手伙伴</span><Select aria-label={`${roomPlanetName(participant.ordinal)} 的接手伙伴`} disabled={busy} value={item.targetParticipantId || 'automatic'} options={[
          { value: 'automatic', label: 'Jev 自动选择' },
          ...room.participants.filter(member => member.status === 'active' && member.id !== item.participantId && !departing.has(member.id)).map(member => ({ value: member.id, label: roomPlanetName(member.ordinal) })),
        ]} onValueChange={value => onRemove(item.participantId, { replacementParticipantId: value === 'automatic' ? '' : value })} /></label> : null}
        {item.stage === 'controller_requires_stop' ? <button type="button" disabled={busy} onClick={() => onRemove(item.participantId, { stopRoot: true })}>停止本轮并移出协调伙伴</button> : null}
      </section>;
    })}
  </div>;
}
