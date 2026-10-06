/// <reference types="node" />

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import {
  agentSnapshotFromResponse,
  applyAgentSnapshot,
  createAgentProjection,
  type AgentActivityProjection,
} from '@/contracts/agent-reducer';
import { FxActivityStack, resetActivityDisclosureOverrides } from './ActivitySummary';
import { publicToolResultView } from './public-tool-result';
import { routeDecisionPlanView } from './route-decision-plan';

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

function settledToolActivity(
  activities: AgentActivityProjection[],
  toolName: string,
  match?: (activity: AgentActivityProjection) => boolean,
): AgentActivityProjection {
  const activity = activities.find((candidate) => (
    candidate.kind === 'tool_finished'
    && candidate.payload.toolName === toolName
    && (!match || match(candidate))
  ));
  if (!activity) throw new Error(`fixture has no settled ${toolName} tool activity`);
  return activity;
}

function routeDecisionEvents(): Array<Record<string, unknown>> {
  return readFileSync(resolve(fixtureRoot, 'room/history.jsonl'), 'utf8')
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line) as Record<string, unknown>)
    .filter((event) => event.eventType === 'route_decision');
}

describe('Minecraft harness collaboration flow', () => {
  beforeEach(() => {
    resetActivityDisclosureOverrides();
  });

  it('projects a real route_decision into a concrete dispatch plan', () => {
    const events = routeDecisionEvents();
    expect(events.length).toBeGreaterThan(0);
    const delegated = events.find((event) => (
      (event.payload as Record<string, unknown>).reason === 'partner_delegate'
    ));
    expect(delegated).toBeDefined();

    const plan = routeDecisionPlanView(delegated);
    expect(plan).not.toBeNull();
    expect(plan!.targetName).toBe('Agent 2');
    expect(plan!.reasonLabel).toBe('伙伴委派');
    expect(plan!.policyLabel).toBe('并行分派');
    expect(plan!.parallelLabel).toBe('并行 1 / 2');
    expect(plan!.phaseName).toContain('并行实现');
    expect(plan!.workItemLabel).toContain('任务 #');
    expect(plan!.candidates).toHaveLength(4);

    const selected = plan!.candidates.filter((candidate) => candidate.selected);
    expect(selected).toHaveLength(1);
    expect(selected[0]!.name).toBe('Agent 2');
    expect(selected[0]!.score).toBe(1);
    expect(selected[0]!.signalLabels).toContain('点名邀请');
    for (const candidate of plan!.candidates) {
      expect(candidate.score).toBeGreaterThanOrEqual(0);
      expect(candidate.score).toBeLessThanOrEqual(1);
    }
  });

  it('returns null for payloads that do not carry the route-decision schema', () => {
    expect(routeDecisionPlanView({ toolName: 'room_partner' })).toBeNull();
    expect(routeDecisionPlanView(undefined)).toBeNull();
  });

  it('expands peer_list into the real partner-to-partner intercom exchange', () => {
    const activities = sessionActivities('ui-specialist', 'agent:00000000-0000-4000-8000-000000000004');
    const view = publicToolResultView(settledToolActivity(activities, 'room_partner', (candidate) => (
      (candidate.payload.result as Record<string, unknown> | undefined)?.operation === 'peer_list'
    )));
    expect(view.resultItemsLabel).toBe('伙伴消息');
    expect(view.resultItems.length).toBeGreaterThan(0);
    const accepted = view.resultItems.find((item) => item.label.includes('验收通过'));
    expect(accepted).toBeDefined();
    expect(accepted!.label).toMatch(/伙伴 #[0-9a-z]{4} → 伙伴 #[0-9a-z]{4}/u);
    expect(accepted!.text).toContain('已通过验收');
    expect(accepted!.text).toContain('已送达');
  });

  it('expands peer_reply into the concrete replied content', () => {
    const activities = sessionActivities('core-specialist', 'agent:00000000-0000-4000-8000-000000000003');
    const view = publicToolResultView(settledToolActivity(activities, 'room_partner', (candidate) => (
      (candidate.payload.result as Record<string, unknown> | undefined)?.operation === 'peer_reply'
    )));
    expect(view.summary).toContain('回复伙伴消息');
    expect(view.outputLabel).toBe('回复内容');
    expect(view.output?.text).toContain('复核结论');
  });

  it('labels an aborted delegation batch with its real op and abort state', () => {
    const activities = sessionActivities('ui-specialist', 'agent:00000000-0000-4000-8000-000000000004');
    const abortView = publicToolResultView(settledToolActivity(activities, 'agents', (candidate) => (
      ((candidate.payload.result as Record<string, unknown> | undefined)?.schemaVersion)
        === 'rag-ime.agent-delegation-abort.v1'
    )));
    expect(abortView.toolLabel).toBe('委派协作');
    // The durable abort receipt still reports the batch as running until the
    // child actually stops; the pending abort must stay visible.
    expect(abortView.fields.find((field) => field.id === 'abortRequested')?.value)
      .toBe('已发出，等待子 Agent 停止');
    expect(abortView.resultItemsLabel).toBe('子任务结果');
    expect(abortView.resultItems[0]?.title).toContain('TaskBrief');
    expect(abortView.resultItems[0]?.text).toBe('进行中 · 研究员');
  });

  it('renders a route_decision activity as an expandable dispatch plan', () => {
    const delegated = routeDecisionEvents().find((event) => (
      (event.payload as Record<string, unknown>).reason === 'partner_delegate'
    ))!;
    const activity: AgentActivityProjection = {
      id: 'route:test',
      turnId: 'turn:test',
      kind: 'route_decision',
      status: 'completed',
      summary: '已确认本轮分工',
      payload: delegated.payload as Record<string, unknown>,
      createdAtMs: Number(delegated.createdAtMs) || 1,
      updatedAtMs: Number(delegated.createdAtMs) || 1,
    };
    render(<FxActivityStack activities={[activity]} />);

    const row = screen.getByRole('button', { name: /^分派决定，/ });
    expect(row).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(row);
    expect(row).toHaveAttribute('aria-expanded', 'true');

    const detail = document.getElementById(row.getAttribute('aria-controls')!)!;
    const plan = within(detail as HTMLElement).getByRole('list', { name: '候选伙伴与权重' });
    expect(within(detail as HTMLElement).getByText('分派给 Agent 2')).toBeInTheDocument();
    expect(within(detail as HTMLElement).getByText('并行 1 / 2')).toBeInTheDocument();
    expect(within(plan).getAllByRole('listitem')).toHaveLength(4);
    expect(within(plan).getByText('点名邀请')).toBeInTheDocument();
    // The raw routing payload never leaks as an id wall.
    expect(detail.textContent).not.toContain('participant:');
    expect(detail.textContent).not.toContain('room-dispatch:');
  });
});
