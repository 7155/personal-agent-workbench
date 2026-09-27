import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Popover, PopoverContent, PopoverTrigger } from '@/components/primitives';
import { permissionPreset } from '@/features/agent/composer/permission-policy';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { RoomPermissionPolicyEditor } from '../room-presentation';
import { parseRoomPermissionPolicy, roomPermissionPoliciesEqual, roomPermissionPolicyNeedsDangerousConfirmation, roomPermissionPolicyNeedsWorkspaceConfirmation, type RoomPermissionPolicy, type RoomSummary } from '../room-types';
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

/** Read the selected Pi Session; change permissions through its Room owner. */
export function RoomPartnerPermissionControls({ room, sessionId, name, busy, disabled, onRoomUpdated }: {
  room: RoomSummary; sessionId: string; name: string; busy: boolean; disabled: boolean; onRoomUpdated?: (room: RoomSummary) => void;
}) {
  const transport = useControlTransport(); const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const stored = parseRoomPermissionPolicy(room.permissionPolicy, room.roomKind);
  const [draft, setDraft] = useState<RoomPermissionPolicy | undefined>(stored);
  useEffect(() => { setDraft(parseRoomPermissionPolicy(room.permissionPolicy, room.roomKind)); }, [room.permissionPolicy, room.roomKind]);
  const key = ['room-composer-permission', sessionId];
  const query = useQuery({ queryKey: key, retry: false, enabled: Boolean(sessionId), staleTime: 30_000, queryFn: async ({ signal }) => {
    // The messages snapshot contains transcript/telemetry, not Session metadata.
    // Follow the canonical directory cursor until this exact partner is found.
    let cursor: { beforeUpdatedAtMs: number; beforeId: string } | undefined;
    const seen = new Set<string>();
    while (!signal.aborted) {
      const value = record(await transport.request({ pathId: 'agent.sessions.list', query: { limit: 100, includeArchived: true, ...cursor }, signal }));
      if (!Array.isArray(value.items)) break;
      const session = value.items.map(record).find(item => item.id === sessionId);
      if (session) {
        if (!['read_only', 'per_action', 'workspace_managed', 'full_trust'].includes(String(session.executionMode))) break;
        return permissionPreset(session.executionMode as 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust', String(session.toolProfileVersion || 'control-center-v1'));
      }
      if (value.hasMore !== true || typeof value.nextBeforeUpdatedAtMs !== 'number' || typeof value.nextBeforeId !== 'string' || !value.nextBeforeId) break;
      const next = `${value.nextBeforeUpdatedAtMs}:${value.nextBeforeId}`;
      if (seen.has(next)) break;
      seen.add(next);
      cursor = { beforeUpdatedAtMs: value.nextBeforeUpdatedAtMs, beforeId: value.nextBeforeId };
    }
    throw new Error('当前伙伴的权限尚未同步。');
  } });
  const mutation = useMutation({ mutationFn: async () => {
    if (!draft || busy || disabled) return;
    await transport.request({ pathId: 'agent.room.archive', params: { roomId: room.id }, body: { archived: false, permissionPolicy: draft,
      ...(roomPermissionPolicyNeedsWorkspaceConfirmation(draft) ? { workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE' } : {}),
      ...(roomPermissionPolicyNeedsDangerousConfirmation(draft) ? { dangerousModeConfirmation: 'ENABLE_FULL_TRUST' } : {}),
    } });
    const response = record(await transport.request({ pathId: 'agent.room.get', params: { roomId: room.id } }));
    const updated = record(response.room);
    if (updated.id !== room.id || !roomPermissionPoliciesEqual(parseRoomPermissionPolicy(updated.permissionPolicy, room.roomKind), draft)) throw new Error('权限已提交，尚未读到一致回执，请重新同步。');
    onRoomUpdated?.(updated as unknown as RoomSummary);
    await client.invalidateQueries({ queryKey: ['room-composer-permission'] });
    setOpen(false);
  } });
  const error = mutation.error ?? query.error;
  return <Popover open={open} onOpenChange={value => { setOpen(value); if (value) void query.refetch(); }}><PopoverTrigger asChild><Button variant="quiet" size="small" className="agent-composer__picker" leadingIcon={<ShieldCheck size={14} aria-hidden />} disabled={disabled} aria-label={`${name} 当前权限：${query.data?.label ?? '尚未同步'}`} title={`查看 ${name} 当前权限；分层更改作用于整个 Room`}>权限 · {query.data?.label ?? '待同步'}</Button></PopoverTrigger><PopoverContent align="start" className="room-partner-permission-popover"><header><strong>{name} 的当前权限</strong><span>{query.data?.label ?? '尚未同步'}</span></header><p>当前值来自这位伙伴的 Session。下方分层策略由 Room 管理，保存会作用于本 Room 及其伙伴。</p><RoomPermissionPolicyEditor policy={draft} roomKind={room.roomKind ?? 'collaboration'} onChange={busy || disabled ? undefined : setDraft} compact menuSelect />{busy ? <p>当前有任务运行，结束后可调整权限。</p> : null}{error ? <p role="alert">{publicAgentErrorText(error)}</p> : null}<footer><Button variant="quiet" size="small" onClick={() => { mutation.reset(); void query.refetch(); }}>重新同步</Button><Button size="small" disabled={!draft || busy || disabled || mutation.isPending || roomPermissionPoliciesEqual(draft, stored)} onClick={() => mutation.mutate()}>保存 Room 分层权限</Button></footer></PopoverContent></Popover>;
}
