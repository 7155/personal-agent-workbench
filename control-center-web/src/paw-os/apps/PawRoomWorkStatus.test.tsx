import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PawRoomWorkStatus } from './PawRoomWorkStatus';
import type { RoomFocusProjection } from './room-focus-projection';
import type { RoomWorkStatus } from './room-work-status';

afterEach(cleanup);
const focus: RoomFocusProjection = {
  goal: { title: '核验版本', description: '', rootId: 'root', state: 'running' },
  partners: [{ participantId: 'mars', sessionId: 's-mars', displayName: '版本复核', celestialName: 'Mars',
    collaborationRole: 'reviewer', state: 'running', currentAction: '读取版本', ownedWorkItemIds: ['a','b'], unread: false }],
  workItems: ['a','b'].map((id) => ({ id, source: 'work-item', objective: `核验${id}`, ownerParticipantId: 'mars',
    state: 'running', acceptanceCriteria: [`要求${id}`], reviewRequired: false, evidence: [], updatedAtMs: 100 })),
  counts: {active:2,review:0,blocked:0,completed:0}, rootEvidence:[],handoffs:[],flow:[],
};
const status: RoomWorkStatus = { state:'running',headline:'1 位伙伴正在执行',detail:'Mars（最终独立复核）· 读取版本',
  animate:true,live:true,updatedAtMs:100,total:2,completed:0,review:0,executingParticipantIds:['mars'],action:'inspect' };
function props() { return {focus,status,onOpenParticipant:vi.fn(),onRetrySync:vi.fn(),onAnswer:vi.fn()}; }

describe('Room composer status dock', () => {
  it('starts compact, with actual counts and no invented percentage', () => {
    render(<PawRoomWorkStatus {...props()} />);
    expect(screen.getByRole('button',{name:'展开任务'})).toHaveAttribute('aria-expanded','false');
    expect(screen.queryByLabelText('任务分派图')).not.toBeInTheDocument();
    expect(screen.getByText('工作项完成 0 / 2')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });
  it('keeps a selected task across folding and streaming updates', async () => {
    const user=userEvent.setup(), p=props();const view=render(<PawRoomWorkStatus {...p} />);
    await user.click(screen.getByRole('button',{name:'展开任务'}));
    const map=screen.getByLabelText('任务分派图');
    await user.click(within(map).getByRole('button',{name:/核验b/}));
    expect(screen.getByLabelText('分工详情')).toHaveTextContent('要求b');
    await user.click(screen.getByRole('button',{name:'收起任务'}));
    view.rerender(<PawRoomWorkStatus {...p} focus={{...focus,workItems:[...focus.workItems].reverse()}} status={{...status,detail:'新回执'}} />);
    await user.click(screen.getByRole('button',{name:'展开任务'}));
    expect(screen.getByLabelText('分工详情')).toHaveTextContent('要求b');
  });
  it('Escape collapses the map and restores the explicit trigger focus', async () => {
    const user=userEvent.setup();render(<PawRoomWorkStatus {...props()} />);
    await user.click(screen.getByRole('button',{name:'展开任务'}));
    fireEvent.keyDown(screen.getByLabelText('任务分派图'),{key:'Escape'});
    expect(screen.getByRole('button',{name:'展开任务'})).toHaveFocus();
    expect(screen.getByLabelText('任务分派图')).not.toBeVisible();
  });
  it('keeps the existing focused control through a state update', async () => {
    const user=userEvent.setup(), p=props();const view=render(<PawRoomWorkStatus {...p} />);
    const trigger=screen.getByRole('button',{name:'展开任务'});trigger.focus();
    view.rerender(<PawRoomWorkStatus {...p} status={{...status,detail:'读取另一文件'}} />);
    expect(trigger).toHaveFocus();await user.click(trigger);
    await user.click(screen.getByRole('button',{name:'查看实际会话与调用'}));
    expect(p.onOpenParticipant).toHaveBeenCalledExactlyOnceWith('mars');
  });
  it('offline uses a read-only resync action and no running animation', async () => {
    const user=userEvent.setup(), p=props();const view=render(<PawRoomWorkStatus {...p} status={{...status,state:'offline',live:false,animate:false,action:'sync',headline:'连接中断'}} />);
    expect(view.container.querySelector('.paw-room-work-status__spin')).toBeNull();
    await user.click(screen.getByRole('button',{name:'重新同步'}));
    expect(p.onRetrySync).toHaveBeenCalledOnce();expect(p.onOpenParticipant).not.toHaveBeenCalled();
  });
  it('routes explicit questions to the existing answer surface', async () => {
    const user=userEvent.setup(), p=props();render(<PawRoomWorkStatus {...p} status={{...status,state:'needs-input',animate:false,action:'answer'}} />);
    await user.click(screen.getByRole('button',{name:'回答问题'}));expect(p.onAnswer).toHaveBeenCalledOnce();
  });
  it('completed history stays inspectable without a spinner', async () => {
    const user=userEvent.setup(), p=props();const view=render(<PawRoomWorkStatus {...p} status={{...status,state:'completed',animate:false,completed:2,headline:'本轮执行已结束'}} />);
    expect(view.container.querySelector('.paw-room-work-status__spin')).toBeNull();
    await user.click(screen.getByRole('button',{name:'展开任务'}));expect(screen.getByLabelText('分工详情')).toHaveTextContent('要求a');
  });
  it('does not crash on an out-of-range optional timestamp',()=>{
    render(<PawRoomWorkStatus {...props()} status={{...status,updatedAtMs:1e20}} />);
    expect(screen.getByText('尚无回执时间')).toBeInTheDocument();
  });
});
