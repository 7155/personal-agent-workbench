// Ported from the supplied v2.1 core suite; runs against repository modules.
import { test } from 'vitest';
import assert from 'node:assert/strict';
import * as model from './organization-model';
import { loadOrganizationCatalog } from './organization-catalog';
import { OrganizationJournal, journalForTransport } from './organization-journal';
import { OrganizationController } from './organization-controller';
import { createAgentModeStore } from './agent-mode-store';
const delay = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve,reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return { promise,resolve,reject }; };
const space = (id='one', fields={}) => ({key:`session:${id}`, title:`空间 ${id}`, category:'unknown',placement:'desk',pinned:false,group:'',revision:0,updatedAtMs:1,sourceRevision:'source-1',...fields});
const batch = (items, extra={}) => ({ok:true,items,unavailable:[],receipts:[],...extra});
const storage = () => { const data = new Map(); return { getItem:k=>data.get(k)??null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k),data }; };
function fixture(items=[space()]) {
  const rows = new Map(items.map(row=>[row.key,{...row}])); const applied=new Map(); const calls=[];
  let adapter; let id=0;
  const raw = async req => {
    calls.push(structuredClone({pathId:req.pathId,body:req.body}));
    if (req.pathId==='agent.organization.read') return batch(req.body.keys.filter(k=>rows.has(k)).map(k=>({...rows.get(k)})),{
      unavailable:req.body.keys.filter(k=>!rows.has(k)),receipts:[...applied.values()].filter(r=>req.body.keys.includes(r.spaceKey)&&rows.get(r.spaceKey).revision===r.appliedRevision).map(({id,spaceKey,appliedRevision})=>({id,spaceKey,appliedRevision}))});
    if (req.pathId==='agent.organization.command') {
      const b=req.body;const existing=applied.get(b.commandId);
      if(existing)return {ok:true,receiptId:b.commandId,replayed:true};
      const item=rows.get(b.spaceKey);
      if(item.revision!==b.expectedRevision)throw Object.assign(new Error('revision conflict'),{status:409});
      const before={...item};item[b.operation]=b.value;item.revision++;
      applied.set(b.commandId,{id:b.commandId,spaceKey:b.spaceKey,appliedRevision:item.revision,before});
      return {ok:true,receiptId:b.commandId,replayed:false};
    }
    if(req.pathId==='agent.organization.undo') {
      const receipt=applied.get(req.body.receiptId);
      if(!receipt)throw Object.assign(new Error('not found'),{status:400});
      if(receipt.undone)return {ok:true,replayed:true};
      const item=rows.get(receipt.spaceKey);
      if(item.revision!==receipt.appliedRevision)throw Object.assign(new Error('new edit'),{status:409});
      rows.set(receipt.spaceKey,{...receipt.before,revision:item.revision+1});receipt.undone=true;
      return {ok:true,replayed:false};
    }
    return {ok:true,proposal:null,message:'依据不足'};
  };
  const request = req=>adapter?adapter(req,raw):raw(req);
  const journal=new OrganizationJournal('pending',storage());
  const controller=new OrganizationController(request,journal,()=>1000,()=>`c${++id}`);
  return {rows,applied,calls,request,raw,journal,controller,adapt:f=>{adapter=f;}};
}

