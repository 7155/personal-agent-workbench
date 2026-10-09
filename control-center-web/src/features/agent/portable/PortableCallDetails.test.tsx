import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { PortableCallDetails, observePortableTools } from './PortableCallDetails';

const entry = (id: string) => ({ toolCallId:id, arguments:{op:'open',sourceId:'paper',page:2}, usage:{executed:true}, result:{status:'ok',operation:'open',sources:[{text:'Actual source <img src=x onerror=alert(1)>'}]} });
describe('portable App owner observations', () => {
 afterEach(cleanup);
 it('keeps actual journal parameters/results and does not invent missing usage or progress', () => {
  const pending=observePortableTools({progress:{runtime:{toolCallId:'call-1',toolName:'lab_research',operation:'open',status:'running'}}});
  const tools=observePortableTools({progress:{research:{journal:[entry('call-1')]}}},pending);
  expect(tools).toHaveLength(1); expect(tools[0]).toMatchObject({id:'call-1',executed:true,status:'ok',arguments:{page:2}});
  const {container}=render(<PortableCallDetails snapshot={{progress:{knowledge:{contextChars:1800}}}} tools={tools}/>);
  fireEvent.click(screen.getByText('上下文与用量'));
  expect(screen.getByText('模型用量尚未记录。')).toBeInTheDocument();
  expect(screen.getByText('资料上下文字符').nextElementSibling).toHaveTextContent('1,800');
  expect(screen.queryByText('资料字符上限')).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('实际工具调用 · 1')); fireEvent.click(screen.getByText('lab_research · open')); fireEvent.click(screen.getByText('调用参数')); fireEvent.click(screen.getByText('返回结果'));
  expect(container.textContent).toContain('"page": 2'); expect(container.textContent).toContain('Actual source <img'); expect(container.querySelector('img')).toBeNull();
  expect(screen.getByText('已返回')).toBeInTheDocument();
 });
 it('separates reported Token usage from source budgets without computing a fake total', () => {
  render(<PortableCallDetails snapshot={{result:{model:{provider:'owner',model:'actual-model',thinkingLevel:'high'},usage:{inputTokens:120,outputTokens:20,cacheReadTokens:40,totalTokens:150},research:{budget:{contextChars:0,maxContextChars:24000}}}}} tools={[]}/>);
  expect(screen.getByText('模型：owner / actual-model · high')).toBeInTheDocument();
  expect(screen.getByText('记录的总 Token').nextElementSibling).toHaveTextContent('150');
  expect(screen.getByText('资料上下文字符').nextElementSibling).toHaveTextContent('0');
  expect(screen.getByText('资料字符上限').nextElementSibling).toHaveTextContent('24,000');
  expect(screen.queryByText(/模型用量尚未记录/)).not.toBeInTheDocument();
 });
 it('retains opened tool details on progress and progressively reveals remaining actual calls', () => {
  const tools=observePortableTools({progress:{research:{journal:Array.from({length:8},(_,i)=>entry(`call-${i}`))}}});
  const view=render(<PortableCallDetails snapshot={{}} tools={tools}/>);
  expect(view.container.querySelectorAll('[data-tool-call-id]')).toHaveLength(6);
  const first=view.container.querySelector<HTMLDetailsElement>('[data-tool-call-id="call-0"]')!;first.open=true;
  fireEvent.click(screen.getByRole('button',{name:'显示其余 2 次调用'}));
  expect(view.container.querySelectorAll('[data-tool-call-id]')).toHaveLength(8);
  const next=observePortableTools({progress:{runtime:{toolCallId:'call-0',toolName:'lab_research',status:'running'}}},tools);
  view.rerender(<PortableCallDetails snapshot={{progress:{stage:'completed'}}} tools={next}/>);
  expect(view.container.querySelector('[data-tool-call-id="call-0"]')).toBe(first);expect(first.open).toBe(true);
  expect(next[0].status).toBe('ok');
 });
 it('does not replace recorded empty results with a later running observation', () => {
  for (const result of [null, 0, false, '']) {
   const previous=[{id:'call-final',name:'lab_research',operation:'open',status:'ok',result}];
   const next=observePortableTools({progress:{runtime:{toolCallId:'call-final',toolName:'lab_research',status:'running'}}},previous);
   expect(next[0]).toEqual(previous[0]);
  }
 });
 it('keeps runtime-only status as the last observation rather than proving physical completion', () => {
  const tools=observePortableTools({progress:{runtime:{toolCallId:'call-1',toolName:'lab_research',operation:'open',status:'running'}}});
  render(<PortableCallDetails snapshot={{progress:{stage:'completed'}}} tools={tools}/>);
  expect(screen.getByText('最近进度：进行中')).toBeInTheDocument();expect(screen.getByText('尚未收到返回结果。')).toBeInTheDocument();
  expect(screen.queryByText('已返回')).not.toBeInTheDocument();
 });
});
