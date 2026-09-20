import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { VaultWorkspace } from './VaultWorkspace';

afterEach(cleanup);
function setup() {
  const space = { id:'v', name:'我的笔记', root:'/notes', excluded:['私人'], paused:false };
  let state='prepared';
  const transport = new MockControlTransport({routes:{'knowledgeVault.manage':(request:ControlRequest)=>{
    const p=request.body as Record<string,unknown>;
    switch(p.action){
      case 'list': return {spaces:[space]};
      case 'snapshot': return {notes:[{id:'a',title:'缓存',path:'a.md',revision:'v1',aliases:[]},{id:'b',title:'索引',path:'b.md',revision:'v2',aliases:[]}],edges:[],total:2};
      case 'read': return {noteId:p.noteId,path:'a.md',revision:'v1',markdown:p.noteId==='a'?'# 缓存\n全文不是摘要 [[索引]]':'# 索引\n引用目标全文',obsidianUri:'obsidian://open?path=a.md'};
      case 'resolve': return {noteId:'b',locatorValid:true};
      case 'settings': return {policy:{inbox:'收件箱',remoteProcessing:false,jevEnabled:false,personalDiary:''}};
      case 'ime_target': return {project:'demo'};
      case 'suggest_targets': return {candidates:[{id:'z',title:'旧设计',path:'z.md',revision:'v3',snippet:'缓存过期设计',matchedTerms:['缓存'],score:4}],notice:'仅表示可能相关',scanIncomplete:false};
      case 'adoptions': return {items:[]};
      case 'proposals': return {items:[{id:'p',revision:1,path:'a.md',reason:'新材料补充',diff:'-旧\n+新',state,readable:true,conflict:false}]};
      case 'approve': state='waiting_editor';return {state};
      case 'pause':space.paused=Boolean(p.paused);return {paused:space.paused};
      default:throw new Error('Unexpected action '+String(p.action));
    }
  }}});
  const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
  render(<ControlTransportProvider transport={transport}><QueryClientProvider client={client}><VaultWorkspace/></QueryClientProvider></ControlTransportProvider>);
  return transport;
}
it('reads full Markdown and resolves wikilinks inside PAW',async()=>{
  setup();const user=userEvent.setup();
  await user.click(await screen.findByRole('button',{name:'阅读 缓存'}));
  expect(await screen.findByText(/全文不是摘要/)).toBeVisible();
  await user.click(screen.getByRole('button',{name:'索引'}));
  expect(await screen.findByText('引用目标全文')).toBeVisible();
});
it('approval waits for the editor and never claims original was saved',async()=>{
  const transport=setup();const user=userEvent.setup();
  await user.click(await screen.findByRole('button',{name:'待审核'}));
  await user.click(await screen.findByRole('button',{name:'接受这一版'}));
  expect(await screen.findByText('等待编辑器')).toBeVisible();
  expect(screen.queryByText('原文已保存')).not.toBeInTheDocument();
  expect(transport.requests.some(r=>(r.request.body as Record<string,unknown>)?.action==='approve')).toBe(true);
});
it('pause removes visible content and graph',async()=>{
  setup();const user=userEvent.setup();
  await user.click(await screen.findByRole('button',{name:'阅读 缓存'}));
  await screen.findByText(/全文不是摘要/);
  await user.click(screen.getByRole('button',{name:'暂停读取'}));
  expect(await screen.findByText(/已暂停读取，正文/)).toBeVisible();
  expect(screen.queryByText(/全文不是摘要/)).not.toBeInTheDocument();
});

it('finds and selects a target outside the initial note page without enabling remote processing',async()=>{
  const transport=setup();const user=userEvent.setup();
  await user.click(await screen.findByRole('button',{name:'待审核'}));
  await user.selectOptions(screen.getByLabelText('原始材料'),'a');
  await user.click(screen.getByRole('button',{name:'查找可能更新的旧笔记'}));
  expect(await screen.findByText('缓存过期设计')).toBeVisible();
  await user.click(screen.getByRole('button',{name:'选择作为修订目标'}));
  expect(screen.getByLabelText('目标笔记')).toHaveValue('z');
  expect(screen.getByRole('button',{name:'整理回顾与修订'})).toBeDisabled();
  expect(transport.requests.some(r=>(r.request.body as Record<string,unknown>)?.action==='organize')).toBe(false);
});
