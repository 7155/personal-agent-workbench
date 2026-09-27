import { useCallback, useEffect, useMemo, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { conversationBusy, useConversationSurface } from '../ConversationSurfaceContext';
import { usePinnedTranscript } from '../hooks/usePinnedTranscript';
import {
  readSessionScrollMemory,
  useSessionScrollMemory,
} from '../hooks/useSessionScrollMemory';
import { useVirtualTranscript } from '../hooks/useVirtualTranscript';
import type { TranscriptMessage } from '../model/types';
import { JumpToBottom } from './JumpToBottom';
import { TranscriptRow } from './TranscriptRow';

/* A first guess only: every mounted row reports its real height back through
 * the virtualizer's ResizeObserver, and the guess is never used again. */
function estimateMessage(message: TranscriptMessage): number {
  if (message.role === 'user') return Math.min(240, 74 + Math.ceil(message.text.length / 65) * 22);
  let size = 56;
  for (const block of message.blocks) {
    if (block.kind === 'text') size += Math.min(640, 38 + Math.ceil(block.text.length / 72) * 22);
    else if (block.kind === 'thinking') size += block.detail ? 62 : 38;
    else size += block.output ? 88 : 58;
  }
  return Math.max(80, size);
}

export function VirtualTranscript({ empty, label, lead, tail }: {
  label: string;
  lead?: ReactNode;
  tail?: ReactNode;
  empty?: ReactNode;
}) {
  const surface = useConversationSurface();
  const { conversationId, messages, phase } = surface;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sizerRef = useRef<HTMLDivElement | null>(null);
  const pinned = usePinnedTranscript(scrollRef, sizerRef, conversationId);
  const rememberedAnchor = readSessionScrollMemory(conversationId)?.row;

  const getKey = useCallback((message: TranscriptMessage) => message.id, []);
  const estimate = useCallback((message: TranscriptMessage) => estimateMessage(message), []);
  const virtual = useVirtualTranscript({
    items: messages,
    getKey,
    estimateSize: estimate,
    scrollRef,
    initialScrollKey: conversationId,
    ...(rememberedAnchor ? {
      initialAnchor: {
        key: rememberedAnchor.rowKey,
        index: rememberedAnchor.rowIndex,
        offsetFromViewportTopPx: rememberedAnchor.offsetFromViewportTopPx,
      },
    } : {}),
  });

  useSessionScrollMemory(conversationId, pinned.captureAnchor, pinned.restoreAnchor, scrollRef);

  /* Arriving content pins in the same commit. The resize observation behind
   * the hook only catches later growth of an already-mounted row — streamed
   * text, an opened disclosure — and would otherwise leave the tail behind by
   * a frame every time the transcript itself changes. */
  const { isPinnedRef, scrollToBottom } = pinned;
  useEffect(() => {
    if (isPinnedRef.current) scrollToBottom('auto');
  }, [isPinnedRef, messages, scrollToBottom, tail]);

  const messageIndex = useMemo(
    () => new Map(messages.map((message, index) => [message.id, index])),
    [messages],
  );

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).matches('input,textarea,button,[contenteditable=true]')) return;
    const current = document.activeElement?.closest<HTMLElement>('[data-message-id]')?.dataset.messageId;
    const index = current ? messageIndex.get(current) ?? messages.length - 1 : messages.length - 1;
    const scroller = scrollRef.current;
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      virtual.scrollToIndex(Math.max(0, index - 1), 'center');
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      virtual.scrollToIndex(Math.min(messages.length - 1, index + 1), 'center');
    } else if (event.key === 'PageUp' && scroller) {
      event.preventDefault();
      scroller.scrollTop -= (scroller.clientHeight || 600) * 0.8;
    } else if (event.key === 'PageDown' && scroller) {
      event.preventDefault();
      scroller.scrollTop += (scroller.clientHeight || 600) * 0.8;
    }
  };

  return (
    <div className="ccui-transcript-shell">
      <div
        aria-busy={conversationBusy(phase)}
        aria-label={label}
        className="ccui-transcript-scroll"
        onKeyDown={onKeyDown}
        ref={scrollRef}
        role="log"
        tabIndex={0}
      >
        {lead ? <div className="ccui-transcript-lead">{lead}</div> : null}
        <div className="ccui-transcript-content" ref={sizerRef}>
        <div className="ccui-transcript-sizer" style={{ height: virtual.totalSize }}>
          {virtual.virtualRows.map((row) => {
            const message = messages[row.index];
            if (!message) return null;
            return (
              <div
                className="ccui-virtual-row"
                data-index={row.index}
                data-message-id={message.id}
                key={row.key}
                ref={virtual.measureElement(row.key)}
                style={{ transform: `translateY(${row.start}px)` }}
              >
                <TranscriptRow message={message} />
              </div>
            );
          })}
        </div>
        {tail ? <div className="ccui-transcript-tail">{tail}</div> : null}
        </div>
        {messages.length === 0 && empty ? <div className="ccui-empty-transcript">{empty}</div> : null}
      </div>
      <JumpToBottom onClick={() => pinned.scrollToBottom('smooth')} visible={pinned.showJumpToBottom} />
    </div>
  );
}
