import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { TooltipProvider } from '../../src/components/primitives';
import { ToolPicker } from '../../src/features/agent/composer/ToolPicker';
import { ToolCard } from '../../src/features/conversation-ui/components/ToolCard';
import { ImageGallery } from '../../src/features/conversation-ui/media/ImageGallery';
import { ComposerShell } from '../../src/features/composer/ComposerShell';
import type { ComposerShellAttachment } from '../../src/features/composer/ComposerShell';
import type { CapabilityPreference } from '../../src/features/plugins/capability-policy';
import { updateReadingPreferences } from '../../src/features/conversation-ui/reading/reading-preferences';
import { demoCatalog, demoImages, demoTools } from './pi-content-v5-data';
import '../../src/design/tokens.css';
import '../../src/design/typography.css';
import '../../src/components/primitives/primitives.css';
import '../../src/features/conversation-ui/conversation-ui.css';
import './pi-content-v5.css';

/** Real React/Popover/Dialog/gallery/composer wrappers, synthetic data only. No Gateway is contacted. */
function Fixture() {
  const [owner, setOwner] = useState('fixture-earth');
  const [preferences, setPreferences] = useState<Record<string, Record<string, CapabilityPreference>>>({});
  const [dark, setDark] = useState(false); const [reduced, setReduced] = useState(false);
  const [running, setRunning] = useState(false); const [draft, setDraft] = useState('');
  const [attachments, setAttachments] = useState<ComposerShellAttachment[]>([]);
  const [note, setNote] = useState('');
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; }, [dark]);
  useEffect(() => { updateReadingPreferences({ motion: reduced ? 'reduced' : 'system' }); }, [reduced]);
  const catalog = demoCatalog(owner, preferences[owner] || {});
  return <TooltipProvider><div className="pi-v5-fixture"><header><strong>Pi 内容组件 · 合成输入 · 不连接模型</strong>
    <select aria-label="选择伙伴" value={owner} onChange={event => setOwner(event.currentTarget.value)}><option value="fixture-earth">Earth</option><option value="fixture-mars">Mars</option></select>
    <button onClick={() => setDark(value => !value)}>浅色 / 深色</button><button aria-pressed={reduced} onClick={() => setReduced(value => !value)}>减少动态</button><button aria-pressed={running} onClick={() => setRunning(value => !value)}>切换工具执行状态</button></header>
    <main><h1>图片与当前伙伴的功能</h1><p>这是生产组件的工程内入口。目录和图片为合成输入，设置不会写入真实 Session。</p>
      <ToolCard block={{ id: 'fixture-pi-read', kind: 'tool', name: 'read', status: running ? 'running' : 'success', summary: '本轮图片读取 · 示例', input: '{"path":"design/screenshots/"}', output: running ? '读取进行中（合成片段）。' : '3 张图片返回（合成回执）。' }} />
      <ImageGallery items={demoImages()} />
      <ComposerShell surface="session" attachments={attachments} onRemoveAttachment={id => setAttachments(items => items.filter(item => item.id !== id))}
        textarea={<textarea aria-label="组件样例草稿" value={draft} onChange={event => setDraft(event.currentTarget.value)} placeholder="只保留本地草稿，不发送" />}
        controls={<><ToolPicker tools={demoTools()} capabilityCatalog={catalog} sessionId={owner} status="ready" adjustmentDisabled={running} capabilityPolicyPending={false} disabled={false} requestOpen={0}
          onSelect={tool => setDraft(value => `${value}${value ? '\n' : ''}${tool.displayName}：`)}
          onCapabilityPreferenceChange={(key, preference) => setPreferences(previous => ({ ...previous, [owner]: { ...previous[owner], [key]: preference } }))} />
          <label className="pi-v5-fixture-pick">添加本地图片<input type="file" accept="image/*" multiple onChange={event => {
            const files = Array.from(event.currentTarget.files ?? []);
            setAttachments(items => [...items, ...files.filter(file => file.type.startsWith('image/')).slice(0, Math.max(0, 8 - items.length)).map((file, index) => ({ id: `local-${Date.now()}-${index}`, name: file.name, mimeType: file.type, byteSize: file.size, previewFile: file }))]);
            event.currentTarget.value = '';
          }} /></label></>}
        actions={<button type="button" onClick={() => setNote('本入口仅预览组件，没有发送消息或调用模型。')}>保留草稿</button>} />
      {note ? <p role="status">{note}</p> : null}
    </main></div></TooltipProvider>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
