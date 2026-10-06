import type { WorkspaceComposerHeaderView } from './PawSessionWorkspace';
import './primary-assistant.css';

export function PrimaryAssistantComposerHeader({ view, onHome }: {
  view: WorkspaceComposerHeaderView;
  onHome: (draft?: string, execute?: boolean, sourceMessageId?: string) => void;
}) {
  const primary = view.session.metadata?.primaryAssistant === true;
  if (!primary && view.session.metadata?.primaryTask !== true) return null;
  return <div className="paw-primary-session-context">
    {primary ? <strong>我的助手 · 长期对话</strong> : <details><summary>执行范围 · {view.session.workspaceRoots.length} 个目录</summary><p>{view.session.workspaceRoots.join('、') || '读取中'}</p></details>}
    <button disabled={view.disabled} onClick={() => onHome(primary ? view.draft : undefined, primary, primary ? view.sourceMessageId() : undefined)} type="button">
      {primary ? '交给助手做' : '返回我的助手'}
    </button>
  </div>;
}
