import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { BrainCircuit } from 'lucide-react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Select } from '@/components/primitives';
import { ToolPicker } from '@/features/agent/composer/ToolPicker';
import { ModelPicker } from '@/features/agent/composer/ModelPicker';
import { modelSelectionFromCatalog, sameModelSelection, type AgentModelSelection } from '@/features/agent/model-selection';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { toolItems } from '@/features/agent/types';
import type { ModelCatalog } from '@/features/agent/types';
import { requireSessionCapabilityCatalog, type CapabilityPreference } from '@/features/plugins/capability-policy';
import { roomParticipantPlanetName } from '../room-participant-identity';
import type { RoomSummary } from '../room-types';
import { RoomPartnerPermissionControls } from './RoomPartnerPermissionControls';

type Participant = {
  id: string;
  sessionId: string;
  displayName: string;
  ordinal?: number;
  status: string;
};

/** Room partners are ordinary Sessions; these controls update that same policy owner. */
export function RoomCapabilityControls({ participants, aliases = {}, busy, disabled, onSelectTool, showModelControls = true, room, onRoomUpdated, showSessionSettings = false, automaticModels = false, modelSyncKey = '' }: {
  participants: Participant[];
  aliases?: Readonly<Record<string, string>>;
  busy: boolean;
  disabled: boolean;
  showModelControls?: boolean;
  room?: RoomSummary;
  onRoomUpdated?: (room: RoomSummary) => void;
  showSessionSettings?: boolean;
  automaticModels?: boolean;
  modelSyncKey?: string;
  onSelectTool: (name: string) => void;
}) {
  const transport = useControlTransport();
  const client = useQueryClient();
  const [selectedId, setSelectedId] = useState('');
  const active = participants.filter((participant) => participant.status === 'active' && participant.sessionId);
  const selected = active.find((participant) => participant.id === selectedId) ?? active[0];
  const sessionId = selected?.sessionId ?? '';
  const queryKey = (id: string) => ['room-composer-capabilities', id];
  async function read(id: string, signal?: AbortSignal) {
    const response = await transport.request({ pathId: 'agent.tools.list', query: { sessionId: id }, signal });
    return { catalog: requireSessionCapabilityCatalog(response, id), tools: toolItems(response) };
  }
  const query = useQuery({
    queryKey: queryKey(sessionId),
    queryFn: ({ signal }) => read(sessionId, signal),
    enabled: Boolean(sessionId) && !disabled,
    retry: false,
  });
  const mutation = useMutation({
    mutationFn: async ({ owner, canonicalId, preference }: { owner: string; canonicalId: string; preference: CapabilityPreference }) => {
      const current = await read(owner);
      await transport.request({
        pathId: 'agent.session.capability-policy.update', params: { sessionId: owner },
        body: { capabilityDisclosurePreferences: {
          ...current.catalog.sessionPolicy!.disclosurePreferences.session,
          [canonicalId]: preference,
        } },
      });
      const confirmed = await read(owner);
      if (!confirmed.catalog.items.some((item) => item.canonicalId === canonicalId)
        || (confirmed.catalog.sessionPolicy!.disclosurePreferences.session[canonicalId] ?? 'inherit') !== preference) {
        throw new Error('设置已提交，但暂时无法确认该功能的状态，请重新读取。');
      }
      client.setQueryData(queryKey(owner), confirmed);
      return confirmed;
    },
  });
  const error = mutation.isError && mutation.variables.owner === sessionId ? mutation.error : query.error;
  const memory = query.data?.catalog.items.find(item => item.canonicalId === 'tool:memory');
  const memoryEnabled = memory?.disclosure.effective === 'enabled';
  if (!selected) return null;
  return <>
    <Select
      className="room-composer__partner-picker"
      aria-label="选择要设置记忆和插件的伙伴"
      value={selected.id}
      options={active.map((participant) => ({ value: participant.id, label: aliases[participant.id] ?? roomParticipantPlanetName(participant) }))}
      onValueChange={setSelectedId}
    />
    {showSessionSettings && room ? <RoomPartnerPermissionControls room={room} sessionId={sessionId} name={aliases[selected.id] ?? roomParticipantPlanetName(selected)} busy={busy} disabled={disabled} onRoomUpdated={onRoomUpdated} /> : null}
    {showSessionSettings ? <Button className="agent-composer__picker" size="small" variant="quiet" leadingIcon={<BrainCircuit size={14} aria-hidden />} role="switch" aria-checked={Boolean(memoryEnabled)} aria-label={`${aliases[selected.id] ?? roomParticipantPlanetName(selected)} 的记忆`} title={`仅作用于当前伙伴 ${aliases[selected.id] ?? roomParticipantPlanetName(selected)}，更改从下一轮生效`} disabled={!memory || busy || disabled || mutation.isPending} onClick={() => mutation.mutate({ owner: sessionId, canonicalId: 'tool:memory', preference: memoryEnabled ? 'disabled' : 'enabled' })}>记忆 · {memory ? memoryEnabled ? '开' : '关' : query.isError ? '未同步' : '未提供'}</Button> : null}
    {showModelControls ? <RoomPartnerModelControls key={sessionId} sessionId={sessionId} disabled={disabled || busy} syncKey={modelSyncKey} /> : null}
    {showSessionSettings && automaticModels ? <span className="room-composer__model-policy-note" title="上方显示当前伙伴的模型与思考强度；自动路由会在每个执行阶段按任务选择模型。">执行时自动路由</span> : null}
    <ToolPicker
      sessionId={sessionId}
      capabilityCatalog={query.data?.catalog}
      tools={query.data?.tools ?? []}
      status={query.data ? 'ready' : query.isError ? 'failed' : 'loading'}
      adjustmentDisabled={busy || disabled}
      capabilityPolicyPending={mutation.isPending}
      disabled={disabled}
      requestOpen={0}
      onSelect={(tool) => onSelectTool(tool.displayName)}
      onCapabilityPreferenceChange={(canonicalId, preference) => {
        if (!busy && !disabled && !mutation.isPending) mutation.mutate({ owner: sessionId, canonicalId, preference });
      }}
    />
    {error ? <span role="alert" className="room-composer__capability-error">
      {publicAgentErrorText(error)}
      <Button size="small" variant="quiet" onClick={() => { mutation.reset(); void query.refetch(); }}>重新读取</Button>
    </span> : null}
  </>;
}

