import type { WorkspaceComposerHeaderView } from './PawSessionWorkspace';
import './primary-assistant.css';

export function PrimaryAssistantComposerHeader({ view, onHome }: {
  view: WorkspaceComposerHeaderView;
  onHome: (draft?: string, execute?: boolean, sourceMessageId?: string) => void;
}) {
  const primary = view.session.metadata?.primaryAssistant === true;
  if (!primary && view.session.metadata?.primaryTask !== true) return null;
  return <div className="paw-primary-session-context"><span>
    <strong>{primary ? '我的助手 · 长期对话' : '当前工作'}</strong>
    <small>{primary ? '聊一聊、查资料；需要执行时，明确交给助手。' : `执行范围：${view.session.workspaceRoots.join('、') || '读取中'} · 过程与结果保留在这里`}</small>
  </span><button disabled={view.disabled} onClick={() => onHome(primary ? view.draft : undefined, primary, primary ? view.sourceMessageId() : undefined)} type="button">
    {primary ? '交给助手做' : '返回我的助手'}
  </button></div>;
}
