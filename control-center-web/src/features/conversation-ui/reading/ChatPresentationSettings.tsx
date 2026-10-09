import { useChatPresentation } from './chat-presentation';
import './chat-presentation-settings.css';

/** Embedded in the existing reading/view menu, never a second chat owner. */
export function ChatPresentationSettings() {
  const presentation = useChatPresentation();
  if (!presentation) return null;
  return <fieldset className="paw-chat-presentation-settings"><legend>聊天显示版本</legend><div className="paw-chat-presentation-settings__choices">
    {presentation.availableVersions.map(version => <button key={version} type="button"
      className="paw-chat-presentation-settings__button"
      aria-pressed={presentation.version === version} onClick={() => presentation.setVersion(version)}>
      {version === 'v1' ? '经典 v1' : '新版 v2'}
    </button>)}
  </div>
    {presentation.previousVersion ? <button className="paw-chat-presentation-settings__button" type="button" onClick={() => presentation.rollback()}>
      恢复上一显示版本 {presentation.previousVersion}
    </button> : null}
    <p>{presentation.scope === 'app' ? '只更换此 App 的聊天显示。' : '只更换当前会话的聊天显示。'}当前输入与对话保留。</p>
  </fieldset>;
}