function RoomPartnerModelControls({ sessionId, disabled, syncKey }: { sessionId: string; disabled: boolean; syncKey: string }) {
  const transport = useControlTransport();
  const client = useQueryClient();
  const queryKey = ['room-composer-model', sessionId];
  async function read(signal?: AbortSignal) {
    const catalog = await transport.request<ModelCatalog>({ pathId: 'agent.session.models', params: { sessionId }, responseContract: 'agent-model-catalog.v1', signal });
    if (catalog.sessionId !== sessionId) throw new Error('暂时无法确认这位伙伴的模型，请重新读取。');
    return catalog;
  }
  const query = useQuery({ queryKey, queryFn: ({ signal }) => read(signal), retry: false });
  useEffect(() => { if (syncKey) void query.refetch(); }, [syncKey]);
  const mutation = useMutation({
    mutationFn: async (selection: AgentModelSelection) => {
      const current = await read();
      if (!sameModelSelection(modelSelectionFromCatalog(current), selection)) {
        await transport.request({ pathId: 'agent.session.model.select', params: { sessionId }, body: { provider: selection.provider, modelId: selection.modelId } });
        await transport.request({ pathId: 'agent.session.thinking.select', params: { sessionId }, body: { level: selection.level } });
      }
      const confirmed = await read();
      client.setQueryData(queryKey, confirmed);
      if (!sameModelSelection(modelSelectionFromCatalog(confirmed), selection)) throw new Error('伙伴的模型设置尚未确认，请重新读取。');
    },
    onSettled: () => client.invalidateQueries({ queryKey }),
  });
  const error = mutation.error ?? query.error;
  return <>
    <ModelPicker catalog={query.data} disabled={disabled || !query.data} pending={mutation.isPending || query.isPending} requestOpen={0} onOpen={() => void query.refetch()} onChange={(provider, modelId, level) => { if (!disabled && !mutation.isPending) mutation.mutate({ provider, modelId, level }); }} />
    {error ? <span role="alert" className="room-composer__capability-error">{publicAgentErrorText(error)}<Button size="small" variant="quiet" onClick={() => { mutation.reset(); void query.refetch(); }}>重新读取模型</Button></span> : null}
  </>;
}
