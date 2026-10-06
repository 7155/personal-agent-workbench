import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Code2, MessageCircle, PackageCheck, Sparkles } from 'lucide-react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Input, SegmentedControl, Switch } from '@/components/primitives';
import { asRecord, publicErrorText, stringValue } from '@/features/overview/management-ui';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import './plugin-studio.css';

export function PluginStudio({ onPreview, onPrepare, onPendingChange, disabled = false }: {
  onPreview(preview: Record<string, unknown>): void;
  onPrepare?(): void;
  onPendingChange?(pending: boolean): void;
  disabled?: boolean;
}) {
  const transport = useControlTransport();
  const navigate = useNavigate();
  const desktop = usePawOsDesktop();
  const [method, setMethod] = useState<'agent' | 'manual'>('agent');
  const [kind, setKind] = useState<'app' | 'plugin'>('app');
  const [resource, setResource] = useState<'skills' | 'prompts'>('skills');
  const [name, setName] = useState('');
  const [purpose, setPurpose] = useState('');
  const [body, setBody] = useState('');
  const [version, setVersion] = useState('0.1.0');
  const [install, setInstall] = useState(true);
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const [error, setError] = useState('');
  const [draftPath, setDraftPath] = useState('');
  const retained = useRef<{ fingerprint: string; sourcePath: string } | undefined>(undefined);
  const openAuthoring = () => {
    const request = [
      `/skill:${kind === 'app' ? 'pawos-app-builder' : 'plugin-creator'}`,
      `请在 PAWOS 内制作${kind === 'app' ? '一个 App' : '一个插件'}：${name.trim()}。`,
      `用途：${purpose.trim()}`,
      body.trim() ? `验收要求：${body.trim()}` : '',
      '先检查已有能力，复用合适的实现；需要制作时创建完整候选、测试并核对实际入口。',
      install
        ? '我授权安装本次按上述用途制作且验证通过的候选。先核对来源、权限和版本，再通过 plugins 的绑定预览与 apply 完成安装；已有授权不要重复要求我跳转确认。出现超出本次用途的权限或发布变更时再说明具体差异。'
        : '本次只制作和验证候选，先不要安装。',
      '保留现有对话、用户数据和可回滚版本；只有真实回执才能报告安装成功。不要发布到外部市场。',
    ].filter(Boolean).join('\n\n');
    const route = `/agent?${new URLSearchParams({ draft: request })}`;
    if (desktop) openPawOsRoute(desktop, route); else navigate(route);
  };
  const prepare = async () => {
    if (inFlight.current || disabled) return;
    onPrepare?.();
    if (!/^[a-z][a-z0-9-]{1,63}$/.test(name.trim()) || !/^\d+\.\d+\.\d+$/.test(version.trim())) {
      setError('插件标识使用小写字母、数字和连字符；版本填写如 0.1.0。'); return;
    }
    inFlight.current = true; setPending(true); setError('');
    onPendingChange?.(true);
    const id = name.trim();
    const fingerprint = JSON.stringify([id, version.trim(), resource, purpose.trim(), body]);
    try {
      let sourcePath = retained.current?.fingerprint === fingerprint ? retained.current.sourcePath : '';
      if (!sourcePath) {
        const filePath = resource === 'skills' ? `skills/${id}/SKILL.md` : `prompts/${id}.md`;
        const header = resource === 'skills'
          ? `---\nname: ${id}\ndescription: ${JSON.stringify(purpose.trim())}\n---\n\n`
          : `---\ndescription: ${JSON.stringify(purpose.trim())}\n---\n\n`;
        const response = asRecord(await transport.request({ pathId: 'agent.extensions.create', body: {
          draftId: `studio-${crypto.randomUUID()}`,
          packageJson: { name: `@paw-local/${id}`, version: version.trim(), description: purpose.trim(), keywords: ['pi-package'], pi: { [resource]: [`./${resource}`] } },
          files: { [filePath]: `${header}${body.trim()}\n` },
        } }));
        sourcePath = stringValue(asRecord(response.draft).sourcePath);
        if (response.ok !== true || !sourcePath) throw new Error('草稿未返回有效的保存位置。');
        retained.current = { fingerprint, sourcePath }; setDraftPath(sourcePath);
      }
      const validation = asRecord(await transport.request({ pathId: 'agent.extensions.validate', body: { packageSource: sourcePath } }));
      if (validation.ok !== true || !stringValue(validation.validationToken)) throw new Error('草稿检查未完成。');
      const preview = asRecord(await transport.request({ pathId: 'agent.extensions.preview', body: {
        action: 'install', validationToken: stringValue(validation.validationToken), enable: true,
      } }));
      if (preview.ok !== true || !stringValue(preview.previewToken) || !stringValue(preview.payloadSha256)) throw new Error('安装预览未完成。');
      onPreview(preview);
    } catch (reason) {
      setError(publicErrorText(reason, '草稿尚未完成安装检查，内容已保留，请重试。'));
    } finally { inFlight.current = false; setPending(false); onPendingChange?.(false); }
  };
  return <section aria-label="制作 App 与插件" className="plugin-studio">
    <header className="plugin-studio__intro"><Sparkles size={24} /><span><h2>把需要的能力，做进 OS</h2><p>让 Agent 制作完整应用，或自己编写可重复使用的技能和提示词。</p></span></header>
    <SegmentedControl<'agent' | 'manual'> aria-label="制作方式" value={method} onValueChange={setMethod} items={[{ value: 'agent', label: '让 Agent 制作' }, { value: 'manual', label: '自己编写' }]} />
    <form className="plugin-studio__form" onSubmit={(event) => { event.preventDefault(); if (method === 'agent') openAuthoring(); else void prepare(); }}>
      {method === 'agent' ? <SegmentedControl<'app' | 'plugin'> aria-label="制作对象" value={kind} onValueChange={setKind} items={[{ value: 'app', label: 'App 应用' }, { value: 'plugin', label: '插件' }]} />
        : <SegmentedControl<'skills' | 'prompts'> aria-label="插件资源" value={resource} onValueChange={setResource} items={[{ value: 'skills', label: 'Skill 技能' }, { value: 'prompts', label: 'Prompt 提示词' }]} />}
      <div className="plugin-studio__identity">
        <label><span>{method === 'agent' ? '名称' : '插件标识'}</span><Input required maxLength={64} value={name} onChange={(e) => { setName(e.target.value); setDraftPath(''); }} placeholder={method === 'agent' ? '例如：资料阅读' : '例如：review-notes'} /></label>
        {method === 'manual' ? <label><span>版本</span><Input required value={version} onChange={(e) => setVersion(e.target.value)} /></label> : null}
      </div>
      <label><span>用途</span><Input required maxLength={400} value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder="它帮助你完成什么？" /></label>
      <label><span>{method === 'agent' ? '验收要求' : '内容'}</span><textarea required={method === 'manual'} maxLength={60000} value={body} onChange={(e) => setBody(e.target.value)} rows={8} placeholder={method === 'agent' ? '描述输入、操作方式和你希望看到的结果。' : '写下这个技能的触发条件、步骤与预期结果。'} /></label>
      {method === 'agent' ? <Switch label="验证通过后安装" description="授权 Agent 完成本次候选的安装；当前对话和数据会保留。" checked={install} onCheckedChange={setInstall} /> : null}
      {error ? <p role="alert" className="plugin-studio__error">{error}</p> : null}
      {method === 'manual' && draftPath ? <p role="status" className="plugin-studio__draft"><Code2 size={14} />草稿已保存 <code>{draftPath}</code></p> : null}
      <footer><span>{method === 'agent' ? '打开制作对话，发送需求后开始。' : '先保存并校验，随后在这里查看安装内容。'}</span>
        <Button type="submit" disabled={disabled} loading={pending} leadingIcon={method === 'agent' ? <MessageCircle size={16} /> : <PackageCheck size={16} />}>{method === 'agent' ? '在 Agent 中制作' : '保存草稿并检查安装'}</Button>
      </footer>
    </form>
  </section>;
}
