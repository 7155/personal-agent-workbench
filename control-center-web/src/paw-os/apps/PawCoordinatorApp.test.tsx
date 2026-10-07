import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect } from 'react';
import { useAgentLiveSession } from '@/features/agent/runtime/use-agent-live-session';
import { agentSessionAddress, useAgentLiveStore } from '@/features/agent/state/live-store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { PawOsAppSurfaceProvider, PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import type { ControlRequest } from '@/platform/transport';
import type { SessionSummary } from '@/features/agent/types';
import { createAgentProjection, reduceAgentEvent } from '@/contracts/agent-reducer';
import type { UiAgentEvent } from '@/contracts/ui-events';
import { CHAT_PRESENTATION_STORAGE_KEY } from '@/features/conversation-ui/reading/chat-presentation';
import { PawCoordinatorApp, coordinatorObjects, coordinatorLatestTurnOutcome } from './PawCoordinatorApp';
vi.mock('./agent-workspace-loader', () => ({ loadSessionWorkspace: async () => ({ default: ({ recordId, onSessionCreated }: { recordId: string; onSessionCreated: (session: SessionSummary, draft: string) => void }) => <div><p>持久会话 {recordId}</p><textarea aria-label="Agent 草稿"/><button onClick={() => onSessionCreated({ ...session, id: 'fork', title: '历史分支' }, '带入分支的草稿')}>测试原生分支回调</button></div> }) }));
vi.mock('@/features/agent/runtime/use-agent-live-session', () => ({ useAgentLiveSession: vi.fn(() => vi.fn()) }));
vi.mock('@/features/rooms/runtime/use-room-live-session', () => ({ useRoomLiveSession: () => vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(useAgentLiveSession).mockReset().mockImplementation(() => vi.fn()); localStorage.removeItem(CHAT_PRESENTATION_STORAGE_KEY); });
const session: SessionSummary = { id:'persistent',title:'Agent',mode:'coordinator',status:'idle',updatedAtMs:1,roleId:'sol',roleVersion:'1',roleBookRevisionId:'1',workspaceRoots:['/'],executionMode:'full_trust' };
const target = { ...session, id:'owned',title:'核对资料' };
const object = { id:target.id,kind:'session' as const,coordinatorId:'owner',sourceSessionId:session.id,task:'核对资料',target };
function setup(command: (request: ControlRequest) => unknown = () => ({ ok:true }), objects: unknown[] = [object]) {
 let directory = objects;
 const transport = new MockControlTransport({ routes: { 'agent.coordinator.ensure': () => ({ ok:true,session,coordinatorId:'owner',objects:directory }),
  'agent.coordinator.command': (request: ControlRequest) => { const result = command(request); if (request.body && typeof request.body === 'object' && !Array.isArray(request.body) && request.body.action === 'create_session') directory = [...directory,{ ...object,id:'new',target:{ ...target,id:'new',title:'新增任务' } }]; return result; },
  'agent.roles.list': { items:[{ schemaVersion:'rag-ime.agent-persona.v1',roleId:'one',version:'1',displayName:'协调伙伴',description:'',prompt:'',capabilities:[] }] } } });
 const openWindow = vi.fn();
 const client = new QueryClient();
 const tree = (active: boolean) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><PawOsDesktopProvider openWindow={openWindow}><PawOsAppSurfaceProvider appId="agent-controller" width={375} height={680} active={active}><PawCoordinatorApp/></PawOsAppSurfaceProvider></PawOsDesktopProvider></ControlTransportProvider></QueryClientProvider>;
 const view = render(tree(true));
 return { transport,openWindow,setActive: (active: boolean) => view.rerender(tree(active)) };
}
function coordinatorOutcomeEvent(sequence: number, turnId: string, eventType: UiAgentEvent['eventType'], payload: Record<string, unknown>): UiAgentEvent {
 return { schemaVersion:'rag-ime.agent-event.v1',sessionId:'source',eventId:`source:${sequence}`,turnId,sequence,createdAtMs:sequence * 10,eventType,payload,resumeToken:`source:${sequence}`,streamKind:'agent' };
}
function completedCoordinatorProjection() {
 const answering=reduceAgentEvent(createAgentProjection('source'),coordinatorOutcomeEvent(1,'previous','text_delta',{delta:'Previous answer'})).state;
 return reduceAgentEvent(answering,coordinatorOutcomeEvent(2,'previous','turn_completed',{status:'completed'})).state;
}
describe('persistent coordinator App', () => {
 it('does not label an empty ready bootstrap as a completed conversation turn', () => {
  const projection=createAgentProjection('empty');
  projection.turnOrder=['bootstrap']; projection.turnsById.bootstrap={id:'bootstrap',status:'completed',messageIds:[],activityIds:[],createdAtMs:1,updatedAtMs:1};
  expect(coordinatorLatestTurnOutcome(projection)).toBeUndefined();
  projection.turnsById.bootstrap.messageIds=['user'];
  projection.messagesById.user={schemaVersion:'rag-ime.agent-message.v1',sessionId:'empty',clientMessageId:'user-client',id:'user',turnId:'bootstrap',role:'user',status:'completed',blocks:[],attachments:[],citations:[],createdAtMs:1};
  expect(coordinatorLatestTurnOutcome(projection)).toBe('completed');
 });
 it('filters ownership by exact source and identity rather than history similarity', () => {
  expect(coordinatorObjects([object,{ ...object,sourceSessionId:'other' },{ ...object,coordinatorId:'other' },{ ...object,id:'wrong' },{ ...object,target:{ ...target,status:'archived' } }],session.id,'owner')).toEqual([object]);
 });
 it('keeps the last real outcome through a newer empty bootstrap and honors the next real turn', () => {
  const projection=createAgentProjection('source');
  const user={schemaVersion:'rag-ime.agent-message.v1' as const,sessionId:'source',clientMessageId:'real-client',id:'user',turnId:'real',role:'user' as const,status:'completed' as const,blocks:[],attachments:[],citations:[],createdAtMs:1};
  projection.messagesById.user=user;
  projection.turnsById.real={id:'real',status:'completed',messageIds:['user'],activityIds:[],createdAtMs:1,updatedAtMs:1};
  projection.turnsById.bootstrap={id:'bootstrap',status:'completed',messageIds:[],activityIds:[],createdAtMs:2,updatedAtMs:2};
  projection.turnOrder=['real','bootstrap'];
  expect(coordinatorLatestTurnOutcome(projection)).toBe('completed');
  projection.messagesById.next={...user,id:'next',turnId:'new',clientMessageId:'new-client'};
  projection.turnsById.new={id:'new',status:'running',messageIds:['next'],activityIds:[],createdAtMs:3,updatedAtMs:3};
  projection.turnOrder.push('new');
  expect(coordinatorLatestTurnOutcome(projection)).toBe('running');
  projection.turnsById.new.status='aborted';
  expect(coordinatorLatestTurnOutcome(projection)).toBe('aborted');
 });
 it('keeps an exact newer Stop outcome before its transcript message arrives', () => {
  const previous=completedCoordinatorProjection();
  const running=reduceAgentEvent(previous,coordinatorOutcomeEvent(3,'new','status_changed',{status:'busy'})).state;
  expect(running.turnsById.new.messageIds).toEqual([]);
  expect(running.turnsById.new.activityIds).toEqual([]);
  expect(running.turnOrder.at(-1)).toBe('new');
  expect(coordinatorLatestTurnOutcome(running)).toBe('running');
  const stopped=reduceAgentEvent(running,coordinatorOutcomeEvent(4,'new','turn_completed',{status:'aborted',aborted:true})).state;
  expect(stopped.status).toBe('idle');
  expect(stopped.turnsById.new.status).toBe('aborted');
  expect(stopped.turnsById.previous.status).toBe('completed');
  expect(coordinatorLatestTurnOutcome(stopped)).toBe('aborted');
 });
 it('keeps a newer tool-only failure through the following empty ready bootstrap', () => {
  const working=reduceAgentEvent(completedCoordinatorProjection(),coordinatorOutcomeEvent(3,'tool-only','tool_started',{toolCallId:'new-tool',toolName:'bash'})).state;
  const failed=reduceAgentEvent(working,coordinatorOutcomeEvent(4,'tool-only','turn_failed',{error:'Exact Runtime terminal failure'})).state;
  expect(failed.turnsById['tool-only'].messageIds).toEqual([]);
  expect(failed.turnsById['tool-only'].activityIds).toContain('new-tool');
  expect(failed.turnsById['tool-only'].status).toBe('failed');
  expect(coordinatorLatestTurnOutcome(failed)).toBe('failed');
  const ready=reduceAgentEvent(failed,coordinatorOutcomeEvent(5,'','status_changed',{status:'ready'})).state;
  expect(ready.turnsById.unscoped.messageIds).toEqual([]);
  expect(ready.turnsById.unscoped.activityIds).toEqual([]);
  expect(ready.turnsById.unscoped.status).toBe('completed');
  expect(ready.turnsById.previous.status).toBe('completed');
  expect(coordinatorLatestTurnOutcome(ready)).toBe('failed');
 });
 it('shows a newer source failure rather than the old completion after ready bootstrap', async () => {
  vi.mocked(useAgentLiveSession).mockImplementation(function useSyncedLiveFixture({ sessionId, onSnapshot, onRecoveryState }) {
   useEffect(() => {
    if (!sessionId) return;
    onSnapshot?.({ sessionId,value:{},view:'recent',presentable:true,hydrated:true,sequence:0,resumeToken:'' });
    onRecoveryState?.('synced');
   }, [sessionId]);
   return vi.fn(async () => true);
  });
  const { transport }=setup(undefined,[]);
  const draft=await screen.findByRole('textbox',{name:'Agent 草稿'});
  fireEvent.change(draft,{target:{value:'失败状态保留未发送草稿'}});
  const address=agentSessionAddress(transport,session.id);
  const event=(sequence: number,turnId: string,type: UiAgentEvent['eventType'],payload: Record<string,unknown>) => ({...coordinatorOutcomeEvent(sequence,turnId,type,payload),sessionId:session.id});
  act(() => useAgentLiveStore.getState().applyEvents(address,[
   event(1,'previous','text_delta',{delta:'Previous answer'}),
   event(2,'previous','turn_completed',{status:'completed'}),
  ]));
  const earth=screen.getByRole('button',{name:'打开 Agent 的原 Session'});
  await waitFor(() => expect(earth).toHaveAccessibleDescription('本轮已完成'));
  expect(earth.querySelector('[data-room-planet]')).toHaveAttribute('data-activity','done');
  act(() => useAgentLiveStore.getState().applyEvents(address,[
   event(3,'tool-only','tool_started',{toolCallId:'new-tool',toolName:'bash'}),
   event(4,'tool-only','turn_failed',{error:'Exact Runtime terminal failure'}),
   event(5,'','status_changed',{status:'ready'}),
  ]));
  await waitFor(() => expect(earth).toHaveAccessibleDescription('需要查看'));
  expect(earth.querySelector('[data-room-planet]')).toHaveAttribute('data-activity','error');
  expect(draft).toHaveValue('失败状态保留未发送草稿');
  expect(transport.requests.every(({request}) => request.pathId==='agent.coordinator.ensure')).toBe(true);
  useAgentLiveStore.getState().clear(address);
 });
 it('opens the persisted conversation and actual owned target without starting any model', async () => {
  const { transport,openWindow }=setup(undefined,[object,{ ...object,id:'foreign',sourceSessionId:'other',target:{ ...target,id:'foreign',title:'其他对话' } }]);
  expect(await screen.findByText('持久会话 persistent')).toBeVisible();
  expect(screen.getByText(/持久对话/)).toBeVisible();
  expect(screen.getByText(/全盘访问/)).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'打开 session 核对资料'}));
  expect(openWindow).toHaveBeenCalledWith({ appId:'agent',target:{ kind:'session',id:'owned',title:'核对资料' } });
  expect(screen.queryByText('其他对话')).not.toBeInTheDocument();
  expect(transport.requests.every(({request})=>request.pathId==='agent.coordinator.ensure')).toBe(true);
 });
 it('opens the original source Session from Earth by keyboard and keeps the same draft when returning to Agent', async () => {
  const user = userEvent.setup();
  const { transport, openWindow, setActive } = setup();
  const draft = await screen.findByRole('textbox', { name: 'Agent 草稿' });
  fireEvent.change(draft, { target: { value: '星球返回仍保留的未发送草稿' } });
  const trigger = document.querySelector<HTMLButtonElement>('.paw-coordinator__rail-toggle')!;
  const close = document.querySelector<HTMLButtonElement>('.paw-coordinator__rail-close')!;
  trigger.style.display = 'block'; close.style.display = 'block';
  await user.click(trigger);
  const earth = screen.getByRole('button', { name: '打开 Agent 的原 Session' });
  expect(earth).toBeEnabled();
  expect(earth).toHaveAccessibleDescription('正在同步');
  expect(earth.querySelector('[data-room-planet]')).toHaveAttribute('data-activity', 'static');
  earth.focus();
  await user.keyboard('{Enter}');
  expect(openWindow).toHaveBeenCalledExactlyOnceWith({ appId: 'agent', target: { kind: 'session', id: session.id, title: session.title } });
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  setActive(false); // The existing desktop focuses the opened original Session.
  setActive(true); // Returning focuses the same mounted Agent App.
  expect(screen.getByRole('textbox', { name: 'Agent 草稿' })).toBe(draft);
  expect(draft).toHaveValue('星球返回仍保留的未发送草稿');
  expect(screen.getByText(`持久会话 ${session.id}`)).toBeVisible();
  await user.click(trigger);
  earth.focus();
  await user.keyboard(' ');
  expect(openWindow).toHaveBeenNthCalledWith(2, { appId: 'agent', target: { kind: 'session', id: session.id, title: session.title } });
  expect(transport.requests.every(({ request }) => request.pathId === 'agent.coordinator.ensure')).toBe(true);
  await user.click(trigger);
  fireEvent.keyDown(close, { key: 'Escape' });
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(draft).toHaveValue('星球返回仍保留的未发送草稿');
 });
 it('creates passively with stable retry identity and keeps the unsent coordinator draft', async () => {
  let requests = 0;
  const { transport } = setup(() => { if (!requests++) throw new Error('connection lost'); return {ok:true}; });
  const draft=await screen.findByRole('textbox',{name:'Agent 草稿'});
  fireEvent.change(draft,{target:{value:'尚未发送的原草稿'}});
  fireEvent.click(screen.getByRole('button',{name:'Session'}));
  fireEvent.change(screen.getByRole('textbox',{name:'新控制对象的任务'}),{target:{value:'新增任务'}});
  fireEvent.click(screen.getByRole('button',{name:'创建'}));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button',{name:'创建'}));
  await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  const writes=transport.requests.filter(({request})=>request.pathId==='agent.coordinator.command').map(({request})=>request.body);
  expect(writes).toHaveLength(2);
  expect(writes[0]).toEqual(writes[1]);
  expect(writes[0]).toMatchObject({sourceSessionId:'persistent',action:'create_session',input:{task:'新增任务'}});
  expect(screen.getByRole('textbox',{name:'Agent 草稿'})).toHaveValue('尚未发送的原草稿');
  expect(transport.requests.some(({request})=>request.pathId==='agent.session.prompt')).toBe(false);
 });
 it('restores focus after Escape releases the controls trap', async () => {
  setup(); await screen.findByText('持久会话 persistent');
  // jsdom does not evaluate container queries; expose narrow-screen controls
  // while verifying the real FocusScope release and browser focus effect.
  const trigger=document.querySelector<HTMLButtonElement>('.paw-coordinator__rail-toggle')!;
  const close=document.querySelector<HTMLButtonElement>('.paw-coordinator__rail-close')!;
  trigger.style.display='block'; close.style.display='block';
  fireEvent.click(trigger);
  expect(close).toHaveFocus();
  fireEvent.keyDown(close,{key:'Escape'});
  await waitFor(()=>expect(trigger).toHaveFocus());
  expect(trigger).toHaveAttribute('aria-expanded','false');
 });
 it('selects and rolls back this App display without remounting the persisted conversation or sending', async () => {
  const {transport}=setup();
  const draft=await screen.findByRole('textbox',{name:'Agent 草稿'});
  fireEvent.change(draft,{target:{value:'版本切换保留草稿'}});
  fireEvent.click(screen.getByRole('button',{name:'Agent 显示设置'}));
  fireEvent.click(await screen.findByRole('button',{name:'经典 v1'}));
  expect(document.querySelector('.paw-coordinator')).toHaveAttribute('data-chat-presentation-version','v1');
  expect(screen.getByRole('textbox',{name:'Agent 草稿'})).toBe(draft);
  expect(draft).toHaveValue('版本切换保留草稿');
  fireEvent.click(screen.getByRole('button',{name:'恢复上一显示版本 v2'}));
  expect(document.querySelector('.paw-coordinator')).toHaveAttribute('data-chat-presentation-version','v2');
  expect(draft).toHaveValue('版本切换保留草稿');
  expect(transport.requests.every(({request})=>request.pathId==='agent.coordinator.ensure')).toBe(true);
 });
 it('opens a native history fork as its own Session while retaining the coordinator identity and fork draft', async () => {
  const {transport,openWindow}=setup();
  Object.defineProperty(transport,'connectionIdentity',{value:'coordinator-fork-test'});
  await screen.findByText('持久会话 persistent');
  fireEvent.click(screen.getByRole('button',{name:'测试原生分支回调'}));
  expect(openWindow).toHaveBeenCalledWith({appId:'agent',target:{kind:'session',id:'fork',title:'历史分支'}});
  expect(screen.getByText('持久会话 persistent')).toBeVisible();
  const saved=Object.keys(localStorage).find(key=>key.includes('session%3Afork'));
  expect(saved).toBeDefined();
  expect(JSON.parse(localStorage.getItem(saved!)!)).toMatchObject({draft:'带入分支的草稿'});
 });
 it('keeps controlling rows static while their owner is recovering', async () => {
  setup(); await screen.findByText('持久会话 persistent');
  const row=screen.getByRole('button',{name:'打开 session 核对资料'}).closest('.paw-coordinator__object');
  expect(row).toHaveAttribute('data-control-active','false');
  expect(screen.getAllByText(/正在同步/).length).toBeGreaterThan(0);
  expect(screen.queryByRole('button',{name:'停止'})).not.toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'继续'})).not.toBeInTheDocument();
 });
});
