import { createContext, useContext, type ReactNode } from 'react';
import type {
  AssistantBlock,
  AssistantMessage,
  RunPhase,
  TranscriptMessage,
  UserMessage,
} from './model/types';

/**
 * PAWOS keeps conversation state in its Runtime-fed live stores, so the
 * clean-room surface reads an already-reduced transcript instead of owning a
 * reducer of its own. This controller is the whole contract a host surface
 * (Room, Room partner satellite, Session) has to satisfy; every capability is
 * optional and the components hide the affordances a host cannot honour.
 */
export interface ConversationSurfaceCapabilities {
  retry: boolean;
  edit: boolean;
  fork: boolean;
  rewind: boolean;
  interrupt: boolean;
  copy: boolean;
}

export interface ConversationSurfaceController {
  conversationId: string;
  messages: readonly TranscriptMessage[];
  phase: RunPhase;
  capabilities: ConversationSurfaceCapabilities;
  retry?(message: AssistantMessage): void;
  /** Per-card guard: a Room only retries the newest unsuperseded failure. */
  canRetry?(message: AssistantMessage): boolean;
  retryPending?: boolean;
  edit?(message: UserMessage): void;
  fork?(message: TranscriptMessage): void;
  rewind?(message: TranscriptMessage): void;
  interruptSteer?(message: UserMessage): void;
  cancelSteer?(message: UserMessage): void;
  /** Host override for a Runtime block the shared card cannot present alone. */
  renderBlock?(block: AssistantBlock, message: AssistantMessage): ReactNode | undefined;
  /** Host body inside a tool card's disclosure (structured diff, tool facts). */
  renderBlockDetail?(block: AssistantBlock, message: AssistantMessage): ReactNode | undefined;
  /** Host action row under a card head; stays visible so an approval never
   *  hides behind a disclosure. */
  renderBlockAction?(block: AssistantBlock, message: AssistantMessage): ReactNode | undefined;
  /** Host slot below an assistant card (terminal receipts, session links). */
  renderMessageFooter?(message: AssistantMessage): ReactNode | undefined;
  renderMessageAvatar?(message: AssistantMessage): ReactNode | undefined;
  /** Optional host-owned identity link; ordinary Session headings stay unchanged. */
  renderMessageIdentity?(message: AssistantMessage): ReactNode | undefined;
  /** Host-validated workspace binding for file references in public prose. */
  resolveMessageSessionId?(message: AssistantMessage): string;
  formatTimestamp(timestamp: number): string;
}

const ConversationSurfaceContext = createContext<ConversationSurfaceController | null>(null);

export function ConversationSurfaceProvider({
  controller,
  children,
}: {
  controller: ConversationSurfaceController;
  children: ReactNode;
}) {
  return <ConversationSurfaceContext.Provider value={controller}>{children}</ConversationSurfaceContext.Provider>;
}

export function useConversationSurface(): ConversationSurfaceController {
  const value = useContext(ConversationSurfaceContext);
  if (!value) throw new Error('useConversationSurface must be used inside ConversationSurfaceProvider');
  return value;
}

export const NO_CAPABILITIES: ConversationSurfaceCapabilities = {
  retry: false,
  edit: false,
  fork: false,
  rewind: false,
  interrupt: false,
  copy: true,
};

export function conversationClock(timestamp: number): string {
  return timestamp
    ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(timestamp))
    : '';
}

export function conversationBusy(phase: RunPhase): boolean {
  return phase === 'sending' || phase === 'responding' || phase === 'stopping';
}
