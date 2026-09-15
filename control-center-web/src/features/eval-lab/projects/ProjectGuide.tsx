import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import { agentCommandReceiptFailure, isAgentCommandPending, isAmbiguousAgentPromptFailure, isUnresolvedAgentCommandPending, publicAgentErrorText } from '@/features/agent/public-error';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { PawSessionWorkspace, sessionWorkspaceProjectionSlice } from '@/paw-os/apps/PawSessionWorkspace';
import type { ControlTransport } from '@/platform/transport';
import { labConnectionKey, requestLabControl } from '../control-request';
import { object, type LabProject } from './types';
import { projectGuidanceMessage } from './project-guidance';
import type { WorkspaceComposerContext } from '@/paw-os/apps/workspace-draft';
import { LabGuideWorkflow } from './LabGuideWorkflow';

export function ProjectGuide({ project, draftRequest, viewContext, onNewProject, onProjectActivity, onEnsure }: {
  viewContext?: Omit<WorkspaceComposerContext, 'onClear'>;
  project: LabProject; draftRequest?: { id: number; text: string }; onNewProject: () => void;
  onProjectActivity: () => void; onEnsure: () => void;
}) {
  const [dismissedContext, setDismissedContext] = useState('');
  const transport = useControlTransport(); const [updated, setUpdated] = useState<SessionSummary>();
  const query = useQuery({ queryKey: ['lab-project-guide', labConnectionKey(transport), project.projectId, project.guideSessionId],
    enabled: Boolean(project.guideSessionId), retry: false, refetchOnWindowFocus: false,
    queryFn: async () => {
      const response = await transport.request({ pathId: 'agent.sessions.list', query: { includeArchived: false, limit: 10,
        surfaceKind: 'extension_app', ownerAppId: 'extension:agent-lab', surfaceKey: `project.${project.projectId}.guide` } });
      const session = sessionItems(response, { includeAppOwned: true }).find((item) => item.id === project.guideSessionId
        && item.ownerAppId === 'extension:agent-lab' && item.surfaceKey === `project.${project.projectId}.guide`);
      if (!session) throw new Error('项目 Agent 会话尚未返回，请重新读取。');
      return session;
    },
  });
  const session = updated?.id === project.guideSessionId ? updated : query.data;
  return <section className="lab-project-guide" aria-label="项目 Agent">
    <header><span className="lab-project-agent-mark" aria-hidden="true" /><div><strong>项目 Agent</strong><small>围绕当前项目继续工作</small></div>{project.guideSessionId ? <LabGuideWorkflow key={project.guideSessionId} sessionId={project.guideSessionId} /> : null}</header>
    {!project.guideSessionId ? <div className="lab-project-guide__empty"><p>连接项目 Agent 后，就可以围绕材料和成果继续工作。</p><Button onClick={onEnsure}>连接项目 Agent</Button></div>
      : session ? <PawSessionWorkspace key={session.id} record={session} recordId={session.id} appearance="embedded" showComposerControls
        composerContext={viewContext && dismissedContext !== viewContext.text ? { ...viewContext, onClear: () => setDismissedContext(viewContext.text) } : undefined}
        composerPlaceholder="继续描述、修正成果，或让我尝试下一步…" draftRequest={draftRequest}
        onNewWork={onNewProject} onSessionCreated={setUpdated} onSessionUpdated={setUpdated} onSessionActivity={onProjectActivity} />
        : <div className="lab-project-guide__empty" role={query.isError ? 'alert' : 'status'}><p>{query.isError ? publicAgentErrorText(query.error, '项目 Agent 暂时无法读取。') : '正在恢复项目对话…'}</p>{query.isError ? <Button onClick={() => void query.refetch()}>重新读取对话</Button> : null}</div>}
  </section>;
}

