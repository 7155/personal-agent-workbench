import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import ReactMarkdown from 'react-markdown';
import { useControlTransport } from '@/app/control-transport';
import type { JsonValue } from '@/platform/transport';

type Note = { id: string; title: string; path: string; revision: string };
type Proposal = { id: string; revision: number; path: string; reason: string; state: string; diff: string; conflict: boolean; readable: boolean };
type Day = { modelDrafts?: {id:string;markdown:string;sourcesCurrent:boolean;generator:string}[]; activityReport?: {markdown:string}; activityUnavailable?: boolean; markdown: string; personalDiary: { markdown?: string } | null; sources: unknown[] };
type Policy = { captureFolder:string;captureProject:string; inbox: string; personalDiary: string; remoteProcessing: boolean; jevEnabled: boolean };
const labels: Record<string, string> = { prepared: '待审核', waiting_editor: '等待编辑器', applying: '等待写入回执', saved_index_pending: '正文已保存 · 索引待更新', saved: '原文已保存', dismissed: '已忽略' };

export function VaultActivity({ vaultId, notes, view }: { vaultId: string; notes: Note[]; view: 'day' | 'review' | 'settings' }) {
  const transport = useControlTransport(), client = useQueryClient();
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [captureFolder,setCaptureFolder]=useState('');
  const [includeActivity,setIncludeActivity]=useState(false);
  const [project, setProject] = useState(''), [date, setDate] = useState(() => new Date().toLocaleDateString('en-CA'));
  const [idea, setIdea] = useState(''), [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [source, setSource] = useState(''), [target, setTarget] = useState(''), [before, setBefore] = useState(''), [after, setAfter] = useState(''), [reason, setReason] = useState('');
  const [statement, setStatement] = useState('');
  const [inbox, setInbox] = useState('收件箱'), [personalDiary, setPersonalDiary] = useState('');
  const [pairing, setPairing] = useState<{ vaultId: string; pairingToken: string } | null>(null);
  const [draft, setDraft] = useState(''), [pack, setPack] = useState(''), [forgetPreview, setForgetPreview] = useState(false);
  const call = async <T,>(body: Record<string, JsonValue>) => await transport.request({ pathId: 'knowledgeVault.manage', body: { ...body, vaultId }, timeoutMs: body.action === 'organize' ? 180_000 : 60_000 }) as T;
  const policy = useQuery({ queryKey: ['vault-policy', vaultId], queryFn: () => call<{ policy: Policy }>({ action: 'settings' }) });
  const dayArgs = { includeActivity, date, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, project };
  const day = useQuery({ queryKey: ['vault-day', vaultId, date, project, includeActivity], queryFn: () => call<Day>({ action: 'day', ...dayArgs }), enabled: view === 'day', refetchInterval: view === 'day' ? 15000 : false });
  const adoptions = useQuery({ queryKey: ['vault-adoptions', vaultId], queryFn: () => call<{items:{id:string;statement:string;state:string}[]}>({action:'adoptions'}), enabled:view==='review', refetchInterval:view==='review'?15000:false });
  const proposals = useQuery({ queryKey: ['vault-proposals', vaultId], queryFn: () => call<{ items: Proposal[] }>({ action: 'proposals' }), enabled: view === 'review', refetchInterval: view === 'review' ? 10_000 : false });
  async function run(work: () => Promise<void>) {
    if (busy) return; setBusy(true); setError(''); setNotice('');
    try { await work(); } catch (e) { setError(e instanceof Error ? e.message : '操作未完成，请重试。'); } finally { setBusy(false); }
  }
  async function refresh() { await Promise.all(['vault-day', 'vault-proposals', 'knowledge-vault', 'vault-adoptions'].map(key => client.invalidateQueries({ queryKey: [key, vaultId] }))); }
  function refs() { const n = notes.find(n => n.id === source); if (!n) throw new Error('请选择原始材料。'); return [{ noteId: n.id, revision: n.revision }]; }
  async function configure(values: Record<string, JsonValue>) { await call({ action: 'configure', ...values }); await policy.refetch(); setNotice('设置已保存。'); }
  const select = (value: string, set: (v: string) => void, placeholder: string) => <select value={value} onChange={e => set(e.target.value)}><option value="">{placeholder}</option>{notes.map(n => <option key={n.id} value={n.id}>{n.title}</option>)}</select>;
  return <section className="vault-activity" aria-label={view === 'day' ? '日期页' : view === 'review' ? '笔记修订' : '笔记设置'}>
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    {(policy.error || day.error || proposals.error) ? <p role="alert">{String(policy.error || day.error || proposals.error)}</p> : null}
    {view !== 'settings' ? <label className="vault-project">项目归属 <input placeholder="留空表示待归属" value={project} onChange={e => setProject(e.target.value)} /></label> : null}
    {view === 'day' ? <>
      <header><h3>今天留下了什么</h3><input aria-label="日期" type="date" value={date} onChange={e => setDate(e.target.value)} /></header>
      <form onSubmit={e => { e.preventDefault(); void run(async () => { await call({ action: 'save', markdown: idea, requestId, project }); setIdea(''); setRequestId(crypto.randomUUID()); await refresh(); setNotice('已保存到 Markdown 收件箱。'); }); }}>
        <label>记下一点想法<textarea rows={4} value={idea} onChange={e => { setIdea(e.target.value); setRequestId(crypto.randomUUID()); }} placeholder="一个问题、一个新理解，或者值得留住的结果…" /></label>
        <button disabled={busy || !idea.trim() || !policy.data?.policy.inbox}>保存到收件箱</button>{!policy.data?.policy.inbox ? <p>先到“设置”指定允许创建笔记的收件箱。</p> : null}
      </form>
      <label><input type="checkbox" checked={includeActivity} disabled={!project.trim()} onChange={e=>setIncludeActivity(e.target.checked)}/>同时读取此项目已准入的 PAW / 输入法活动</label><section><h3>工作回顾</h3>{day.isPending ? <p role="status">正在读取…</p> : <ReactMarkdown components={{img:({alt})=><span>附件：{alt || "图片"}（请在原笔记查看）</span>}}>{day.data?.markdown ?? ''}</ReactMarkdown>}
        <button disabled={busy || !day.data?.sources.length || !policy.data?.policy.inbox} onClick={() => void run(async () => { const r = await call<{ path: string }>({ action: 'export_day', ...dayArgs }); setNotice(`已另存：${r.path}。后续编辑不会被覆盖。`); })}>另存工作回顾</button>
      </section><section><h3>已获准的项目活动</h3>{day.data?.activityReport?<ReactMarkdown components={{img:({alt})=><span>{alt}</span>}}>{day.data.activityReport.markdown}</ReactMarkdown>:<p>开启后读取现有 Memory 准入链中的来源，不开启被动采集、不补写未取得的对话。</p>}</section><section><h3>已保存的模型回顾</h3>{day.data?.modelDrafts?.map(d=><article key={d.id}><p>机器草稿 · {d.generator} · {d.sourcesCurrent?'来源版本一致':'来源已变化，请重新整理'}</p><ReactMarkdown components={{img:({alt})=><span>{alt}</span>}}>{d.markdown}</ReactMarkdown><button disabled={busy||!d.sourcesCurrent} onClick={()=>void run(async()=>{const r=await call<{path:string}>({action:'export_model_diary',diaryId:d.id});setNotice(`已另存 ${r.path}，不会覆盖本人日记。`);})}>另存此回顾</button></article>)}</section><section><h3>我的日记</h3>{day.data?.personalDiary?.markdown ? <ReactMarkdown components={{img:({alt})=><span>附件：{alt || "图片"}（请在原笔记查看）</span>}}>{day.data.personalDiary.markdown}</ReactMarkdown> : <p>未授权日记或当天文件不存在。可在设置指定日期文件，不参与远程整理。</p>}</section>
    </> : null}
    {view === 'review' ? <>
      <header><h3>让旧笔记跟上新理解</h3><p>选择原始材料和目标笔记，准备提案后先看差异，再决定是否更新。</p></header>
      <div className="vault-revision-inputs"><label>原始材料{select(source, setSource, '选择来源笔记')}</label><label>目标笔记{select(target, setTarget, '不指定，只整理回顾')}</label></div>
      <button disabled={busy || !source || !policy.data?.policy.remoteProcessing} onClick={() => void run(async () => { const r = await call<{ diary: string; notice: string; reason: string; jev: { notice?: string } }>({ action: 'organize', sourceRefs: refs(), noteId: target, ...dayArgs }); setDraft(r.diary); setNotice([r.notice, r.reason, r.jev.notice].filter(Boolean).join(' ')); await refresh(); })}>{busy ? '正在处理…' : '整理回顾与修订'}</button>
      {!policy.data?.policy.remoteProcessing ? <p>当前仅本地。手动提案可用；模型整理需在设置中授权发送所选材料。</p> : null}
      {draft ? <section><h4>工作回顾草稿 · 与提案共享原始来源</h4><ReactMarkdown components={{img:({alt})=><span>附件：{alt || "图片"}（请在原笔记查看）</span>}}>{draft}</ReactMarkdown></section> : null}
      <details><summary>手动准备精确修改</summary><form onSubmit={e => { e.preventDefault(); void run(async () => { const n = notes.find(n => n.id === target); if (!n) throw new Error('请选择目标笔记。'); await call({ action: 'prepare', noteId: target, baseRevision: n.revision, sourceRefs: refs(), before, after, reason, project }); await refresh(); setNotice('提案已准备，原文尚未改变。'); }); }}>
        <label>原文片段（留空表示追加）<textarea rows={3} value={before} onChange={e => setBefore(e.target.value)} /></label><label>建议片段<textarea required rows={4} value={after} onChange={e => setAfter(e.target.value)} /></label><label>修改依据<input value={reason} onChange={e => setReason(e.target.value)} /></label><button disabled={busy || !source || !target || !after.trim()}>准备提案</button>
      </form></details>
      <section aria-label="待审核提案">{!proposals.data?.items.length ? <p className="vault-empty">还没有待审核更新。有明确新材料时再整理，不必每天清空收件箱。</p> : null}
        {proposals.data?.items.map(p => <article className="vault-proposal" key={p.id}><header><h4>{p.path || '目标不可读'}</h4><span>{labels[p.state] ?? p.state}</span></header><p>{p.reason}</p>{p.conflict ? <p role="alert">原文已经改变，请保留双方内容重新准备提案。</p> : null}<pre>{p.diff}</pre>
          <div className="vault-actions"><button disabled={busy || !p.readable || p.conflict || p.state !== 'prepared'} onClick={() => void run(async () => { await call({ action: 'approve', proposalId: p.id, proposalRevision: p.revision }); await refresh(); setNotice('已批准这一版，请在 Obsidian 中运行“PAW：查看笔记更新”应用。'); })}>接受这一版</button>
            <button disabled={busy || !p.readable || p.conflict || p.state === 'saved'} onClick={() => void run(async () => { const r = await call<{ path: string }>({ action: 'draft', proposalId: p.id, proposalRevision: p.revision }); setNotice(`已另存 ${r.path}，没有修改原笔记。`); })}>另存草稿</button>
            <button disabled={busy || p.state !== 'prepared'} onClick={() => void run(async () => { await call({ action: 'dismiss', proposalId: p.id, proposalRevision: p.revision }); await refresh(); })}>忽略</button></div>
        </article>)}
      </section><section><h3>采纳为项目事项</h3><p>这是独立于改文的动作。只采纳你明确选定的原文陈述，不采纳整篇笔记。</p><label>精确原文陈述<textarea rows={3} value={statement} onChange={e=>setStatement(e.target.value)}/></label><button disabled={busy||!target||!project.trim()||!statement.trim()} onClick={()=>void run(async()=>{const n=notes.find(n=>n.id===target);const r=await call<{notice:string}>({action:'adopt_memory',noteId:target,baseRevision:n?.revision??'',statement,project,confirm:true});setNotice(r.notice);await refresh();})}>明确采纳到本项目 Memory</button><ul>{adoptions.data?.items.map(item=><li key={item.id}>{item.statement} · {item.state==='adopted'?'已采纳':item.state==='needs_review'?'依据已变化，已停用，等待复核':'处理中'}</li>)}</ul></section><section><h3>下次继续用</h3><p>重新读取目标笔记当前版本，生成本次讨论参考。不会自动发送或执行。</p><button disabled={busy || !target} onClick={() => void run(async () => { const r = await call<{ markdown: string; notice: string }>({ action: 'context', noteIds: [target] }); setPack(r.markdown); setNotice(r.notice); })}>预览本次参考</button>{pack ? <><textarea aria-label="本次讨论参考" readOnly rows={8} value={pack} /><button onClick={() => void run(async () => { const r = await call<{ markdown: string }>({ action: 'context', noteIds: [target] }); if (r.markdown !== pack) { setPack(r.markdown); throw new Error('内容已更新，请核对新的预览后再复制。'); } await navigator.clipboard.writeText(pack); setNotice('已复制，请确认目标应用后自行粘贴。'); })}>复制参考</button><button disabled={busy||!project.trim()} onClick={()=>void run(async()=>{const r=await call<{notice:string}>({action:'ime_prepare',noteIds:[target],project});setNotice(r.notice);})}>在输入法中复用</button></> : null}</section>
    </> : null}
    {view === 'settings' ? <>
      <header><h3>范围与连接</h3><p>阅读、新文件创建、模型处理和编辑器配对各自独立。</p></header>
      <section><h4>新笔记保存位置</h4><p>当前：{policy.data?.policy.inbox || '未授权'}</p><form onSubmit={e => { e.preventDefault(); void run(() => configure({ inbox })); }}><label>相对目录<input value={inbox} onChange={e => setInbox(e.target.value)} /></label><button disabled={busy || !inbox}>允许在此创建新笔记</button></form></section>
      <section><h4>自动收集文件材料</h4><p>笔记页打开时每 15 秒检查指定目录，记录新文件和新版本的来源，不复制正文、不自动上传。当前：{policy.data?.policy.captureFolder || '关闭'}</p><label>相对目录<input value={captureFolder} onChange={e=>setCaptureFolder(e.target.value)} /></label><label>项目<input value={project} onChange={e=>setProject(e.target.value)} /></label><button disabled={busy||!captureFolder.trim()} onClick={()=>void run(()=>configure({captureFolder,captureProject:project}))}>启用此目录采集</button><button disabled={busy} onClick={()=>void run(()=>configure({captureFolder:''}))}>关闭采集</button></section><section><h4>个人日记</h4><p>仅日期页本地阅读。当前：{policy.data?.policy.personalDiary || '未授权'}</p><form onSubmit={e => { e.preventDefault(); void run(() => configure({ personalDiary })); }}><label>日期文件规则<input value={personalDiary} onChange={e => setPersonalDiary(e.target.value)} placeholder="日记/{date}.md" /></label><button disabled={busy}>保存日记读取范围</button></form></section>
      <section><h4>模型整理</h4><label><input type="checkbox" checked={policy.data?.policy.remoteProcessing ?? false} disabled={busy} onChange={e => void run(() => configure({ remoteProcessing: e.target.checked }))} />允许把明确选中的原文发给配置的模型</label><label><input type="checkbox" checked={policy.data?.policy.jevEnabled ?? false} disabled={busy || !policy.data?.policy.remoteProcessing} onChange={e => void run(() => configure({ jevEnabled: e.target.checked }))} />使用 Jev 辅助判断</label><p>Jev 不能批准写入；Key 使用 PAW 受保护配置，不保存在插件。</p></section>
      <section><h4>Obsidian 编辑器</h4><p>安装配套插件后，填入笔记库 ID 和配对码。插件仅在本次运行内存中保留配对码。</p><div className="vault-actions"><button disabled={busy} onClick={() => void run(async () => { setPairing(await call({ action: 'pair' })); })}>生成配对码</button><button disabled={busy} onClick={() => void run(async () => { await call({ action: 'revoke' }); setPairing(null); setNotice('配对已撤销。'); })}>撤销配对</button></div>{pairing ? <div><label>笔记库 ID<input readOnly value={pairing.vaultId} /></label><label>配对码<input readOnly type="password" value={pairing.pairingToken} /></label><button onClick={() => void run(async () => { await navigator.clipboard.writeText(pairing.pairingToken); setNotice('已复制配对码。'); })}>复制配对码</button></div> : null}</section>
      <section><h4>带走我的笔记</h4><p>导出当前可读 Markdown。不会打包私人排除目录、Key 或批准权限；配对和应用记录需独立备份。</p><button disabled={busy} onClick={()=>void run(async()=>{const r=await call<{base64:string;fileName:string;noteCount:number}>({action:'export_notes'});const data=Uint8Array.from(atob(r.base64),c=>c.charCodeAt(0));const url=URL.createObjectURL(new Blob([data],{type:'application/zip'}));const a=document.createElement('a');a.href=url;a.download=r.fileName;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);setNotice(`已准备 ${r.noteCount} 篇 Markdown 的导出包。`);})}>导出可读笔记</button></section><section><h4>清理整理记录</h4><p>保留所有 Markdown、本人日记和独立采纳的 Memory；不能撤回外部副本。</p><button disabled={busy} onClick={() => void run(async () => { const r = await call<{ proposals: number }>({ action: 'forget_preview' }); setNotice(`将清理 ${r.proposals} 个提案及整理记录和配对，保留 Markdown。`); setForgetPreview(true); })}>查看清理范围</button>{forgetPreview ? <button disabled={busy} onClick={() => void run(async () => { await call({ action: 'forget', confirm: true }); setForgetPreview(false); setPairing(null); setPack(''); setDraft(''); await refresh(); setNotice('记录已清理，原笔记保留。'); })}>确认清理整理记录</button> : null}</section>
    </> : null}
  </section>;
}
