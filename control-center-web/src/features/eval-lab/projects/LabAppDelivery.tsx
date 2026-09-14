import { Download, ExternalLink, PackageCheck } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Field, Input } from '@/components/primitives';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { PAW_EXTENSION_INSTALLATION_CHANGED_EVENT } from '@/paw-os/extensions/installation';
import { commandLabApp, downloadLabApp, pendingLabAppCommands, useLabApps, type LabAppCommand, type LabEvaluationSelection } from './apps';
import { LabAppPreview } from './LabAppPreview';
import { LabAppConfiguration } from './LabAppConfiguration';
import { evaluationSelectionKey, LabEvaluationSelectionReview } from './LabEvaluationSelectionReview';
import { projectCommandRejected, projectError } from './api';

type Props = { projectId: string; initialAppId?: string; initialVersion?: number; initialCallId?: string; preparing?: boolean; blocked?: boolean; evaluationSelection?: LabEvaluationSelection; onClearSelection?: () => void; onGuide?: () => void; onPrepare?: (directory: string, appId?: string, selection?: LabEvaluationSelection) => Promise<boolean>; onOpenEvaluation?: (suiteId: string, jobId: string) => void };
export function LabAppDelivery(props: Props) { return <ProjectAppDelivery key={props.projectId} {...props} />; }
function ProjectAppDelivery({ projectId, initialAppId = '', initialVersion, initialCallId = '', preparing = false, blocked = false, evaluationSelection, onClearSelection, onPrepare, onGuide, onOpenEvaluation }: Props) {
  const desktop = usePawOsDesktop();
  const transport = useControlTransport(); const catalog = useLabApps(projectId); const [selectedId, setSelectedId] = useState(initialAppId);
  const appId = selectedId || catalog.data?.items[0]?.appId || ''; const [selectedVersion, setSelectedVersion] = useState<number | undefined>(initialVersion);
  const requestedVersion = selectedVersion ?? catalog.data?.items.find((item) => item.appId === appId)?.latestVersion;
  const [closedCallId, setClosedCallId] = useState('');
  const selectedCallId = initialCallId !== closedCallId && appId === initialAppId && requestedVersion === initialVersion ? initialCallId : '';
  const query = useLabApps(projectId, appId, requestedVersion, selectedCallId); const app = query.data?.app; const version = query.data?.version;
  useEffect(() => { if (initialAppId) { setSelectedId(initialAppId); setSelectedVersion(initialVersion); } }, [initialAppId, initialVersion, initialCallId]);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [pending, setPending] = useState<LabAppCommand>();
  const [activity, setActivity] = useState(''); const [notice, setNotice] = useState('');
  const [directory, setDirectory] = useState('app');
  const [verifiedSelection, setVerifiedSelection] = useState('');
  const selectionReady = !evaluationSelection || verifiedSelection === evaluationSelectionKey(evaluationSelection);
  const prepareDisabled = busy || preparing || blocked || Boolean(pending) || !directory.trim() || !selectionReady;
  useEffect(() => { setPending(pendingLabAppCommands(transport, appId).find((command) => ['activate', 'deactivate'].includes(command.action))); }, [transport, appId]);
  useEffect(() => { if (requestedVersion && selectedVersion === undefined) setSelectedVersion(requestedVersion); }, [requestedVersion, selectedVersion]);
  const act = async (command: LabAppCommand) => {
    setBusy(true); setError(''); setNotice(''); setActivity(command.action);
    try {
      await commandLabApp(transport, command); setPending(undefined); await Promise.all([query.refetch(), catalog.refetch()]);
      window.dispatchEvent(new Event(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT));
      setNotice(command.action === 'activate' ? '此版本已添加至 PAW，可以打开使用。' : '已从 PAW 停用，版本和记录仍保留。');
    } catch (reason) {
      const definite = projectCommandRejected(reason); setPending(definite ? undefined : command); setError(projectError(reason));
      if (definite) void Promise.all([query.refetch(), catalog.refetch()]);
    }
    finally { setBusy(false); setActivity(''); }
  };
  const download = async (target: 'paw' | 'standalone') => {
    if (!app || !version) return; setBusy(true); setError(''); setNotice(''); setActivity(target);
    try { await downloadLabApp(transport, app.appId, version.version, target); setNotice(`v${version.version} 下载已发起。${target === 'standalone' ? '解压后按 README 启动并配置模型服务。' : '可将应用包导入 PAW。'}`); }
    catch (reason) { setError(projectError(reason)); }
    finally { setBusy(false); setActivity(''); }
  };
  return <section className="lab-app-delivery" aria-label="项目应用交付">
    <header><div><small>应用交付</small><h2>{version?.spec.title ?? app?.title ?? '把成果变成可用的应用'}</h2></div>
      {app && version ? <select aria-label="应用版本" disabled={busy} value={version.version} onChange={(event) => { setSelectedVersion(Number(event.target.value)); setNotice(''); setError(''); }}>
        {query.data?.versions?.map((item) => <option key={item.version} value={item.version}>v{item.version}{app.activeVersion === item.version ? ' · PAW 当前版本' : ''}</option>)}
      </select> : null}</header>
    {catalog.isError || query.isError ? <div className="lab-project-error" role="alert"><p>{projectError(catalog.error ?? query.error)}</p><Button onClick={() => void Promise.all([catalog.refetch(), query.refetch()])}>重新读取应用</Button></div> : null}
    {error || pending ? <div className="lab-project-error" role="alert"><p>{error || '应用操作尚未确认，已保留原请求。'}</p>{pending ? <Button onClick={() => void act(pending)} disabled={busy}>核对原操作</Button> : null}</div> : null}
    {notice ? <p className="lab-app-delivery__notice" role="status">{notice}</p> : null}
    {evaluationSelection ? <LabEvaluationSelectionReview selection={evaluationSelection} onVerified={setVerifiedSelection} onClear={onClearSelection} /> : null}
    {onPrepare ? <details className="lab-app-delivery__sources" open={Boolean(evaluationSelection) || preparing || undefined}><summary>从执行目录准备应用</summary>
      <form onSubmit={(event) => { event.preventDefault(); if (prepareDisabled) return; void (async () => {
        if (await onPrepare(directory, appId || undefined, evaluationSelection)) {
          const [fresh] = await Promise.all([catalog.refetch(), query.refetch()]);
          const prepared = appId ? fresh.data?.items.find((item) => item.appId === appId) : fresh.data?.items[0];
          if (prepared) { setSelectedId(prepared.appId); setSelectedVersion(prepared.latestVersion); }
        }
      })(); }}><p>填写项目执行目录中的应用子目录，里面应有 app.json、界面和方法文件。准备后可试用并导出；已有应用会增加一个版本。</p>
        <Field label="应用源目录" htmlFor="lab-app-source-directory"><Input id="lab-app-source-directory" value={directory} onChange={(event) => setDirectory(event.target.value)} required /></Field>
        <Button type="submit" disabled={prepareDisabled}>{preparing ? '正在准备版本…' : '准备应用版本'}</Button>
        {preparing ? <p role="status">正在冻结资料并准备应用版本，完成后即可继续。请勿重复提交。</p> : null}
      </form></details> : null}
    {catalog.data?.items.length ? <nav aria-label="项目应用">{catalog.data.items.map((item) => <button key={item.appId} disabled={busy} aria-current={item.appId === appId ? 'page' : undefined} onClick={() => { setSelectedId(item.appId); setSelectedVersion(undefined); setNotice(''); setError(''); }}>{item.title}</button>)}</nav> : null}
    {!catalog.isPending && !catalog.isError && !catalog.data?.items.length ? <div className="lab-project-empty"><PackageCheck size={28} /><h3>还没有准备好的应用版本</h3><p>让项目 Agent 把当前方案实现为应用。准备好后，先在这里试用，再选择添加至 PAW 或下载独立 App。</p>{onGuide ? <Button variant="primary" disabled={preparing || blocked} onClick={onGuide}>让 Agent 准备应用</Button> : null}</div>
      : app && version ? <><div className="lab-app-delivery__toolbar">
        <span>{version.fileCount} 份冻结源文件 · {(version.byteSize / 1024).toFixed(1)} KB · {version.spec.model.model}</span>
        <Button disabled={busy || Boolean(pending) || app.activeVersion === version.version} onClick={() => void act({ action: 'activate', appId, expectedRevision: app.revision, clientRequestId: `app-activate:${crypto.randomUUID()}`, input: { version: version.version } })}><PackageCheck size={15} />{activity === 'activate' ? '正在添加…' : app.activeVersion === version.version ? '已添加至 PAW' : app.activeVersion ? '将此版本用于 PAW' : '添加至 PAW'}</Button>
        {app.activeVersion === version.version ? <Button onClick={() => openPawOsRoute(desktop, `/extensions/${appId.slice('extension:'.length)}`)}><ExternalLink size={15} />打开 App</Button> : null}
        <Button disabled={busy} onClick={() => void download('standalone')}><Download size={15} />{activity === 'standalone' ? '正在下载独立 App…' : '下载独立 App'}</Button>
        <Button disabled={busy} onClick={() => void download('paw')}><Download size={15} />{activity === 'paw' ? '正在下载 PAW 应用…' : '下载 PAW 应用包'}</Button>
      </div><p className="lab-app-delivery__hint">{selectedCallId ? `正在阅读 v${version.version} 的原调用记录` : `正在试用 v${version.version}`} · 切换版本会打开对应实现，旧版本和运行记录会保留。</p>
      {version.spec.knowledge ? <p className="lab-app-delivery__boundary">已包含 {version.spec.knowledge.documentCount.toLocaleString()} 篇文档，可先查询来源，再生成带依据的回答。</p> : null}
      {version.spec.externalWorkspace ? <p className="lab-app-delivery__boundary">连接工作台：{version.spec.externalWorkspace.title}。工作台服务需要单独启动，未随包复制；资料问答不会自动执行工作台任务。</p> : null}
      <details className="lab-app-delivery__requirements"><summary>独立 App 的运行方式与模型配置</summary><p>独立 App 使用 Python 启动。{['dense', 'hybrid'].includes(version.spec.knowledge?.profile.mode ?? '') ? '此版本使用语义或混合检索，首次使用需按 README 执行一次 setup，安装本地检索组件，之后按常规步骤启动。' : '解压后按 README 启动。'}生成回答需配置目标模型服务或连接 PAW；应用包不携带登录凭据。</p></details>
      <LabAppConfiguration version={version} onOpenEvaluation={onOpenEvaluation} />
      {selectedCallId ? <Button size="small" onClick={() => setClosedCallId(selectedCallId)}>返回应用试用</Button> : null}
      <LabAppPreview key={`${appId}:${version.version}`} app={app} version={version} calls={query.data?.call ? [query.data.call, ...(query.data.calls ?? []).filter((call) => call.callId !== query.data!.call!.callId)] : query.data?.calls ?? []} selectedCallId={selectedCallId} onActivity={() => void query.refetch()} />
      <details className="lab-app-delivery__sources"><summary>版本内容与来源</summary><p>源内容指纹：{version.contentHash}</p><ul>{version.sourceFiles.map((file) => <li key={file.path}>{file.path} · {file.byteSize} bytes</li>)}</ul>
        {app.activeVersion ? <Button size="small" disabled={busy || Boolean(pending)} onClick={() => void act({ action: 'deactivate', appId, expectedRevision: app.revision, clientRequestId: `app-disable:${crypto.randomUUID()}`, input: {} })}>从 PAW 停用，保留版本和记录</Button> : null}</details></>
        : !catalog.isError && !query.isError ? <p role="status">正在读取应用版本…</p> : null}
  </section>;
}
