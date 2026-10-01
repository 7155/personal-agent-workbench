import { MarkdownBody } from '@/features/agent/timeline/MarkdownRenderer';
import { Play } from 'lucide-react';
import { Fragment } from 'react';
import { useConversationSurface } from '../ConversationSurfaceContext';
import type { AssistantMessage } from '../model/types';
import { MessageActions } from './MessageActions';
import { ThinkingBlock } from './ThinkingBlock';
import { ToolCard } from './ToolCard';

export function AssistantTurn({ message }: { message: AssistantMessage }) {
  const surface = useConversationSurface();
  const { capabilities } = surface;
  const streaming = message.blocks.some((block) => block.kind === 'text' && block.streaming);
  const copyText = message.blocks
    .map((block) => block.kind === 'text' ? block.text : '')
    .filter(Boolean)
    .join('\n\n');
  const retryable = Boolean(
    capabilities.retry && surface.retry && (surface.canRetry?.(message) ?? true),
  );
  const footer = surface.renderMessageFooter?.(message);
  return (
    <article className="ccui-turn ccui-assistant-turn" data-turn-id={message.turnId}>
      {message.actor ? (
        <header className="ccui-assistant-head">
          {surface.renderMessageIdentity?.(message) ?? <>
            {surface.renderMessageAvatar?.(message)}
            <strong>{message.actor}</strong>
          </>}
          {message.actorRole ? <small>{message.actorRole}</small> : null}
          <time dateTime={new Date(message.timestamp).toISOString()}>{surface.formatTimestamp(message.timestamp)}</time>
        </header>
      ) : null}
      <div className="ccui-assistant-body">
        {message.blocks.map((block) => {
          const blockFooter = surface.renderBlockFooter?.(block, message);
          const override = surface.renderBlock?.(block, message);
          if (override === null) return blockFooter ? <Fragment key={block.id}><div className="ccui-assistant-host-footer">{blockFooter}</div></Fragment> : null;
          if (override !== undefined) return <Fragment key={block.id}><div className="ccui-host-block">{override}</div>{blockFooter ? <div className="ccui-assistant-host-footer">{blockFooter}</div> : null}</Fragment>;
          const detail = surface.renderBlockDetail?.(block, message);
          if (block.kind === 'thinking') {
            return <Fragment key={block.id}><ThinkingBlock block={block} {...(detail ? { detail } : {})} />{blockFooter ? <div className="ccui-assistant-host-footer">{blockFooter}</div> : null}</Fragment>;
          }
          if (block.kind === 'tool') {
            const action = surface.renderBlockAction?.(block, message);
            return <Fragment key={block.id}><ToolCard
              block={block}
              {...(action ? { action } : {})}
              {...(detail ? { detail } : {})}
            />{blockFooter ? <div className="ccui-assistant-host-footer">{blockFooter}</div> : null}</Fragment>;
          }
          return <Fragment key={block.id}><div className="ccui-markdown">
              <MarkdownBody
                documentKey={`${message.id}:${block.id}`}
                sessionId={surface.resolveMessageSessionId?.(message) ?? ''}
                streamingTail={Boolean(block.streaming)}
                text={block.text}
              />
            </div>{blockFooter ? <div className="ccui-assistant-host-footer">{blockFooter}</div> : null}</Fragment>;
        })}
        {streaming ? <small className="ccui-live-hint">正在生成公开回复</small> : null}
        {message.error ? <div className="ccui-error-card" role="status">{message.error}</div> : null}
      </div>
      {footer ? <div className="ccui-assistant-host-footer">{footer}</div> : null}
      <div className="ccui-assistant-footer">
        <MessageActions
          retryLabel="继续"
          retryIcon={<Play aria-hidden="true" size={12} />}
          retryPending={Boolean(surface.retryPending)}
          text={copyText}
          {...(capabilities.copy && copyText ? { onCopy: () => undefined } : {})}
          {...(retryable ? { onRetry: () => surface.retry?.(message) } : {})}
          {...(capabilities.fork && surface.fork ? { onFork: () => surface.fork?.(message) } : {})}
          {...(capabilities.rewind && surface.rewind ? { onRewind: () => surface.rewind?.(message) } : {})}
        />
      </div>
    </article>
  );
}
