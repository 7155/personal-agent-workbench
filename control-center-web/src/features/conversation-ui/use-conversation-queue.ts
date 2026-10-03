import { useCallback, useEffect, useRef, useState } from 'react';
import {
  editQueuedDraft,
  enqueueQueuedDraft,
  FRONTEND_QUEUE_CAP,
  mergeQueueBackToDraft,
  removeQueuedDraft,
  reorderQueuedDrafts,
} from './model/queue';
import type { QueuedDraft } from './model/types';

export interface ConversationQueueController {
  queue: readonly QueuedDraft[];
  capReached: boolean;
  /** Hold a draft in front of the Runtime send path. `false` means the cap
   *  refused it and the text must stay in the composer. */
  enqueue(text: string): boolean;
  remove(id: string): void;
  edit(id: string, text: string): void;
  reorder(activeId: string, overId: string): void;
  clear(): void;
  sendNow(id: string): void;
  /** Stop pulls unconsumed drafts back into the composer instead of dropping
   *  them — the queue only ever held them, it never dispatched them. */
  restoreToDraft(currentText: string): string;
}

/**
 * Front-end follow-up queue with the clean-room package's contract: cap 8,
 * one drain per settled turn, reorder/edit/remove/clear, and restore-on-stop.
 *
 * Nothing here reaches Pi until `send` is called for the head draft, so every
 * queued row stays fully reversible and no Runtime ordering is invented.
 */
export function useConversationQueue({
  busy,
  conversationId,
  send,
  cap = FRONTEND_QUEUE_CAP,
}: {
  busy: boolean;
  conversationId: string;
  /** `false` is a synchronous refusal: keep the held draft for correction or
   *  an explicit retry. Existing void callers accept the handoff. */
  send(text: string): boolean | void;
  cap?: number;
}): ConversationQueueController {
  const [queue, setQueue] = useState<readonly QueuedDraft[]>([]);
  const [capReached, setCapReached] = useState(false);
  // Admission and consumption happen synchronously in event handlers. React
  // may defer/replay state updaters, so their return timing cannot decide
  // whether the composer should clear or a draft has already been sent.
  const queueRef = useRef<{ conversationId: string; items: readonly QueuedDraft[] }>({ conversationId, items: [] });
  const replaceQueue = useCallback((items: readonly QueuedDraft[]) => {
    if (queueRef.current.conversationId !== conversationId) return;
    queueRef.current = { conversationId, items };
    setQueue(items);
  }, [conversationId]);
  const sendRef = useRef(send);
  sendRef.current = send;

  useEffect(() => {
    queueRef.current = { conversationId, items: [] };
    replaceQueue([]);
    setCapReached(false);
  }, [conversationId, replaceQueue]);

  // Drain exactly one held draft once the current turn settles. Keeping this
  // in an effect makes the dependency on committed busy state explicit and
  // avoids racing the optimistic append the send path performs.
  useEffect(() => {
    if (busy || queueRef.current.conversationId !== conversationId) return;
    const next = queueRef.current.items[0];
    if (!next) return;
    if (sendRef.current(next.text) === false) return;
    replaceQueue(removeQueuedDraft(queueRef.current.items, next.id));
    setCapReached(false);
  }, [busy, conversationId, queue, replaceQueue]);

  const enqueue = useCallback((text: string) => {
    const value = text.trim();
    if (!value) return false;
    if (queueRef.current.conversationId !== conversationId) return false;
    const current = queueRef.current.items;
    const result = enqueueQueuedDraft(current, {
      id: `queued-${crypto.randomUUID()}`,
      text: value,
      conversationId,
      busy: true,
      existingDepth: current.length,
    }, cap);
    if (result.accepted) replaceQueue(result.queue);
    setCapReached(!result.accepted);
    return result.accepted;
  }, [cap, conversationId, replaceQueue]);

  const remove = useCallback((id: string) => {
    if (queueRef.current.conversationId !== conversationId) return;
    replaceQueue(removeQueuedDraft(queueRef.current.items, id));
    setCapReached(false);
  }, [conversationId, replaceQueue]);

  const edit = useCallback((id: string, text: string) => {
    if (queueRef.current.conversationId !== conversationId) return;
    replaceQueue(editQueuedDraft(queueRef.current.items, id, text));
  }, [conversationId, replaceQueue]);

  const reorder = useCallback((activeId: string, overId: string) => {
    if (queueRef.current.conversationId !== conversationId) return;
    replaceQueue(reorderQueuedDrafts(queueRef.current.items, activeId, overId));
  }, [conversationId, replaceQueue]);

  const clear = useCallback(() => {
    if (queueRef.current.conversationId !== conversationId) return;
    replaceQueue([]);
    setCapReached(false);
  }, [conversationId, replaceQueue]);

  const sendNow = useCallback((id: string) => {
    if (queueRef.current.conversationId !== conversationId) return;
    const item = queueRef.current.items.find((candidate) => candidate.id === id);
    if (!item) return;
    // React may invoke state updater functions twice in StrictMode. Dispatching
    // from inside the updater therefore sent one human action to Runtime twice.
    // Resolve the immutable queued draft first, then keep the updater pure.
    if (sendRef.current(item.text) === false) return;
    replaceQueue(removeQueuedDraft(queueRef.current.items, id));
    setCapReached(false);
  }, [conversationId, replaceQueue]);

  const restoreToDraft = useCallback((currentText: string) => {
    if (queueRef.current.conversationId !== conversationId) return currentText;
    const restored = mergeQueueBackToDraft(queueRef.current.items, currentText);
    replaceQueue([]);
    setCapReached(false);
    return restored;
  }, [conversationId, replaceQueue]);

  return { queue, capReached, enqueue, remove, edit, reorder, clear, sendNow, restoreToDraft };
}
