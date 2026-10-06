import { describe, expect, it } from 'vitest';
import { sessionTaskStateLabel, type SessionTaskState } from './PawSessionTaskbar';
const idle: SessionTaskState = { busy: false, stopping: false, paused: false, pending: false, waiting: false, disconnected: false, error: false };
describe('task state is a projection, not an inferred completion', () => {
  it('keeps an ended turn distinct from its unfinished goal', () => {
    expect(sessionTaskStateLabel({...idle, turnStatus: 'completed', goal: {configured: true, objective: 'inspect', status: 'active'}})).toBe('本轮已结束 · 任务未完成');
    expect(sessionTaskStateLabel({...idle, turnStatus: 'completed'})).toBe('本轮已结束');
  });
  it('requires the owning goal state to report task completion', () => {
    expect(sessionTaskStateLabel({...idle, goal: {configured: true, objective: 'inspect', status: 'completed'}})).toBe('任务已完成');
    expect(sessionTaskStateLabel({...idle, goal: {configured: false, objective: '', status: 'completed'}})).toBe('可以继续对话');
  });
  it('does not call a Stop request or a failed turn successful', () => {
    expect(sessionTaskStateLabel({...idle, busy: true, stopping: true})).toBe('正在请求停止');
    expect(sessionTaskStateLabel({...idle, turnStatus: 'aborted'})).toBe('本轮已停止');
    expect(sessionTaskStateLabel({...idle, turnStatus: 'failed'})).toBe('本轮未完成');
  });
  it('marks stale state and waiting input before displaying live execution', () => {
    expect(sessionTaskStateLabel({...idle, disconnected: true, busy: true})).toBe('连接恢复中 · 上次状态');
    expect(sessionTaskStateLabel({...idle, waiting: true, busy: true})).toBe('等待你处理');
    expect(sessionTaskStateLabel({...idle, pending: true})).toBe('等待响应');
  });
});
