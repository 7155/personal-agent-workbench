import { Paperclip } from 'lucide-react';
import { useConversationSurface } from '../ConversationSurfaceContext';
import type { UserMessage } from '../model/types';
import { MessageActions } from './MessageActions';
import { SteerReceipt } from './SteerReceipt';

export function UserTurn({ message }: { message: UserMessage }) {
  const surface = useConversationSurface();
  const { capabilities } = surface;
  return (
    <article
      className={`ccui-turn ccui-user-turn${message.queued ? ' is-queued' : ''}`}
      data-delivery={message.deliveryStatus ?? 'sent'}
    >
      <div className="ccui-user-bubble">
        {message.attachments?.length ? (
          <div className="ccui-attachment-strip">
            {message.attachments.map((file) => <span className="ccui-attachment-chip" key={file.id} title={file.name}><Paperclip size={12} aria-hidden /><span>{file.name}</span></span>)}
          </div>
        ) : null}
        <div className="ccui-user-text">{message.text}</div>
      </div>
      <div className="ccui-user-footer">
        {message.deliveryStatus === 'failed' ? <span className="ccui-delivery-failed">没有送达</span> : null}
        {message.steerReceipt ? (
          <SteerReceipt
            canInterrupt={message.steerReceipt === 'unread' && capabilities.interrupt}
            clock={surface.formatTimestamp}
            state={message.steerReceipt}
            timestamp={message.timestamp}
            {...(surface.interruptSteer ? { onInterrupt: () => surface.interruptSteer?.(message) } : {})}
            {...(surface.cancelSteer ? { onCancelAndEdit: () => surface.cancelSteer?.(message) } : {})}
          />
        ) : (
          <time dateTime={new Date(message.timestamp).toISOString()}>{surface.formatTimestamp(message.timestamp)}</time>
        )}
        <MessageActions
          text={message.text}
          {...(capabilities.copy ? { onCopy: () => undefined } : {})}
          {...(capabilities.edit && surface.edit ? { onEdit: () => surface.edit?.(message) } : {})}
          {...(capabilities.fork && surface.fork ? { onFork: () => surface.fork?.(message) } : {})}
          {...(capabilities.rewind && surface.rewind ? { onRewind: () => surface.rewind?.(message) } : {})}
        />
      </div>
    </article>
  );
}
