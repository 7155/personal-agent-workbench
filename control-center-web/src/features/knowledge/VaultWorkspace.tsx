import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { VaultMarkdown } from './VaultMarkdown';
import { useControlTransport } from '@/app/control-transport';
import './vault-workspace.css';
import { VaultRelations } from './VaultRelations';
import { VaultActivity } from './VaultActivity';

type Space={id:string;name:string;root:string;excluded:string[];paused:boolean};
type Note={id:string;path:string;title:string;revision:string;aliases:string[];identityState:string};
type Edge={source:string;target:string;kind:'explicit_link'|'revision_source';label:string;locator:string;locatorValid:boolean;sourceRevision:string};
type Snapshot={notes:Note[];edges:Edge[];total:number;truncated:boolean;unreadableCount:number};
type Reading={noteId:string;path:string;markdown:string;revision:string;obsidianUri:string};

export function VaultWorkspace(){
  const [graphMode,setGraphMode]=useState<'notes'|'project'|'growth'>('notes');
  const transport=useControlTransport(),client=useQueryClient();
  const [root,setRoot]=useState(''),[excluded,setExcluded]=useState('.obsidian\n.git\n日记\n私人\nprivate\nPAW生成');
  const [spaceId,setSpaceId]=useState(''),[noteId,setNoteId]=useState(''),[query,setQuery]=useState(''),[search,setSearch]=useState('');
  const [view,setView]=useState<'list'|'graph'|'day'|'review'|'settings'>('list'),[edge,setEdge]=useState<Edge|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const call=async<T,>(body:Record<string,string|boolean|string[]>)=>await transport.request({pathId:'knowledgeVault.manage',body}) as T;
  const spaces=useQuery({queryKey:['knowledge-vaults'],queryFn:()=>call<{spaces:Space[]}>({action:'list'})});
  const space=spaces.data?.spaces.find(s=>s.id===spaceId);
  useEffect(()=>{if(!spaceId&&spaces.data?.spaces[0])setSpaceId(spaces.data.spaces[0].id);},[spaceId,spaces.data]);
  const snapshot=useQuery({queryKey:['knowledge-vault',spaceId,search,view==='graph'?noteId:''],queryFn:()=>call<Snapshot>({action:'snapshot',vaultId:spaceId,query:search,focusId:view==='graph'?noteId:''}),enabled:!!space&&!space.paused,refetchInterval:15_000});
  const reading=useQuery({queryKey:['knowledge-note',spaceId,noteId],queryFn:()=>call<Reading>({action:'read',vaultId:spaceId,noteId}),enabled:!!space&&!space.paused&&!!noteId,refetchInterval:5_000});
  async function connect(){setBusy(true);setError('');try{const result=await call<{space:Space}>({action:'connect',root,excluded:excluded.split('\n').map(p=>p.trim()).filter(Boolean)});setSpaceId(result.space.id);setNoteId('');await spaces.refetch();}catch(e){setError(e instanceof Error?e.message:'文件夹连接失败');}finally{setBusy(false);}}
  async function pause(){if(!space)return;setBusy(true);try{await call({action:'pause',vaultId:space.id,paused:!space.paused});setNoteId('');setEdge(null);client.removeQueries({queryKey:['knowledge-note',spaceId]});client.removeQueries({queryKey:['vault-attachment',spaceId]});client.removeQueries({queryKey:['knowledge-vault',spaceId]});await spaces.refetch();}catch(e){setError(String(e));}finally{setBusy(false);}}
  const notes=space?.paused?[]:snapshot.data?.notes??[],edges=space?.paused?[]:snapshot.data?.edges??[];
  const active=notes.find(n=>n.id===noteId);
  function open(note:Note){setNoteId(note.id);setEdge(null);}
  return <section className="vault-workspace" aria-label="本地笔记">
    <header className="vault-workspace__header"><div><h2>本地笔记</h2><p>直接阅读同一份 Markdown。原文留在你的文件夹里，默认不交给模型。</p></div>
      {space?<><select aria-label="笔记文件夹" value={spaceId} onChange={e=>{setSpaceId(e.target.value);setNoteId('');setEdge(null);}}>{spaces.data?.spaces.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select><button disabled={busy} onClick={()=>void pause()}>{space.paused?'恢复读取':'暂停读取'}</button><button disabled={space.paused||snapshot.isFetching} onClick={()=>{void snapshot.refetch();if(noteId)void reading.refetch();}}>刷新</button></>:null}
    </header>
    <details className="vault-connect" open={!space}><summary>连接 Markdown 文件夹</summary><form onSubmit={e=>{e.preventDefault();void connect();}}>
      <label>本地文件夹路径<input required value={root} onChange={e=>setRoot(e.target.value)} placeholder="/Users/…/我的笔记"/></label>
      <label>排除目录（每行一个相对路径）<textarea rows={3} value={excluded} onChange={e=>setExcluded(e.target.value)}/></label>
      <p>这里只授权读取。私人日记默认排除；不复制原文、不改已有笔记，也不自动采纳为 Memory。</p><button disabled={busy||!root.trim()} type="submit">{busy?'正在连接…':'连接并读取'}</button>
    </form></details>
    {(error||spaces.error||snapshot.error||reading.error)?<p role="alert">{error||String(spaces.error||snapshot.error||reading.error)}</p>:null}
    {space?.paused?<p role="status">此文件夹已暂停读取，正文、搜索和关系图均已收起。</p>:space?<>
      <nav className="vault-workspace__tools" aria-label="笔记浏览"><form onSubmit={e=>{e.preventDefault();setSearch(query);}}><input aria-label="搜索笔记" value={query} onChange={e=>setQuery(e.target.value)} placeholder="正文、标题、中文或别名"/><button type="submit">搜索</button></form><button aria-pressed={view==='list'} onClick={()=>setView('list')}>笔记</button><button aria-pressed={view==='graph'} onClick={()=>setView('graph')}>关系图</button><button aria-pressed={view==='day'} onClick={()=>setView('day')}>日期页</button><button aria-pressed={view==='review'} onClick={()=>setView('review')}>待审核</button><button aria-pressed={view==='settings'} onClick={()=>setView('settings')}>设置</button><span>{snapshot.data?.total??0} 篇</span></nav>
      {snapshot.data?.truncated?<p role="status">本次显示最多 200 篇；部分文件未读取或结果超出范围，请缩小目录或搜索范围。不会将缺失内容当作已索引。</p>:null}
      {view==='day'||view==='review'||view==='settings'?<VaultActivity key={spaceId} vaultId={spaceId} notes={notes} view={view}/>:<div className="vault-workspace__panes"><aside aria-label="笔记目录">
        {snapshot.isPending?<p>正在读取笔记…</p>:!notes.length?<p>当前范围没有匹配笔记。</p>:null}
        {notes.map(note=><button key={note.id} className="vault-note" aria-label={`阅读 ${note.title}`} aria-pressed={note.id===noteId} onClick={()=>open(note)}><strong>{note.title}</strong><small>{note.path}</small>{note.identityState==='duplicate_id'?<small>稳定标识重复，请在编辑器中核对</small>:null}</button>)}
      </aside><main>
        {view==='graph'?<section className="vault-graph" aria-label="显式笔记关系图"><div className="vault-actions"><button aria-pressed={graphMode==='notes'} onClick={()=>setGraphMode('notes')}>笔记引用</button><button aria-pressed={graphMode==='project'} onClick={()=>setGraphMode('project')}>项目应用</button><button aria-pressed={graphMode==='growth'} onClick={()=>setGraphMode('growth')}>成长轨迹</button></div>{graphMode!=='notes'?<VaultRelations vaultId={spaceId} mode={graphMode} onOpen={setNoteId}/>:<><p>显示明确链接及真实应用记录。相似度不作为支持证据，点笔记可在此阅读。</p>
          <svg viewBox="0 0 600 360" role="img" aria-label="笔记引用关系概览">{edges.filter(e=>notes.findIndex(n=>n.id===e.source)<30&&notes.findIndex(n=>n.id===e.target)<30).map((e,i)=>{const a=position(notes.findIndex(n=>n.id===e.source)),b=position(notes.findIndex(n=>n.id===e.target));return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#b9c5d0"/>;})}{notes.slice(0,30).map((n,i)=>{const p=position(i);return <g key={n.id} role="button" tabIndex={0} aria-label={`阅读 ${n.title}`} onClick={()=>open(n)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open(n);}}}><circle cx={p.x} cy={p.y} r={7} fill={n.id===noteId?'#087ea4':'#5a7186'}/><text x={p.x+11} y={p.y+4} fontSize={11}>{n.title.slice(0,14)}</text></g>;})}</svg>
          {notes.length>30?<small>概览显示前 30 篇；全部可读笔记仍在左侧目录。</small>:null}
          <ul>{edges.map((e,i)=><li key={i}><button onClick={()=>setEdge(e)}>{notes.find(n=>n.id===e.source)?.title} → {notes.find(n=>n.id===e.target)?.title} · {e.label}</button></li>)}</ul>
          {edge?<p role="status">依据：{edge.kind==='revision_source'?'真实应用回执':'来源笔记中的明确链接'} · 版本 {edge.sourceRevision.slice(0,8)}{edge.locator?` · 定位 ${edge.locator}（${edge.locatorValid?'存在':'已失效'}）`:''}</p>:null}
        </>}</section>:null}
        {noteId?<article className="vault-reader" aria-label="笔记全文"><header><div><h3>{active?.title??reading.data?.path}</h3><small>{reading.data?.path} · {reading.data?.revision.slice(0,8)}</small></div>{reading.data?<a href={reading.data.obsidianUri}>用 Obsidian 深度编辑</a>:null}</header>
          {reading.isPending?<p>正在读取完整原文…</p>:reading.data?<VaultMarkdown vaultId={spaceId} noteId={noteId} markdown={reading.data.markdown} onLink={link=>{void call<Reading & {locatorValid:boolean}>({action:'resolve',vaultId:spaceId,noteId,link}).then(n=>{setNoteId(n.noteId);setError(n.locatorValid?'':'引用的标题或块已失效，显示完整原文。');}).catch(e=>setError(String(e)));}}/>:null}
          <footer>这是原文件的只读视图；保存笔记不等于采纳其内容，也不授予执行权限。</footer>
        </article>:<p className="vault-empty">选择一篇笔记，在 PAW 内阅读完整原文。Obsidian 是可选编辑器。</p>}
      </main></div>}
    </>:null}
  </section>;
}
function position(index:number){return {x:25+(index%3)*195,y:24+Math.floor(index/3)*34};}
