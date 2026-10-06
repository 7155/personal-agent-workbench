/// <reference types="node" />

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  agentSnapshotFromResponse,
  applyAgentSnapshot,
  createAgentProjection,
  type AgentActivityProjection,
} from '@/contracts/agent-reducer';
import { FxActivityStack, resetActivityDisclosureOverrides } from './ActivitySummary';
import { publicToolResultView } from './public-tool-result';

const fixtureRoot = resolve(process.cwd(), 'e2e/fixtures/minecraft-harness-20260825');

function sessionActivities(role: string, sessionId: string): AgentActivityProjection[] {
  const raw = JSON.parse(readFileSync(resolve(fixtureRoot, `sessions/${role}.snapshot.json`), 'utf8')) as unknown;
  const state = applyAgentSnapshot(
    createAgentProjection(sessionId),
    agentSnapshotFromResponse(raw),
  );
  return state.activityOrder
    .map((id) => state.activitiesById[id])
    .filter((activity): activity is AgentActivityProjection => Boolean(activity));
}

const coordinatorActivities = () => sessionActivities(
  'coordinator',
  'agent:00000000-0000-4000-8000-000000000001',
);
const coreSpecialistActivities = () => sessionActivities(
  'core-specialist',
  'agent:00000000-0000-4000-8000-000000000003',
);

function settledToolActivity(
  activities: AgentActivityProjection[],
  toolName: string,
): AgentActivityProjection {
  const activity = activities.find((candidate) => (
    candidate.kind === 'tool_finished'
    && candidate.payload.toolName === toolName
  ));
  if (!activity) throw new Error(`fixture has no settled ${toolName} tool activity`);
  return activity;
}

function requestValue(view: ReturnType<typeof publicToolResultView>, id: string): string {
  return view.request.find((field) => field.id === id)?.value ?? '';
}

function fieldValue(view: ReturnType<typeof publicToolResultView>, id: string): string {
  return view.fields.find((field) => field.id === id)?.value ?? '';
}

describe('Minecraft harness conversation depth', () => {
  beforeEach(() => {
    resetActivityDisclosureOverrides();
  });

  // Unmount before the environment tears down: an expanded disclosure keeps a
  // bounded finish timer that must not outlive the test's window.
  afterEach(() => {
    cleanup();
  });

  it('expands room_partner into the concrete sent content and publication receipt', () => {
    const view = publicToolResultView(settledToolActivity(coordinatorActivities(), 'room_partner'));
    expect(view.toolLabel).toBe('协作发言');
    expect(view.operation).toBe('post');
    expect(fieldValue(view, 'operation')).toBe('发布协作消息');
    expect(requestValue(view, 'kind')).toBe('成果通报');
    expect(view.outputLabel).toBe('发送内容');
    expect(view.output?.text).toContain('真实浏览器 smoke 通过');
    expect(fieldValue(view, 'published')).toBe('已发布到 Room');
    expect(view.rawResult?.format).toBe('json');
  });

  it('expands a delegation call into task brief, acceptance criteria, and run receipts', () => {
    const view = publicToolResultView(settledToolActivity(coreSpecialistActivities(), 'agents'));
    expect(view.toolLabel).toBe('委派协作');
    expect(fieldValue(view, 'operation')).toBe('委派协作任务');
    expect(requestValue(view, 'agent')).toContain('worker');
    expect(requestValue(view, 'task')).toContain('Bounded registration repair');
    expect(requestValue(view, 'acceptanceCriteria')).toContain('1. ');
    expect(requestValue(view, 'allowedTools')).toContain('工作文档');
    expect(requestValue(view, 'contextMode')).toBe('全新上下文');
    expect(fieldValue(view, 'batchState')).toBe('已完成');
    expect(view.resultItemsLabel).toBe('子任务结果');
    expect(view.resultItems.length).toBeGreaterThan(0);
    expect(view.resultItems[0]?.label).toContain('Bounded registration repair');
    expect(view.resultItems[0]?.text).toContain('执行者');
  });

  it('expands agent_goal into objective, state, and the recorded evidence list', () => {
    const view = publicToolResultView(settledToolActivity(coordinatorActivities(), 'agent_goal'));
    expect(view.toolLabel).toBe('长期目标');
    expect(fieldValue(view, 'operation')).toBe('标记目标完成');
    expect(fieldValue(view, 'objective')).toContain('方块生存游戏');
    expect(fieldValue(view, 'successCriteria')).toContain('第一人称移动');
    expect(fieldValue(view, 'goalState')).toBe('已完成');
    expect(view.resultItemsLabel).toBe('完成证据');
    const testEvidence = view.resultItems.find((item) => item.text.includes('npm test'));
    expect(testEvidence?.label).toBe('测试');
  });

  it('expands work_documents into the authority target and document state', () => {
    const view = publicToolResultView(settledToolActivity(coordinatorActivities(), 'work_documents'));
    expect(view.toolLabel).toBe('工作文档');
    expect(fieldValue(view, 'operation')).toBe('读取权威上下文');
    expect(requestValue(view, 'authorityKind')).toBe('会话目标');
    expect(fieldValue(view, 'documentState')).toBe('已完成');
  });

  it('expands a write receipt into the real written body, not an empty change card', () => {
    const view = publicToolResultView(settledToolActivity(coordinatorActivities(), 'write'));
    expect(view.target).toContain('workdoc_');
    expect(view.outputLabel).toBe('写入内容');
    expect(view.output?.text).toContain('方块生存游戏');
    expect(view.change?.additions).toBeGreaterThan(0);
  });

  it('expands workspace_job into the real command and live job state', () => {
    const view = publicToolResultView(settledToolActivity(coordinatorActivities(), 'workspace_job'));
    expect(view.toolLabel).toBe('后台任务');
    expect(requestValue(view, 'command')).toBe('PORT=4174 node scripts/dev.mjs');
    expect(fieldValue(view, 'jobLabel')).toContain('荒野方境');
    expect(fieldValue(view, 'jobState')).toBe('进行中');
  });

  it('renders fixture steps as a compact tree without raw runtime ids and reveals payload on demand', () => {
    const activities = coordinatorActivities().filter((activity) => (
      activity.kind === 'tool_finished'
    ));
    const { container } = render(<FxActivityStack activities={activities} />);

    expect(container).not.toHaveTextContent('room_partner');
    expect(container).not.toHaveTextContent('agent_goal');
    expect(container).not.toHaveTextContent('work_documents');
    expect(container).not.toHaveTextContent('workspace_job');
    const stackToggle = screen.getByRole('button', {
      name: new RegExp(`全部展开工具与思考步骤，共 ${activities.length} 项`),
    });
    expect(stackToggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(stackToggle);
    expect(screen.getAllByRole('treeitem').length).toBe(activities.length);

    const partnerRow = screen.getByRole('button', { name: /^协作发言，/ });
    expect(partnerRow).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(partnerRow);
    expect(partnerRow).toHaveAttribute('aria-expanded', 'true');

    const partnerDetail = document.getElementById(partnerRow.getAttribute('aria-controls')!)!;
    expect(within(partnerDetail as HTMLElement).getByText('发送内容')).toBeInTheDocument();
    expect(within(partnerDetail as HTMLElement).getByText(/真实浏览器 smoke 通过/)).toBeInTheDocument();
    // Raw JSON stays one level deeper as an explicit disclosure.
    const rawToggle = within(partnerDetail as HTMLElement).getByText('完整返回');
    expect(rawToggle.closest('summary')).toHaveAttribute('aria-expanded', 'false');
  });
});