test('global catalog reads all 250 supplied keys, not only visible page',async()=>{
  const items=Array.from({length:250},(_,i)=>space(String(i),{title:i===249?'唯一复工项目':`项目${i}`}));
  const f=fixture(items);await f.controller.setKeys(items.map(x=>x.key));
  const all=f.controller.getSnapshot().catalog.items;assert.equal(all.length,250);assert.equal(f.calls.length,3);
  assert.equal(model.selectSpaces(all,{query:'唯一复工',placement:'all'}).length,1);f.controller.dispose();
});
test('filter first, paginate second',()=>{
  const items=Array.from({length:250},(_,i)=>space(String(i),{title:i===249?'目标':'其他'}));
  assert.equal(model.pageItems(model.selectSpaces(items,{query:'目标',placement:'all'}),0).items[0].key,'session:249');
});
test('global category/placement filters and NFKC multi-term matching',()=>{
  const item=space('x',{title:'ＲＡＧ 实验',group:'知识库',category:'waiting',placement:'shelf'});
  assert.equal(model.selectSpaces([item],{query:'rag   知识库',placement:'all',category:'waiting'}).length,1);
  assert.equal(model.selectSpaces([item],{query:'rag',placement:'desk'}).length,0);
});
test('deterministic pin/record update/title ordering',()=>{
  const list=[space('c',{updatedAtMs:100}),space('b',{updatedAtMs:200}),space('a',{pinned:true,updatedAtMs:1})];
  assert.deepEqual(model.selectSpaces(list,{query:'',placement:'all'}).map(x=>x.key),['session:a','session:b','session:c']);
});
test('empty directory makes no requests',async()=>{
  let calls=0;const r=await loadOrganizationCatalog(async()=>{calls++;},[],new AbortController().signal);assert.equal(calls,0);assert.equal(r.requestedCount,0);
});
test('bounded read concurrency',async()=>{
  let active=0,maximum=0;
  const keys=Array.from({length:601},(_,i)=>`session:${i}`);
  await loadOrganizationCatalog(async req=>{active++;maximum=Math.max(maximum,active);await delay();active--;return batch(req.body.keys.map(k=>space(k.split(':')[1])));},keys,new AbortController().signal);
  assert.equal(maximum,3);
});
test('partial failure identifies exact missing batch without stale rows',async()=>{
  const keys=Array.from({length:205},(_,i)=>`session:${i}`);let n=0;
  const r=await loadOrganizationCatalog(async req=>{if(++n===2)throw Error('offline');return batch(req.body.keys.map(k=>space(k.split(':')[1])));},keys,new AbortController().signal);
  assert.equal(r.failedKeys.length,100);assert.equal(r.items.length,105);assert.equal(r.requestedCount,205);
});
test('cancelled late read does not return usable catalog',async()=>{
  const abort=new AbortController(),wait=deferred();
  const p=loadOrganizationCatalog(()=>wait.promise,['session:one'],abort.signal);
  abort.abort();wait.resolve(batch([space()]));await assert.rejects(p,{name:'AbortError'});
});
test('duplicate or outside response keys rejected',()=>{
  assert.throws(()=>model.parseBatch(batch([space(),space()]),['session:one']));
  assert.throws(()=>model.parseBatch(batch([space('other')]),['session:one']));
});
test('omitted response records are incomplete, not empty',()=>assert.throws(()=>model.parseBatch(batch([]),['session:one'])));
test('unavailable is explicitly accounted for',()=>assert.deepEqual(model.parseBatch(batch([],{unavailable:['session:one']}),['session:one']).unavailable,['session:one']));
test('invalid revision and pin/shelf contradiction rejected',()=>{
  for(const revision of [-1,true,NaN,1.1])assert.throws(()=>model.parseSpace(space('x',{revision})));
  assert.throws(()=>model.parseSpace(space('x',{pinned:true,placement:'shelf'})));
});
test('receipt must refer to returned space and current revision',()=>{
  assert.throws(()=>model.parseBatch(batch([space()],{receipts:[{id:'c',spaceKey:'session:other'}]}),['session:one']));
  assert.throws(()=>model.parseBatch(batch([space()],{receipts:[{id:'c',spaceKey:'session:one',appliedRevision:2}]}),['session:one']));
});
test('legacy receipt shape still accepted',()=>assert.equal(model.parseBatch(batch([space()],{receipts:[{id:'c',spaceKey:'session:one'}]}),['session:one']).receipts.length,1));
test('exact proposal expiry boundary rejected',()=>assert.throws(()=>model.parseProposal({ok:true,proposal:{id:'p',spaceKey:'session:one',category:'waiting',basis:'title',expiresAtMs:1000}},space(),1000)));
test('stale proposal revision rejected',()=>assert.throws(()=>model.parseProposal({ok:true,proposal:{id:'p',spaceKey:'session:one',category:'waiting',basis:'title',expiresAtMs:2000,expectedRevision:2}},space(),1000)));
test('metadata loader refuses runaway catalog without truncation',async()=>{
  await assert.rejects(loadOrganizationCatalog(async()=>batch([]),['session:a','session:b'],new AbortController().signal,{maxKeys:1}),/目录过大/);
});
test('page clamps after result count shrinks',()=>assert.deepEqual(model.pageItems([1,2],99,1),{items:[2],page:1,pages:2}));
test('late scope A cannot overwrite current scope B',async()=>{
  const f=fixture([space('a'),space('b')]),wait=deferred();
  f.adapt((req,raw)=>req.pathId==='agent.organization.read'&&req.body.keys[0]==='session:a'?wait.promise:raw(req));
  const first=f.controller.setKeys(['session:a']);await f.controller.setKeys(['session:b']);wait.resolve(batch([space('a')]));await first;
  assert.deepEqual(f.controller.getSnapshot().catalog.items.map(x=>x.key),['session:b']);f.controller.dispose();
});
test('changed scope failed load removes old rows',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);f.adapt(()=>{throw Error('offline');});
  await f.controller.setKeys(['session:new']);assert.equal(f.controller.getSnapshot().catalog.items.length,0);assert.equal(f.controller.getSnapshot().phase,'error');f.controller.dispose();
});
test('successful write followed by failed refresh is not called a failed write',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);let wrote=false;
  f.adapt(async(req,raw)=>{if(wrote&&req.pathId==='agent.organization.read')throw Error('offline');const r=await raw(req);if(req.pathId==='agent.organization.command')wrote=true;return r;});
  await f.controller.change('session:one',{operation:'category',value:'waiting'});
  assert.equal(f.journal.getSnapshot(),null);assert.match(f.controller.getSnapshot().notice,/已保存.*只重试读取/);assert.equal(f.rows.get('session:one').revision,1);f.controller.dispose();
});
test('lost command response retry reuses ID and applies only once',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);let dropped=false;
  f.adapt(async(req,raw)=>{const r=await raw(req);if(req.pathId==='agent.organization.command'&&!dropped){dropped=true;throw Error('response lost');}return r;});
  await f.controller.change('session:one',{operation:'category',value:'waiting'});
  assert.equal(f.journal.getSnapshot().id,'c1');assert.equal(f.journal.getSnapshot().uncertain,true);
  await f.controller.retryPending();assert.equal(f.journal.getSnapshot(),null);assert.equal(f.rows.get('session:one').revision,1);
  assert.deepEqual(f.calls.filter(x=>x.pathId==='agent.organization.command').map(x=>x.body.commandId),['c1','c1']);f.controller.dispose();
});
test('unknown previous write blocks new conflicting changes',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);f.adapt(()=>{throw Error('network');});
  await f.controller.change('session:one',{operation:'group',value:'A'});await f.controller.change('session:one',{operation:'group',value:'B'});
  assert.equal(f.journal.getSnapshot().body.value,'A');assert.match(f.controller.getSnapshot().error,/先核实/);f.controller.dispose();
});
test('definite first 409 rejection clears pending and rereads',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);f.rows.get('session:one').revision=5;
  await f.controller.change('session:one',{operation:'group',value:'A'});assert.equal(f.journal.getSnapshot(),null);assert.equal(f.controller.getSnapshot().catalog.items[0].revision,5);f.controller.dispose();
});
test('409 on retry after unknown is still unknown',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);f.adapt(()=>{throw Error('lost');});await f.controller.change('session:one',{operation:'group',value:'A'});
  f.adapt(()=>{throw Object.assign(Error('now unavailable'),{status:409});});await f.controller.retryPending();assert.equal(f.journal.getSnapshot().uncertain,true);f.controller.dispose();
});
test('mismatched receipt cannot confirm success',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);f.adapt(()=>({ok:true,receiptId:'wrong'}));
  await f.controller.change('session:one',{operation:'group',value:'A'});assert.ok(f.journal.getSnapshot());f.controller.dispose();
});
test('matching metadata alone never confirms an uncertain command',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);f.adapt(async(req,raw)=>{const r=await raw(req);if(req.pathId==='agent.organization.command')throw Error('lost');return r;});
  await f.controller.change('session:one',{operation:'group',value:'A'});await f.controller.refresh();assert.ok(f.journal.getSnapshot());f.controller.dispose();
});
test('undo and duplicate undo confirmations are supported',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);await f.controller.change('session:one',{operation:'group',value:'A'});
  const r=f.controller.getSnapshot().catalog.receipts[0];await f.controller.undo(r);assert.equal(f.rows.get('session:one').group,'');assert.equal(f.journal.getSnapshot(),null);f.controller.dispose();
});
test('disposed controller cannot publish late reads',async()=>{
  const wait=deferred(),journal=new OrganizationJournal('x');const c=new OrganizationController(()=>wait.promise,journal);let renders=0;c.subscribe(()=>renders++);
  const p=c.setKeys(['session:one']);c.dispose();const prior=renders;wait.resolve(batch([space()]));await p;assert.equal(renders,prior);
});
test('disposing during write preserves pending intent for next view',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);const wait=deferred();f.adapt(()=>wait.promise);
  const p=f.controller.change('session:one',{operation:'group',value:'A'});await Promise.resolve();f.controller.dispose();assert.equal(f.journal.getSnapshot().uncertain,true);
  wait.reject(Error('abort'));await p;assert.ok(f.journal.getSnapshot());
});
test('suggestion request only includes selected key',async()=>{
  const f=fixture();await f.controller.setKeys(['session:one']);await f.controller.suggest('session:one');
  assert.deepEqual(f.calls.find(x=>x.pathId==='agent.organization.suggest').body,{spaceKey:'session:one'});f.controller.dispose();
});
test('new keys discard stale pending suggestion',async()=>{
  const f=fixture([space(),space('two')]);await f.controller.setKeys(['session:one']);const wait=deferred();f.adapt((req,raw)=>req.pathId==='agent.organization.suggest'?wait.promise:raw(req));
  const p=f.controller.suggest('session:one');await f.controller.setKeys(['session:two']);wait.resolve({ok:true,proposal:{id:'p',spaceKey:'session:one',category:'waiting',basis:'title',expiresAtMs:2000}});await p;
  assert.equal(Object.keys(f.controller.getSnapshot().proposals).length,0);f.controller.dispose();
});
test('journal survives reconstruction and is never automatically replayed',()=>{
  const store=storage(),j=new OrganizationJournal('x',store);
  j.begin({id:'c1',spaceKey:'session:one',createdAtMs:10,uncertain:false,pathId:'agent.organization.command',body:{commandId:'c1',spaceKey:'session:one',expectedRevision:0,operation:'group',value:'A'}});
  const next=new OrganizationJournal('x',store);assert.equal(next.getSnapshot().uncertain,true);assert.equal(next.getSnapshot().body.commandId,'c1');
});
test('different connection journals cannot reuse pending commands',()=>{
  const a=journalForTransport({connectionIdentity:'fixture-a'}),b=journalForTransport({connectionIdentity:'fixture-b'});assert.notEqual(a,b);
});
test('anonymous transports are not conflated',()=>{
  const t={};assert.equal(journalForTransport(t),journalForTransport(t));assert.notEqual(journalForTransport({}),journalForTransport({}));
});
test('malformed journal does not issue an action',()=>{
  const s=storage();s.setItem('x','{"pathId":"agent.session.prompt"}');const j=new OrganizationJournal('x',s);assert.equal(j.getSnapshot(),null);assert.equal(j.getPersistenceError(),true);
});
test('storage quota failure retains in-memory write recovery',()=>{
  const j=new OrganizationJournal('x',{getItem:()=>null,setItem:()=>{throw Error('quota');},removeItem:()=>{}});
  j.begin({id:'c',spaceKey:'session:one',createdAtMs:10,uncertain:false,pathId:'agent.organization.undo',body:{receiptId:'c'}});
  assert.ok(j.getSnapshot());assert.ok(j.getPersistenceError());
});
test('mode store defaults to Jev while preserving each explicit saved preference',()=>{
  for (const initial of [null,'invalid','jev','traditional']) {
    let value=initial;const s=createAgentModeStore({read:()=>value,write:v=>{value=v;},subscribe:()=>()=>{}});
    assert.equal(s.getSnapshot(),initial==='traditional'?'traditional':'jev');assert.equal(s.getSnapshot(),s.getSnapshot());assert.equal(s.getServerSnapshot(),'jev');
  }
});
test('mode selection updates every subscriber without Runtime commands',()=>{
  let value='traditional',writes=0;const s=createAgentModeStore({read:()=>value,write:v=>{writes++;value=v;},subscribe:()=>()=>{}});
  s.getSnapshot();
  let a=0,b=0;const ua=s.subscribe(()=>a++),ub=s.subscribe(()=>b++);s.select('jev');assert.equal(a,1);assert.equal(b,1);assert.equal(writes,1);ua();ub();
});
test('mode store receives external tab changes and unsubscribes cleanly',()=>{
  let value='traditional',external,closed=0;const s=createAgentModeStore({read:()=>value,write:v=>{value=v;},subscribe:cb=>{external=cb;return()=>closed++;}});
  const un=s.subscribe(()=>{});value='jev';external();assert.equal(s.getSnapshot(),'jev');un();assert.equal(closed,1);
});
test('mode preference write failure does not block switching',()=>{
  const s=createAgentModeStore({read:()=>{throw Error('private');},write:()=>{throw Error('private');},subscribe:()=>()=>{}});
  const un=s.subscribe(()=>{});s.select('jev');assert.equal(s.getSnapshot(),'jev');un();
});
test('setting an unknown mode does nothing',()=>{
  const s=createAgentModeStore();s.select('invalid');assert.equal(s.getSnapshot(),'jev');
});
test('a successful mutation rereads only the affected space, not every catalog page',async()=>{
  const items=Array.from({length:251},(_,i)=>space(String(i)));const f=fixture(items);await f.controller.setKeys(items.map(i=>i.key));
  f.calls.length=0;await f.controller.change('session:250',{operation:'group',value:'新增分组'});
  const reads=f.calls.filter(c=>c.pathId==='agent.organization.read');assert.equal(reads.length,1);assert.deepEqual(reads[0].body.keys,['session:250']);
  assert.equal(f.controller.getSnapshot().catalog.items.length,251);f.controller.dispose();
});
test('a failed preference write survives closing and reopening all local subscribers',()=>{
  const s=createAgentModeStore({read:()=> 'traditional',write:()=>{throw Error('quota');},subscribe:()=>()=>{}});
  let off=s.subscribe(()=>{});s.select('jev');off();off=s.subscribe(()=>{});assert.equal(s.getSnapshot(),'jev');off();
});
test('in-memory mode store keeps explicit selection on subsequent subscription',()=>{
  const s=createAgentModeStore();s.select('traditional');const off=s.subscribe(()=>{});assert.equal(s.getSnapshot(),'traditional');off();
});