/** The shared Pi Session owns admission, streaming and recovery after this send. */
export type PendingProjectMessage = { projectId: string; sessionId: string; clientMessageId: string; message: string; delivery: 'prompt' | 'steer'; outcome: 'unknown' | 'rejected'; error: string };
function messageKey(transport: ControlTransport, projectId: string) { return `paw.lab.project-messages.v1:${labConnectionKey(transport)}:${projectId}`; }
export function pendingProjectMessages(transport: ControlTransport, project: LabProject): PendingProjectMessage[] {
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(messageKey(transport, project.projectId)) ?? '[]');
    if (!Array.isArray(raw)) return [];
    return raw.filter((item): item is PendingProjectMessage => {
      const row = object(item);
      return row.projectId === project.projectId && row.sessionId === project.guideSessionId
        && typeof row.clientMessageId === 'string' && typeof row.message === 'string' && typeof row.error === 'string'
        && (row.delivery === 'prompt' || row.delivery === 'steer')
        && (row.outcome === 'unknown' || row.outcome === 'rejected');
    });
  } catch { return []; }
}
export function retainProjectMessage(transport: ControlTransport, project: LabProject, message: PendingProjectMessage, remove = false) {
  try {
    const remaining = pendingProjectMessages(transport, project).filter((item) => item.clientMessageId !== message.clientMessageId);
    if (!remove) remaining.push(message);
    sessionStorage.setItem(messageKey(transport, project.projectId), JSON.stringify(remaining));
  } catch { /* The shared live Session store also retains the current send. */ }
}
export async function sendProjectGuideMessage(transport: ControlTransport, project: LabProject, message: string, clientMessageId: string): Promise<void> {
  const sessionId = project.guideSessionId;
  if (!sessionId) throw new Error('项目 Agent 尚未连接。');
  const store = useAgentLiveStore.getState();
  const active = sessionWorkspaceProjectionSlice(store, sessionId).activeTurnId;
  const original = pendingProjectMessages(transport, project).find((item) => item.clientMessageId === clientMessageId);
  if (original && original.message !== message) throw new Error('原消息身份已绑定其他内容，请先核对已保留的消息。');
  const delivery = original?.delivery ?? (active ? 'steer' : 'prompt');
  const pending: PendingProjectMessage = { projectId: project.projectId, sessionId, clientMessageId, message, delivery, outcome: 'unknown', error: '' };
  retainProjectMessage(transport, project, pending);
  store.appendOptimistic(sessionId, { clientMessageId, text: message, nowMs: Date.now() });
  try {
    const response = object(await requestLabControl(transport, { pathId: 'agent.session.prompt', params: { sessionId },
      body: { message, clientMessageId, delivery } }));
    if (response.accepted === false && response.cancelled === true && response.admissionCancelled === true) {
      store.discardOptimistic(sessionId, clientMessageId);
      throw Object.assign(new Error('消息已取消，项目与成果已保留。'), { admissionCancelled: true });
    }
    if (response.accepted !== true) throw new TypeError('消息接纳回执未完整返回，请核对原消息。');
    store.acknowledgeOptimistic(sessionId, clientMessageId, Date.now());
    retainProjectMessage(transport, project, pending, true);
  } catch (reason) {
    const cancelled = object(reason).admissionCancelled === true;
    const uncertain = isAgentCommandPending(reason) || isAmbiguousAgentPromptFailure(reason)
      || (reason instanceof Error && /服务未及时返回回执|读取已取消/u.test(reason.message));
    retainProjectMessage(transport, project, { ...pending, outcome: uncertain ? 'unknown' : 'rejected', error: publicAgentErrorText(reason) }, cancelled);
    if (agentCommandReceiptFailure(reason)?.code === 'AGENT_COMMAND_CONFLICT') store.discardOptimistic(sessionId, clientMessageId);
    else {
      const state = isAgentCommandPending(reason) ? isUnresolvedAgentCommandPending(reason) ? 'unresolved' : 'pending'
        : uncertain ? 'ambiguous' : undefined;
      store.failOptimistic(sessionId, clientMessageId, publicAgentErrorText(reason), Date.now(), state);
    }
    throw reason;
  }
}
export const initialProjectMessage = projectGuidanceMessage('guided');
