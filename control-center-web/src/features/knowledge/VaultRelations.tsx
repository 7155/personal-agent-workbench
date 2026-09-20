import { useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
type Relations={nodes:{id:string;kind:string;label:string;noteId:string}[];edges:{source:string;target:string;label:string;basis:string}[]};
export function VaultRelations({vaultId,mode,onOpen}:{vaultId:string;mode:'project'|'growth';onOpen:(noteId:string)=>void}){
 const transport=useControlTransport();
 const data=useQuery({queryKey:['vault-relations',vaultId,mode],queryFn:async()=>await transport.request({pathId:'knowledgeVault.manage',body:{action:'graph_business',vaultId,graphMode:mode}}) as Relations});
 if(data.isPending)return <p role="status">正在读取工作关系…</p>;
 if(data.error)return <p role="alert">关系读取失败，请刷新重试。</p>;
 const nodes=data.data?.nodes??[],edges=data.data?.edges??[];
 return <section className="vault-business-relations"><p>{mode==='project'?'只展示明确的项目绑定、已采纳事项和实际修订。':'只展示实际保存与修订的轨迹，不据此评价是否学会。'}</p>{!nodes.length?<p>尚无对应工作关系。保存材料或完成一次修订后会出现在这里。</p>:null}<div className="vault-relation-nodes">{nodes.map(n=><button key={n.id} disabled={!n.noteId} onClick={()=>onOpen(n.noteId)}>{n.label}</button>)}</div><ul>{edges.map((e,i)=><li key={i}><details><summary>{nodes.find(n=>n.id===e.source)?.label} → {nodes.find(n=>n.id===e.target)?.label} · {e.label}</summary><p>依据：{e.basis}</p></details></li>)}</ul></section>;
}
