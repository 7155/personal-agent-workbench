import type { ReactNode } from 'react';
import {
  ConversationSurfaceProvider,
  type ConversationSurfaceController,
} from '../ConversationSurfaceContext';
import { VirtualTranscript } from './VirtualTranscript';

/**
 * The one conversation shell PAWOS mounts wherever a transcript is read: the
 * Room's public timeline, a partner satellite, a Session. The host owns state
 * and passes a controller; the shell owns reading — pinned scroll, variable
 * height virtualization, turn cards, tool receipts and steer receipts.
 *
 * `children` land in the surface foot, below the transcript, which is where a
 * host puts its queue tray and composer when it wants them inside the same
 * scroll container boundary.
 */
export function ConversationSurface({
  children,
  controller,
  density = 'comfortable',
  empty,
  label,
  lead,
  tail,
}: {
  controller: ConversationSurfaceController;
  label: string;
  density?: 'comfortable' | 'compact';
  lead?: ReactNode;
  tail?: ReactNode;
  empty?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <ConversationSurfaceProvider controller={controller}>
      <section
        aria-label={label}
        className="ccui-conversation-surface"
        data-conversation-id={controller.conversationId}
        data-density={density}
        data-phase={controller.phase}
      >
        <VirtualTranscript
          label={`${label}时间线`}
          {...(lead ? { lead } : {})}
          {...(tail ? { tail } : {})}
          {...(empty ? { empty } : {})}
        />
        {children ? <div className="ccui-surface-foot">{children}</div> : null}
      </section>
    </ConversationSurfaceProvider>
  );
}
