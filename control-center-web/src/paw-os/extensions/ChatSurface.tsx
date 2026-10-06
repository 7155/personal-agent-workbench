import type { ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { agentSessionAddress, latestActiveAgentTurnId, selectAgentProjection, useAgentLiveStore } from '@/features/agent/state/live-store';
import type { SessionSummary } from '@/features/agent/types';
import { PawSessionWorkspace } from '@/paw-os/apps/PawSessionWorkspace';
import { messageWithWorkspaceContext } from '@/paw-os/apps/workspace-draft';
import { chatSurfaceCompatibility, type ChatSurfaceRequirement } from './chat-surface-contract';
export { CHAT_SURFACE_API_MAJOR, chatSurfaceCompatibility, type ChatSurfaceRequirement } from './chat-surface-contract';

export type ChatComposerContextV1 = {
  contextId?: string; kind?: 'map' | 'project'; label: string; detail: string; text: string;
  onClear(): void; onOpen?(): void; items?: Array<{ id: string; label: string; onRemove(): void }>;
};
export type ChatComposerHeaderV1 = {
  session: SessionSummary; draft: string; disabled: boolean; sourceMessageId(): string | undefined;
};

export function chatMessageWithContextV1(message: string, context?: ChatComposerContextV1): string {
  return messageWithWorkspaceContext(message, context);
}
export type ChatSurfaceV1Props = {
  api: ChatSurfaceRequirement;
  session: SessionSummary;
  active?: boolean;
  view?: 'workspace' | 'embedded';
  composer?: {
    placeholder?: string; showControls?: boolean;
    /** Visible user-message data, passed through the existing context owner.
     * This is never a system prompt, workspace authorization, or tool policy. */
    context?: ChatComposerContextV1;
    draftIntent?: { id: number; text: string; contextKey?: string };
    header?: (view: ChatComposerHeaderV1) => ReactNode;
  };
  onNewConversation(): void;
  onSessionCreated(session: SessionSummary, draft: string): void;
  onSessionUpdated(session: SessionSummary): void;
  onActivity?(): void;
};

/** Stable OS entry for vertical Apps. Session state, admission, history,
 * attachments, tools, Stop and recovery remain in the existing owner. */
export function PawChatSurface({ api, session, active, view = 'workspace', composer, onNewConversation, onSessionCreated, onSessionUpdated, onActivity }: ChatSurfaceV1Props) {
  const compatibility = chatSurfaceCompatibility(api);
  if (!compatibility.supported) return <div className="paw-extension-app-state" role="alert">
    <strong>当前聊天界面不兼容</strong><span>{compatibility.reason}</span>
    <span>请更新 App 或工作台后重试。现有对话记录不会被删除。</span>
  </div>;
  return <PawSessionWorkspace record={session} recordId={session.id} active={active}
    appearance={view === 'embedded' ? 'embedded' : 'full'} composerPlaceholder={composer?.placeholder}
    showComposerControls={composer?.showControls} composerContext={composer?.context} draftRequest={composer?.draftIntent} renderComposerHeader={composer?.header}
    onNewWork={onNewConversation} onSessionCreated={onSessionCreated} onSessionUpdated={onSessionUpdated} onSessionActivity={onActivity} />;
}

/** A narrow read contract; Apps need not depend on Workspace/store internals. */
export function useChatSurfaceBusy(sessionId: string): boolean {
  const transport = useControlTransport();
  const address = agentSessionAddress(transport, sessionId);
  return useAgentLiveStore(state => {
    const projection = selectAgentProjection(state, address);
    return Boolean(projection?.durableRecovery?.compactionTarget
      || projection?.durableRecovery?.activeTurn?.turnId || latestActiveAgentTurnId(projection));
  });
}
