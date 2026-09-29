import { useMemo } from 'react';
import type { RoomProjectionState } from '@/contracts/room-reducer';
import type { RoomSummary } from '@/features/rooms/room-types';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import { roomPlanetName } from '@/features/rooms/room-copy';
import { JEV_TASK_STAGE_LABELS, type JevSnapshot } from '@/features/semantic-workspace/jev-execution';
import { jevMission } from '@/features/semantic-workspace/jev-mission';
import { useReceiptHighlight } from '@/features/semantic-workspace/use-receipt-highlight';
import { usePresentationMotion } from '@/features/conversation-ui/reading/reading-preferences';
import { buildJevCollaboration, type CollaborationNode, type CollaborationPerson } from './jev-collaboration-model';
import { JevCollaborationView, CollaborationIcon } from './JevCollaborationView';

export interface JevCollaborationPanelProps {
  graph: JevSnapshot | null;
  room?: RoomSummary;
  projection?: RoomProjectionState;
  active: boolean;
  historical?: boolean;
  observeCompletions?: boolean;
  presentation?: 'peek' | 'full';
  onExpand?: () => void;
  onInspectTask: (node: CollaborationNode) => void;
  onOpenParticipant?: (id: string) => void;
}

/** Existing Jev state -> presentation. No new subscription, Runtime or dispatch owner. */
export function JevCollaborationPanel({ graph, room, projection, active, historical = false,
  observeCompletions = true, presentation = 'full', onExpand, onInspectTask, onOpenParticipant,
}: JevCollaborationPanelProps) {
  const mission = useMemo(() => jevMission(graph), [graph]);
  const model = useMemo(() => buildJevCollaboration(graph, room, mission.tasks, JEV_TASK_STAGE_LABELS, roomPlanetName, projection), [graph, room, mission, projection]);
  const motionAllowed = usePresentationMotion(active && !historical && !model.stopped && !model.final);
  const keys = model.nodes.map(node => `${node.id}:${node.revision}:${node.stage}`);
  const fresh = useReceiptHighlight(model.graphId, keys, presentation === 'full' && motionAllowed && observeCompletions, 1000);
  const avatar = (person: CollaborationPerson, size: number, running: boolean) => <RoomPlanetAvatar ordinal={person.ordinal} size={size} decorative activity={running && motionAllowed ? 'working' : 'static'} />;
  if (presentation === 'peek') {
    const involved = model.people.filter(person => model.nodes.some(node => node.owner?.id === person.id || node.runs.some(run => run.person?.id === person.id)) || model.runs.some(run => run.person?.id === person.id));
    if (!graph || !model.nodes.length && !model.runs.length) return null;
    return <button className="jcv-peek" type="button" onClick={onExpand} disabled={!onExpand} aria-label="打开多 Agent 协作全景">
      <span className="jcv-peek__top"><span className="jcv-peek__planets">{involved.slice(0, 5).map(person => <span key={person.id} title={person.name}>{avatar(person, 28, false)}</span>)}{involved.length > 5 ? <small>+{involved.length - 5}</small> : null}</span><CollaborationIcon name="expand" size={15} /></span>
      <strong>协作全景<CollaborationIcon name="arrow" size={14} /></strong><span className="jcv-peek__copy">谁在执行，谁在等待，结果如何交接</span>
      <span className="jcv-peek__mini" aria-hidden="true">{model.nodes.slice(0, 12).map(node => <i key={node.id} data-tone={node.tone} />)}</span>
    </button>;
  }
  return <JevCollaborationView model={model} active={active} historical={historical} motionAllowed={motionAllowed} freshKeys={fresh} renderAvatar={avatar} onInspectTask={onInspectTask} onOpenParticipant={onOpenParticipant} />;
}
