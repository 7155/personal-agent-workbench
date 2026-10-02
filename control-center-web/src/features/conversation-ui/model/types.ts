/* Vendored clean-room conversation model. See ../ATTRIBUTION.md. */

import type { CodeModeDetails } from './codemode';

export type Id = string;

export type RunPhase = 'idle' | 'sending' | 'responding' | 'stopping' | 'error';

export type DeliveryStatus = 'sending' | 'sent' | 'failed';
export type SteerReceiptState = 'unread' | 'read' | 'settling' | 'done';
export type ToolStatus = 'pending' | 'running' | 'success' | 'error' | 'cancelled';

export interface AttachmentRef {
  id: Id;
  name: string;
  kind: 'file' | 'image' | 'context';
  size?: number;
}

export interface ToolCallBlock {
  id: Id;
  kind: 'tool';
  name: string;
  summary?: string;
  input?: string;
  output?: string;
  status: ToolStatus;
  /** Native Pi codemode receipt; nested calls stay inside their outer Tool row. */
  codeMode?: CodeModeDetails;
  /** A completed dispatch receipt assigns work; it does not complete the task. */
  receiptKind?: 'dispatch';
  /** Transport timeout is not proof that an already-sent operation failed. */
  executionOutcome?: 'applied' | 'unknown' | 'not_started';
  startedAt?: number;
  endedAt?: number;
}

export interface ThinkingBlock {
  id: Id;
  kind: 'thinking';
  summary: string;
  detail?: string;
  status: 'running' | 'done';
  startedAt?: number;
  endedAt?: number;
}

export interface TextBlock {
  id: Id;
  kind: 'text';
  text: string;
  streaming?: boolean;
}

export type AssistantBlock = TextBlock | ThinkingBlock | ToolCallBlock;

export interface UserMessage {
  id: Id;
  role: 'user';
  text: string;
  timestamp: number;
  attachments?: AttachmentRef[];
  deliveryStatus?: DeliveryStatus;
  queued?: boolean;
  awaitingPickup?: boolean;
  steerReceipt?: SteerReceiptState;
  parentId?: Id;
}

export interface AssistantMessage {
  id: Id;
  role: 'assistant';
  timestamp: number;
  blocks: AssistantBlock[];
  parentId?: Id;
  stopReason?: string;
  error?: string;
  /** PAWOS addition: which Runtime actor published this loop. */
  actor?: string;
  /** Stable participant identity; display names never determine avatar ownership. */
  actorId?: string;
  /** Exact source Session published with this Room card; absent if unbound or conflicting. */
  actorSessionId?: string;
  /** PAWOS addition: secondary actor line (collaboration role, tool owner). */
  actorRole?: string;
  /** PAWOS addition: stable Runtime turn identity behind this card. */
  turnId?: Id;
}

export type TranscriptMessage = UserMessage | AssistantMessage;

export interface QueuedDraft {
  id: Id;
  text: string;
  attachments: AttachmentRef[];
  queuedAt: number;
  forConversationId: Id;
  queuedWhileBusy: boolean;
  queuedBehindPending: boolean;
}
