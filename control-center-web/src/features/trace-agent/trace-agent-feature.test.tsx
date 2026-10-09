import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import type { ControlRequest } from '@/platform/transport';
import { ControlTransportHttpError } from '@/platform/http-transport';
import type { TraceDiagnosticReportV1 } from '@/contracts/generated/trace-diagnostic-report.v1';
import type { TraceDiagnosticReportListV1 } from '@/contracts/generated/trace-diagnostic-report-list.v1';
import {
  TRACE_AGENT_DIAGNOSTIC_MAX_POLL_DURATION_MS,
  TRACE_AGENT_SKILL_REF,
  TraceAgentFeature,
} from './index';
import { buildTraceAgentHandoffRoute } from './handoff';
import { clearTraceDiagnosticStart } from './diagnostic-start';
import traceAgentCss from './trace-agent.css?raw';

afterEach(() => {
  vi.useRealTimers();
  window.pawBrowserHost = undefined;
  clearTraceDiagnosticStart();
  cleanup();
});

describe('TraceAgentFeature', () => {
  it('retains visited library drafts and capability filters without starting an execution', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'observability.traceOptimization.library': { ok: true, projects: [], projectId: '', patterns: [], truncated: false },
      'observability.traceOptimization.capabilities': { ok: true, items: [], unavailable: [] },
    } });
    renderFeature(transport, [], ['/trace-agent?view=knowledge']);
    const search = await screen.findByRole('searchbox', { name: '查找经验' });
    await user.type(search, '还未提交的查找');
    const navigation = screen.getByRole('navigation', { name: 'Trace Agent 应用导航' });
    await user.click(within(navigation).getByRole('button', { name: '能力库' }));
    const kind = await screen.findByRole('combobox', { name: '能力类型' });
    await user.selectOptions(kind, 'tool');
    await user.selectOptions(screen.getByRole('combobox', { name: '版本状态' }), 'candidate');
    await user.click(within(navigation).getByRole('button', { name: '经验库' }));
    expect(screen.getByRole('searchbox', { name: '查找经验' })).toBe(search);
    expect(search).toHaveValue('还未提交的查找');
    await user.click(within(navigation).getByRole('button', { name: '能力库' }));
    expect(screen.getByRole('combobox', { name: '能力类型' })).toBe(kind);
    expect(kind).toHaveValue('tool');
    expect(screen.getByRole('combobox', { name: '版本状态' })).toHaveValue('candidate');
    expect(transport.requests.every(({ request }) => request.pathId === 'observability.traceOptimization.library'
      || request.pathId === 'observability.traceOptimization.capabilities')).toBe(true);
  });

  it('opens an independent workspace and preserves the new-task draft across app views', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    renderFeature(transport, [], ['/trace-agent']);
    expect(await screen.findByRole('heading', { name: '工作台' })).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Trace Agent 应用导航' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '开始诊断' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '新建优化任务' }));
    const objective = await screen.findByRole('textbox', { name: '希望改善的结果（可选）' });
    await user.type(objective, '减少 Skill 重复重试');
    await user.click(screen.getByRole('button', { name: '工作台' }));
    expect(screen.queryByRole('textbox', { name: '希望改善的结果（可选）' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '新建任务' }));
    expect(screen.getByRole('textbox', { name: '希望改善的结果（可选）' })).toHaveValue('减少 Skill 重复重试');
  });

  it('freezes the selected Skill-only distillation intent into the report request and actual prompt', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    renderFeature(transport, []);
    const settings = await screen.findByRole('region', { name: '本次任务设置' });
    expect(within(settings).getByRole('checkbox', { name: '全部' })).toBeChecked();
    await user.click(within(settings).getByRole('radio', { name: /从对话沉淀方法/ }));
    await user.click(within(settings).getByRole('checkbox', { name: '全部' }));
    expect(screen.getByRole('button', { name: '开始诊断' })).toBeDisabled();
    await user.click(within(settings).getByRole('checkbox', { name: 'Skill' }));
    await user.type(within(settings).getByRole('textbox'), '总结可以复用的排障方法');
    await user.click(screen.getByRole('tab', { name: 'Room 协作' }));
    await user.click(within(await screen.findByRole('listitem', { name: /失败的协作/ })).getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: '开始诊断' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(true));
    const create = transport.requests.find(({ request }) => request.pathId === 'observability.traceDiagnosticReports.create')!.request;
    expect(create.body).toMatchObject({ intent: { mode: 'distill', scopeMode: 'selected', focusAreas: ['skill'], objective: '总结可以复用的排障方法' }, targets: [{ kind: 'session', id: 'session-source' }, { kind: 'room', id: 'room-source' }] });
    const prompt = String((transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')!.request.body as Record<string, unknown>).message);
    expect(prompt).toContain('"mode":"distill","scopeMode":"selected","focusAreas":["skill"]');
    expect(prompt).toContain('不得修改未选方向');
    expect(prompt).toContain('总结可以复用的排障方法');
    expect(within(settings).getByRole('checkbox', { name: 'Skill' })).toBeDisabled();
  });

  it('keeps an explicitly cleared source selection empty across refresh and tab changes', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    renderFeature(transport, []);
    const source = await screen.findByRole('checkbox', { name: '选择 失败的对话' });
    await waitFor(() => expect(source).toBeChecked());
    await user.click(source);
    expect(source).not.toBeChecked();
    expect(screen.getByRole('button', { name: '开始诊断' })).toBeDisabled();

    const readsBefore = transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.list').length;
    await user.click(screen.getByRole('button', { name: '刷新对象' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.list').length).toBeGreaterThan(readsBefore));
    expect(source).not.toBeChecked();
    await user.click(screen.getByRole('tab', { name: 'Room 协作' }));
    const room = within(await screen.findByRole('listitem', { name: /失败的协作/ })).getByRole('checkbox');
    expect(room).not.toBeChecked();
    expect(screen.getByRole('button', { name: '开始诊断' })).toBeDisabled();
    await user.click(room);
    await user.click(screen.getByRole('button', { name: '开始诊断' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'observability.traceDiagnosticReports.create')).toBe(true));
    const request = transport.requests.find(({ request }) => request.pathId === 'observability.traceDiagnosticReports.create')!.request;
    expect((request.body as { targets: { kind: string; id: string }[] }).targets.map(({ kind, id }) => ({ kind, id }))).toEqual([{ kind: 'room', id: 'room-source' }]);
  });

  it('carries the exact saved diagnostic report into Lab without starting another run', async () => {
    const reportId = `trace-report:${'c'.repeat(32)}`;
    const routes: string[] = [];
    const transport = traceAgentTransport({ diagnosticReport: persistedTraceReportFixture(reportId, 'completed') });
    renderFeature(transport, routes, [`/trace-agent?reportId=${encodeURIComponent(reportId)}`]);
    await screen.findByRole('region', { name: 'Trace 诊断网页报告' });
    await userEvent.setup().click(screen.getByRole('button', { name: '在 Lab 中批量验证' }));
    expect(routes).toContain(`/eval-lab?traceReportId=${encodeURIComponent(reportId)}`);
    expect(transport.requests.some(({ request }) => ['agent.rooms.create', 'agent.session.prompt'].includes(request.pathId))).toBe(false);
  });

  it('keeps inline transcript expansion targets at least 24px high', () => {
    expect(traceAgentCss).toMatch(/\.trace-agent-timeline__title > button\s*\{[^}]*min-height:\s*24px;/s);
  });

  it('preselects an incoming failure handoff but fails closed without canonical project authority', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    const route = buildTraceAgentHandoffRoute({
      kind: 'memory',
      entityId: 'memory-maintenance:job-1',
      title: '记忆整理失败',
      summary: '读取或保存失败',
      error: 'invalid Pi Runtime Host JSONL: Unterminated string',
      failureRef: 'memory-maintenance:job-1',
      sourceRoute: '/memory?view=activity',
      refs: { phase: 'managed_memory_model' },
      occurredAtMs: 123,
    });
    const routes: string[] = [];
    renderFeature(transport, routes, [route]);

    const selected = await screen.findByRole('region', { name: '已选择诊断对象' });
    expect(selected).toHaveTextContent('记忆整理失败');
    expect(screen.getByTestId('trace-agent-incoming-handoff')).toHaveTextContent('memory-maintenance:job-1');
    await user.click(within(selected).getByRole('button', { name: '回到原位置' }));
    expect(routes).toContain('/memory?view=activity');

    const action = await screen.findByTestId('trace-agent-diagnostic-action');
    expect(action).toHaveTextContent('binding_required');
    expect(within(action).getByRole('button', { name: '开始诊断' })).toBeDisabled();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('keeps the canonical Session project binding while granting full-trust diagnostic and repair access', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    const route = buildTraceAgentHandoffRoute({
      kind: 'session',
      entityId: 'session-source',
      sessionId: 'session-source',
      title: '原 Session 执行失败',
      summary: '请 Trace 诊断并修复。',
      sourceRoute: '/agent?session=session-source',
      workspaceRoots: ['/untrusted/url/root'],
      occurredAtMs: 123,
    });
    renderFeature(transport, [], [route]);

    expect(await screen.findByRole('region', { name: '已选择诊断对象' })).toHaveTextContent('原 Session 执行失败');
    await user.click(screen.getByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));

    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    const sessionCreates = transport.requests
      .filter(({ request }) => request.pathId === 'agent.sessions.create')
      .map(({ request }) => request);
    expect(sessionCreates).toHaveLength(2);
    for (const request of sessionCreates) {
      expect(request.body).toMatchObject({
        executionMode: 'full_trust',
        workspaceRoots: ['/workspace/paw'],
      });
      expect(request.body).not.toMatchObject({ workspaceRoots: ['/'] });
      expect(request.body).not.toMatchObject({ workspaceRoots: ['/untrusted/url/root'] });
    }
    const modeUpdates = transport.requests
      .filter(({ request }) => request.pathId === 'agent.session.mode.update')
      .map(({ request }) => request);
    expect(modeUpdates).toHaveLength(2);
    for (const request of modeUpdates) {
      expect(request.body).toMatchObject({
        executionMode: 'full_trust',
        workspaceRoots: ['/workspace/paw'],
      });
    }
    const repairPromptRequest = transport.requests
      .filter(({ request }) => request.pathId === 'agent.session.prompt')
      .at(-1)?.request;
    const repairPrompt = String((repairPromptRequest?.body as Record<string, unknown> | undefined)?.message);
    expect(repairPrompt).toMatch(
      /"repairSessionPolicy":\s*\{[\s\S]*?"workspaceRoots":\s*\[\s*"\/workspace\/paw"\s*\]/,
    );
  });

  it('keeps the canonical Room project binding while granting full-trust repair access', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({
      rooms: [{
        id: 'room-source',
        title: '失败的协作',
        status: 'active',
        updatedAtMs: 110,
        participantCount: 3,
        workspaceRoots: ['/workspace/room'],
      }],
    });
    const route = buildTraceAgentHandoffRoute({
      kind: 'room',
      entityId: 'room-source',
      roomId: 'room-source',
      title: 'Room 协作失败',
      summary: '请 Trace 诊断并修复。',
      sourceRoute: '/rooms?room=room-source',
      workspaceRoots: ['/untrusted/url/root'],
      occurredAtMs: 123,
    });
    renderFeature(transport, [], [route]);

    expect(await screen.findByRole('region', { name: '已选择诊断对象' })).toHaveTextContent('Room 协作失败');
    await user.click(screen.getByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));

    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    const repairCreate = transport.requests
      .filter(({ request }) => request.pathId === 'agent.sessions.create')
      .at(-1)?.request;
    expect(repairCreate?.body).toMatchObject({
      executionMode: 'full_trust',
      workspaceRoots: ['/workspace/room'],
    });
    expect(repairCreate?.body).not.toMatchObject({ workspaceRoots: ['/'] });
    expect(repairCreate?.body).not.toMatchObject({ workspaceRoots: ['/untrusted/url/root'] });
  });

  it('inherits a Run canonical Session project binding without weakening full-trust authority', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    const route = buildTraceAgentHandoffRoute({
      kind: 'runtime',
      entityId: 'run-source',
      runId: 'run-source',
      title: '运行失败',
      summary: '从运行记录进入诊断。',
      sourceRoute: '/observability?runId=run-source',
      occurredAtMs: 123,
    });
    renderFeature(transport, [], [route]);

    expect(await screen.findByRole('region', { name: '已选择诊断对象' })).toHaveTextContent('run-source');
    await user.click(screen.getByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));

    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    const repairCreate = transport.requests
      .filter(({ request }) => request.pathId === 'agent.sessions.create')
      .at(-1)?.request;
    expect(repairCreate?.body).toMatchObject({
      executionMode: 'full_trust',
      workspaceRoots: ['/workspace/paw'],
    });
    expect(repairCreate?.body).not.toMatchObject({ workspaceRoots: ['/'] });
  });

  it('inherits a Memory run canonical Session project binding without trusting URL paths', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    const route = buildTraceAgentHandoffRoute({
      kind: 'memory',
      entityId: 'memory-maintenance:job-1',
      runId: 'run-source',
      title: '记忆维护运行失败',
      summary: '维护运行写入失败。',
      sourceRoute: '/memory?view=activity',
      workspaceRoots: ['/untrusted/url/root'],
      occurredAtMs: 123,
    });
    renderFeature(transport, [], [route]);

    expect(await screen.findByRole('region', { name: '已选择诊断对象' })).toHaveTextContent('记忆维护运行失败');
    await user.click(screen.getByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));

    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    const repairCreate = transport.requests
      .filter(({ request }) => request.pathId === 'agent.sessions.create')
      .at(-1)?.request;
    expect(repairCreate?.body).toMatchObject({
      executionMode: 'full_trust',
      workspaceRoots: ['/workspace/paw'],
    });
    expect(repairCreate?.body).not.toMatchObject({ workspaceRoots: ['/'] });
    expect(repairCreate?.body).not.toMatchObject({ workspaceRoots: ['/untrusted/url/root'] });
  });

  it('fails closed with binding_required and lets the user bind the source project explicitly', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({
      pickedWorkspaceRoot: '/workspace/selected-project',
      sessions: [{
        id: 'session-source',
        title: '未绑定项目的对话',
        mode: 'assistant',
        status: 'idle',
        updatedAtMs: 100,
        workspaceRoots: [],
        messageCount: 4,
        lastMessagePreview: 'write/edit validation error',
        executionMode: 'per_action',
        toolProfileVersion: 'control-center-v1',
      }],
    });
    renderFeature(transport, []);

    const action = await screen.findByTestId('trace-agent-diagnostic-action');
    expect(action).toHaveTextContent('binding_required');
    expect(within(action).getByRole('button', { name: '开始诊断' })).toBeDisabled();
    await user.click(within(action).getByRole('button', { name: '选择项目' }));

    await waitFor(() => expect(transport.filePickCalls).toEqual([{
      purpose: 'workspace-root',
      selection: 'directory',
      multiple: true,
      maxFiles: 4,
    }]));
    const bindingRequest = transport.requests.find(({ request }) => (
      request.pathId === 'agent.session.mode.update'
      && request.params?.sessionId === 'session-source'
    ));
    expect(bindingRequest?.request.body).toMatchObject({
      mode: 'coordinator',
      workspaceRoots: ['/workspace/selected-project'],
    });
    await waitFor(() => expect(within(action).getByRole('button', { name: '开始诊断' })).toBeEnabled());
    await user.click(within(action).getByRole('button', { name: '开始诊断' }));
    const diagnosticCreate = transport.requests.find(({ request }) => (
      request.pathId === 'agent.sessions.create'
      && (request.body as Record<string, unknown> | undefined)?.surfaceKey === 'diagnostic'
    ));
    expect(diagnosticCreate?.request.body).toMatchObject({
      workspaceRoots: ['/workspace/selected-project'],
    });
    expect(diagnosticCreate?.request.body).not.toMatchObject({ workspaceRoots: ['/'] });
  });

  it('binds an unbound source through the installed Electron host directory picker', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({
      sessions: [{
        id: 'session-electron-source',
        title: '安装态未绑定项目的对话',
        mode: 'assistant',
        status: 'idle',
        updatedAtMs: 100,
        workspaceRoots: [],
        messageCount: 4,
        lastMessagePreview: 'memory maintenance failed',
        executionMode: 'per_action',
        toolProfileVersion: 'control-center-v1',
      }],
    });
    Object.defineProperty(transport, 'pickFiles', { configurable: true, value: undefined });
    const pickWorkspaceDirectory = vi.fn(async () => ({
      name: 'personal-agent-workbench',
      path: '/workspace/personal-agent-workbench',
    }));
    window.pawBrowserHost = {
      kind: 'electron-webview',
      partition: 'persist:paw-browser',
      pickWorkspaceDirectory,
    } as unknown as NonNullable<typeof window.pawBrowserHost>;
    renderFeature(transport, []);

    const action = await screen.findByTestId('trace-agent-diagnostic-action');
    await user.click(within(action).getByRole('button', { name: '选择项目' }));

    await waitFor(() => expect(pickWorkspaceDirectory).toHaveBeenCalledTimes(1));
    expect(transport.requests.find(({ request }) => (
      request.pathId === 'agent.session.mode.update'
      && request.params?.sessionId === 'session-electron-source'
    ))?.request.body).toMatchObject({
      mode: 'coordinator',
      workspaceRoots: ['/workspace/personal-agent-workbench'],
    });
    await waitFor(() => expect(within(action).getByRole('button', { name: '开始诊断' })).toBeEnabled());
  });

  it('fails closed when a Run has no canonical Session workspace binding', async () => {
    const user = userEvent.setup();
    const unbound = runObservationSnapshot([
      observationEvent(1, '', ''),
    ], false);
    const transport = traceAgentTransport({ observationSource: unbound });
    const route = buildTraceAgentHandoffRoute({
      kind: 'runtime',
      entityId: 'run-source',
      runId: 'run-source',
      title: '无 Session 绑定的运行',
      summary: '只有运行证据。',
      sourceRoute: '/observability?runId=run-source',
      occurredAtMs: 123,
    });
    renderFeature(transport, [], [route]);

    expect(await screen.findByRole('region', { name: '已选择诊断对象' })).toHaveTextContent('run-source');
    const action = await screen.findByTestId('trace-agent-diagnostic-action');
    expect(action).toHaveTextContent('binding_required');
    expect(within(action).getByRole('button', { name: '开始诊断' })).toBeDisabled();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.create')).toBe(false);
  });

  it('keeps a handoff-only input visible but blocks diagnosis without canonical project authority', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    const route = buildTraceAgentHandoffRoute({
      kind: 'memory',
      entityId: 'memory-maintenance:job-only',
      title: '记忆维护失败',
      summary: '只有失败交接包，没有可用的 Session、Room 或 Run。',
      error: '读取或保存失败',
      failureRef: 'memory-maintenance:job-only',
      sourceRoute: '/memory?view=activity',
      refs: { phase: 'managed_memory_model' },
      occurredAtMs: 123,
    });
    renderFeature(transport, [], [route]);

    const selected = await screen.findByRole('region', { name: '已选择诊断对象' });
    expect(selected).toHaveTextContent('仅结构化交接包');
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'observability.snapshot' && request.query?.runId === 'memory-maintenance:job-only'
    ))).toBe(false));

    const action = screen.getByTestId('trace-agent-diagnostic-action');
    expect(action).toHaveTextContent('binding_required');
    expect(within(action).getByRole('button', { name: '开始诊断' })).toBeDisabled();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('uses the newly selected target Trace after leaving an incoming handoff', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    const route = buildTraceAgentHandoffRoute({
      kind: 'session',
      entityId: 'session-handoff',
      title: '旧交接对象',
      summary: '从旧对象进入 Trace Agent。',
      sessionId: 'session-handoff',
      traceId: 'trace:handoff',
      sourceRoute: '/agent?session=session-handoff',
      occurredAtMs: 123,
    });
    renderFeature(transport, [], [route]);

    await user.click(await screen.findByRole('listitem', { name: /失败的对话/ }));
    await user.click(screen.getByRole('button', { name: '开始诊断' }));

    const promptRequest = await waitFor(() => {
      const request = transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')?.request;
      expect(request).toBeTruthy();
      return request!;
    });
    const prompt = String((promptRequest.body as Record<string, unknown> | undefined)?.message);
    expect(prompt).toContain('trace:source');
    expect(prompt).not.toContain('trace:handoff');
  });

  it('keeps normal correction and validation prose in the timeline, not failure evidence', async () => {
    const normal = '此前保存失败，请验证原记录；error/fail/timeout 是要核对的内容';
    const observation = observationSnapshot();
    const transport = traceAgentTransport({
      observationSource: {
        ...observation,
        items: [{ ...observation.items[0], status: 'completed', name: 'validation', summary: '验证已完成' }],
      },
      sourceSnapshot: {
        ...sessionSourceSnapshot(),
        items: ['user', 'assistant'].map((role) => ({
          id: `normal-${role}`, role, status: 'completed', createdAtMs: 150,
          summary: normal,
          blocks: [{ id: `${role}-text`, type: 'text', status: 'completed', data: { text: normal } }],
        })),
        liveEvents: [
          { eventId: 'normal-tool', eventType: 'tool_finished', status: 'completed', createdAtMs: 155, payload: { summary: '验证已完成' } },
          { eventId: 'unknown-event', eventType: 'participant_activity', createdAtMs: 160, payload: { summary: '失败是否发生仍未知' } },
          { eventId: 'stopped-event', eventType: 'turn_completed', status: 'aborted', createdAtMs: 165, payload: { summary: '已停止；未继续验证' } },
        ],
      },
    });
    renderFeature(transport, []);
    const timeline = await screen.findByRole('region', { name: '原始对话时间线' });
    await waitFor(() => expect(timeline).toHaveTextContent(normal));
    const evidence = screen.getByRole('region', { name: '已读取的失败证据' });
    expect(evidence).toHaveTextContent('当前没有已投影的失败明细');
    expect(evidence).not.toHaveTextContent(normal);
    expect(evidence.querySelectorAll('article')).toHaveLength(0);
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('retains explicit failed and error receipts without requiring failure words', async () => {
    const transport = traceAgentTransport({
      sourceSnapshot: {
        ...sessionSourceSnapshot(),
        items: [
          { id: 'failed-direct', role: 'assistant', status: 'failed', createdAtMs: 150, summary: '原请求 A 未返回' },
          { id: 'explicit-error', role: 'assistant', status: 'completed', createdAtMs: 151, errorMessage: '服务回执 B' },
          { id: 'failed-tool', role: 'tool', status: 'failed', createdAtMs: 152,
            blocks: [{ id: 'tool-result', type: 'tool_result', status: 'failed', data: { content: '原工具回执 C' } }] },
          { id: 'error-block', role: 'assistant', status: 'completed', createdAtMs: 153,
            blocks: [{ id: 'error', type: 'error', status: 'completed', data: { message: '原运行回执 D' } }] },
        ],
        liveEvents: [
          { eventId: 'failed-event', eventType: 'turn_failed', createdAtMs: 154, payload: { summary: '原终态回执 E' } },
          { eventId: 'payload-failed', eventType: 'tool_finished', createdAtMs: 155, payload: { status: 'failed', summary: '原工具回执 F' } },
          { eventId: 'explicit-event-error', eventType: 'participant_activity', createdAtMs: 156, payload: { errorMessage: '服务回执 G' } },
        ],
      },
    });
    renderFeature(transport, []);
    const evidence = await screen.findByRole('region', { name: '已读取的失败证据' });
    await waitFor(() => expect(evidence).toHaveTextContent('原请求 A 未返回'));
    for (const text of ['服务回执 B', '原工具回执 C', '原运行回执 D', '原终态回执 E', '原工具回执 F', '服务回执 G']) {
      expect(evidence).toHaveTextContent(text);
    }
    expect(evidence).toHaveTextContent('write/edit validation error');
    const unknown = within(evidence).getByText('服务回执 G').closest('article');
    expect(unknown?.querySelector('.trace-agent-evidence__dot')).toHaveClass('trace-agent-evidence__dot--participant_activity');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.session.prompt')).toBe(false);
  });

  it('shows transcript and Trace failure evidence, then starts a full-trust Skill-bound diagnostic Session with unrestricted reads', async () => {
    const user = userEvent.setup();
    const routes: string[] = [];
    const transport = traceAgentTransport();
    renderFeature(transport, routes);

    const target = await screen.findByRole('listitem', { name: /失败的对话/ });
    expect(within(target).getByText(/write\/edit validation error/)).toBeInTheDocument();
    const evidence = await screen.findByRole('region', { name: '已读取的失败证据' });
    await waitFor(() => expect(evidence).toHaveTextContent('write/edit validation error'));
    const timeline = await screen.findByRole('region', { name: '原始对话时间线' });
    expect(timeline).toHaveAttribute('data-scrollable', 'true');
    expect(timeline).toHaveTextContent('用户 · 检查当前实现');
    expect(timeline).toHaveTextContent('思考摘要 · 核对失败证据');
    expect(timeline).toHaveTextContent('工具开始 · 读取目标文件');
    expect(timeline).toHaveTextContent('工具完成 · 读取目标文件完成');
    expect(timeline).toHaveTextContent('助手 · 发现写入版本冲突');
    expect(within(timeline).getAllByTestId('trace-agent-timeline-entry').map((entry) => entry.getAttribute('data-kind'))).toEqual([
      'user',
      'reasoning',
      'tool_started',
      'tool_finished',
      'assistant',
    ]);

    await user.click(screen.getByRole('button', { name: '开始诊断' }));
    await screen.findByRole('region', { name: 'Trace 诊断报告' });

    const createRequest = transport.requests.find(({ request }) => request.pathId === 'agent.sessions.create')?.request;
    expect(createRequest?.body).toMatchObject({
      mode: 'coordinator',
      _modelRoute: 'traceDiagnostic',
      surfaceKind: 'extension_app',
      ownerAppId: 'extension:trace-agent',
      surfaceKey: 'diagnostic',
      toolProfileVersion: 'control-center-auto-approve-v1',
      executionMode: 'full_trust',
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
      workspaceRoots: ['/workspace/paw'],
      toolAllowlistMode: 'profile',
      projectContextEnabled: true,
      piSkillsEnabled: true,
      codexSkillsEnabled: true,
    });
    const modeRequest = transport.requests.find(({ request }) => request.pathId === 'agent.session.mode.update')?.request;
    expect(modeRequest?.body).toEqual({
      mode: 'coordinator',
      toolProfileVersion: 'control-center-auto-approve-v1',
      executionMode: 'full_trust',
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
      workspaceRoots: ['/workspace/paw'],
      toolAllowlistMode: 'profile',
      projectContextEnabled: true,
      piSkillsEnabled: true,
      codexSkillsEnabled: true,
    });
    const promptRequest = transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')?.request;
    expect(promptRequest?.body).toMatchObject({ delivery: 'prompt' });
    expect(String((promptRequest?.body as Record<string, unknown> | undefined)?.message)).toContain(TRACE_AGENT_SKILL_REF);
    expect(String((promptRequest?.body as Record<string, unknown> | undefined)?.message)).toContain('session-source');
    expect(String((promptRequest?.body as Record<string, unknown> | undefined)?.message)).toContain('无需逐项审批');
    expect(String((promptRequest?.body as Record<string, unknown> | undefined)?.message)).toContain('根目录 /');
    expect(String((promptRequest?.body as Record<string, unknown> | undefined)?.message)).toContain('Project workspace binding（默认 cwd/context）：/workspace/paw');

    const report = screen.getByRole('region', { name: 'Trace 诊断报告' });
    const diagnosticTimeline = await within(report).findByRole('region', { name: '诊断 Agent 对话与报告' });
    expect(diagnosticTimeline).toHaveTextContent('用户 · 请诊断当前失败');
    expect(diagnosticTimeline).toHaveTextContent('工具完成 · 已核对 Trace 与原始对话');
    expect(diagnosticTimeline).toHaveTextContent('助手 · 根因是资源版本回执不合法；建议重新读取后再编辑。');
    expect(transport.requests.some(({ request }) => (
      request.pathId === 'agent.session.snapshot'
      && request.params?.sessionId === 'agent:trace-diagnostic'
    ))).toBe(true);
    await user.click(within(report).getByRole('button', { name: '查看关联 Trace' }));
    await user.click(within(report).getByRole('button', { name: '打开诊断 Agent 对话' }));
    expect(routes).toEqual([
      '/observability?traceId=trace%3Asource',
      '/agent?session=agent%3Atrace-diagnostic',
    ]);
  });

  it('keeps polling while the diagnostic assistant is streaming, then stops on a completed report', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({
      diagnosticSnapshots: [streamingDiagnosticSessionSnapshot(), diagnosticSessionSnapshot()],
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    expect(report).toHaveTextContent('生成中');

    await waitFor(() => expect(report).toHaveTextContent('已完成'), { timeout: 3_500 });
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'agent.session.snapshot'
      && request.params?.sessionId === 'agent:trace-diagnostic'
    )).length).toBeGreaterThanOrEqual(2);
  });

  it('retains the created report and Session after a rejected prompt and retries its frozen message identity', async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const transport = traceAgentTransport({
      diagnosticSnapshots: [emptyDiagnosticSessionSnapshot()],
      diagnosticPrompt: () => {
        if (attempts++ === 0) throw new ControlTransportHttpError('agent.session.prompt', 409, 'Runtime needs_configuration');
        return { ok: true };
      },
    });
    const routes: string[] = [];
    const firstView = renderFeature(transport, routes);
    const settings = await screen.findByRole('region', { name: '本次任务设置' });
    await user.click(within(settings).getByRole('radio', { name: /从对话沉淀方法/ }));
    await user.click(within(settings).getByRole('checkbox', { name: '全部' }));
    await user.click(within(settings).getByRole('checkbox', { name: 'Skill' }));
    await user.type(within(settings).getByRole('textbox'), '保留原诊断目标');
    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    expect(report).toHaveTextContent('启动待确认');
    await user.click(within(report).getByRole('button', { name: '打开诊断 Agent 对话' }));
    expect(routes).toContain('/agent?session=agent%3Atrace-diagnostic');
    firstView.unmount();
    renderFeature(transport, routes);
    await screen.findByRole('region', { name: 'Trace 诊断报告' });
    const restoredSettings = screen.getByRole('region', { name: '本次任务设置' });
    expect(within(restoredSettings).getByRole('radio', { name: /从对话沉淀方法/ })).toBeChecked();
    expect(within(restoredSettings).getByRole('checkbox', { name: 'Skill' })).toBeChecked();
    expect(within(restoredSettings).getByRole('textbox')).toHaveValue('保留原诊断目标');
    await user.click(await screen.findByRole('button', { name: '重试诊断启动' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '诊断已启动' })).toBeDisabled());
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'observability.traceDiagnosticReports.create')).toHaveLength(1);
    const prompts = transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt');
    expect(prompts).toHaveLength(2);
    expect(prompts[0]!.request.body).toEqual(prompts[1]!.request.body);
  });

  it('reconciles an ambiguous prompt by exact message identity without sending it again', async () => {
    const user = userEvent.setup();
    let clientMessageId = '';
    const transport = traceAgentTransport({
      diagnosticPrompt: (request) => {
        clientMessageId = String((request.body as Record<string, unknown>).clientMessageId);
        throw new TypeError('fetch failed after acceptance');
      },
      diagnosticSnapshot: () => ({
        ...emptyDiagnosticSessionSnapshot(),
        items: clientMessageId ? [{ id: 'accepted-user', role: 'user', clientMessageId, createdAtMs: 201, blocks: [{ type: 'text', data: { text: '已接收原诊断请求' } }] }] : [],
      }),
    });
    renderFeature(transport, []);
    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    await screen.findByRole('region', { name: 'Trace 诊断报告' });
    const check = screen.queryByRole('button', { name: '核对启动状态' });
    if (check) await user.click(check);
    await waitFor(() => expect(screen.getByRole('button', { name: '诊断已启动' })).toBeDisabled());
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt')).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create')).toHaveLength(1);
  });

  it('waits for terminal settlement before finalizing a canonical structured snapshot', async () => {
    const user = userEvent.setup();
    const completedWhileBusy = {
      ...diagnosticSessionSnapshot(),
      status: 'busy',
    };
    const transport = traceAgentTransport({
      diagnosticSnapshots: [completedWhileBusy, diagnosticSessionSnapshot()],
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => (
      request.pathId === 'agent.session.snapshot'
      && request.params?.sessionId === 'agent:trace-diagnostic'
    ))).toHaveLength(1));
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.traceDiagnosticReport.finalize'
    ))).toHaveLength(0);

    await waitFor(() => expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.traceDiagnosticReport.finalize'
    ))).toHaveLength(1), { timeout: 3_500 });
  });

  it('finalizes a faulted diagnostic Session into a persisted failed report', async () => {
    const user = userEvent.setup();
    const reportId = `trace-report:${'f'.repeat(32)}`;
    const generating = {
      ...persistedTraceReportFixture(reportId),
      revision: 1,
      status: 'generating' as const,
      result: null,
      failureReason: '',
    };
    const failed = {
      ...generating,
      revision: 2,
      status: 'failed' as const,
      failureReason: '诊断 Session 未生成可校验的结构化报告。',
    };
    const transport = traceAgentTransport({
      diagnosticReport: generating,
      diagnosticFinalizeReport: failed,
      diagnosticSnapshots: [{
        ...emptyDiagnosticSessionSnapshot(),
        status: 'faulted',
      }],
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'observability.traceDiagnosticReport.finalize'
    ))).toBe(true));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await waitFor(() => expect(report).toHaveTextContent('诊断失败，已保存失败报告'));
    expect(report).toHaveTextContent('诊断 Session 未生成可校验的结构化报告');
    expect(within(report).getByRole('button', { name: '交给 Agent 修复' })).toBeDisabled();
  });

  it('keeps repair locked when the diagnostic Session has text but no structured evidence report', async () => {
    const user = userEvent.setup();
    const base = diagnosticSessionSnapshot();
    const incomplete = {
      ...base,
      items: base.items.map((item) => item.id === 'message-assistant-diagnostic'
        ? {
          ...item,
          blocks: item.blocks.map((block) => ({
            ...block,
            data: { text: '报告已完成，但这里只有一段普通文本。' },
          })),
        }
        : item),
    };
    const transport = traceAgentTransport({ diagnosticSnapshots: [incomplete] });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    expect(within(report).getByRole('button', { name: '交给 Agent 修复' })).toBeDisabled();
    expect(report).toHaveTextContent('生成中');
  });

  it('stops a diagnostic poll at the deadline and lets Refresh retry the diagnostic snapshot', async () => {
    const transport = traceAgentTransport({
      diagnosticSnapshots: [emptyDiagnosticSessionSnapshot()],
    });
    renderFeature(transport, []);
    const start = await screen.findByRole('button', { name: '开始诊断' });

    vi.useFakeTimers();
    start.click();
    let report = screen.queryByRole('region', { name: 'Trace 诊断报告' });
    for (let attempt = 0; attempt < 20 && !report; attempt += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      report = screen.queryByRole('region', { name: 'Trace 诊断报告' });
    }
    expect(report).not.toBeNull();
    if (!report) throw new Error('Trace diagnostic report did not render');
    expect(report).toHaveTextContent('生成中');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(TRACE_AGENT_DIAGNOSTIC_MAX_POLL_DURATION_MS + 100);
    });
    expect(report).toHaveTextContent('读取超时');

    const beforeRefresh = transport.requests.filter(({ request }) => (
      request.pathId === 'agent.session.snapshot'
      && request.params?.sessionId === 'agent:trace-diagnostic'
    )).length;
    await act(async () => {
      screen.getByRole('button', { name: '刷新对象' }).click();
      await Promise.resolve();
      await Promise.resolve();
    });
    const afterRefresh = transport.requests.filter(({ request }) => (
      request.pathId === 'agent.session.snapshot'
      && request.params?.sessionId === 'agent:trace-diagnostic'
    )).length;
    expect(afterRefresh).toBeGreaterThan(beforeRefresh);
  });

  it('preserves timeline expansion and pagination when a Session snapshot receives new events', async () => {
    const user = userEvent.setup();
    const first = longSourceSnapshot(61, '源快照事件');
    const second = longSourceSnapshot(62, '更新后的源快照事件');
    const transport = traceAgentTransport({ sourceSnapshots: [first, second] });
    renderFeature(transport, []);

    const timeline = await screen.findByRole('region', { name: '原始对话时间线' });
    await waitFor(() => expect(within(timeline).getAllByTestId('trace-agent-timeline-entry')).toHaveLength(60));
    await user.click(within(timeline).getByRole('button', { name: '展开助手全文' }));
    await user.click(within(timeline).getByRole('button', { name: '加载更早 1 条' }));
    expect(within(timeline).getAllByTestId('trace-agent-timeline-entry')).toHaveLength(61);

    await user.click(screen.getByRole('button', { name: '刷新对象' }));
    await waitFor(() => expect(timeline).toHaveTextContent('更新后的源快照事件 61'));
    expect(timeline).toHaveTextContent('完整消息结尾');
    const refreshedEntries = within(timeline).getAllByTestId('trace-agent-timeline-entry');
    expect(refreshedEntries).toHaveLength(62);
    expect(refreshedEntries[0]).toHaveAttribute('data-sequence', '1');
    expect(refreshedEntries.at(-1)).toHaveAttribute('data-sequence', '62');
  });

  it('replaces partial and final messages and blocks by stable identity', async () => {
    const duplicateStream = {
      ok: true,
      sessionId: 'session-source',
      status: 'idle',
      items: [
        {
          id: 'assistant-stream',
          role: 'assistant',
          status: 'streaming',
          timelineSequence: 2,
          createdAtMs: 100,
          blocks: [
            { id: 'text-stream', type: 'text', status: 'streaming', data: { text: '临时回答' } },
            { id: 'reasoning-stream', type: 'reasoning_summary', status: 'running', timelineSequence: 2.1, data: { summary: '临时思考' } },
          ],
        },
        {
          id: 'assistant-stream',
          role: 'assistant',
          status: 'completed',
          timelineSequence: 2,
          createdAtMs: 110,
          blocks: [
            { id: 'text-stream', type: 'text', status: 'completed', data: { text: '最终回答' } },
            { id: 'reasoning-stream', type: 'reasoning_summary', status: 'completed', timelineSequence: 2.1, data: { summary: '最终思考' } },
          ],
        },
      ],
      liveEvents: [],
    };
    renderFeature(traceAgentTransport({ sourceSnapshot: duplicateStream }), []);

    const timeline = await screen.findByRole('region', { name: '原始对话时间线' });
    await waitFor(() => expect(within(timeline).getAllByTestId('trace-agent-timeline-entry')).toHaveLength(2));
    const entries = within(timeline).getAllByTestId('trace-agent-timeline-entry');
    expect(entries.filter((entry) => entry.getAttribute('data-kind') === 'assistant')).toHaveLength(1);
    expect(entries.filter((entry) => entry.getAttribute('data-kind') === 'reasoning')).toHaveLength(1);
    expect(timeline).toHaveTextContent('最终回答');
    expect(timeline).toHaveTextContent('最终思考');
    expect(timeline).not.toHaveTextContent('临时回答');
    expect(timeline).not.toHaveTextContent('临时思考');
  });

  it('orders durable user messages without sequences before their own assistant and tool events', async () => {
    // Read from the real Session messages response: user rows have only epoch
    // timestamps, while assistant and history activity rows have JSONL ordinals.
    // Content and identities are replaced; the ordering fields are unchanged.
    const snapshot = {
      ok: true,
      sessionId: 'session-source',
      status: 'idle',
      items: [
        { id: 'user-1', turnId: 'turn-1', role: 'user', createdAtMs: 1788324850227, blocks: [{ type: 'text', data: { text: '第一轮请求' } }] },
        { id: 'assistant-1', turnId: 'turn-1', role: 'assistant', timelineSequence: 2.9, createdAtMs: 1788324854774, blocks: [{ type: 'text', data: { text: '第一轮回答' } }] },
        { id: 'user-2', turnId: 'turn-2', role: 'user', createdAtMs: 1788325217090, blocks: [{ type: 'text', data: { text: '第二轮请求' } }] },
        { id: 'assistant-2', turnId: 'turn-2', role: 'assistant', timelineSequence: 6.9, createdAtMs: 1788325224772, blocks: [{ type: 'text', data: { text: '第二轮回答' } }] },
        { id: 'user-3', turnId: 'turn-3', role: 'user', createdAtMs: 1788327047098, blocks: [{ type: 'text', data: { text: '第三轮请求' } }] },
        { id: 'assistant-3', turnId: 'turn-3', role: 'assistant', timelineSequence: 8.9, createdAtMs: 1788327049961, blocks: [{ type: 'text', data: { text: '第三轮回答' } }] },
      ],
      liveEvents: [
        { eventId: 'reasoning-1', turnId: 'turn-1', eventType: 'reasoning_summary', sequence: 1, timelineSequence: 2.1, createdAtMs: 1788324854774, payload: { summary: '第一轮思考' } },
        { eventId: 'reasoning-2', turnId: 'turn-2', eventType: 'reasoning_summary', sequence: 2, timelineSequence: 4.1, createdAtMs: 1788325221542, payload: { summary: '第二轮思考' } },
        { eventId: 'tool-start', turnId: 'turn-2', eventType: 'tool_started', sequence: 3, timelineSequence: 4.2010000000000005, createdAtMs: 1788325221543, payload: { summary: '工具开始' } },
        { eventId: 'tool-finish', turnId: 'turn-2', eventType: 'tool_finished', sequence: 4, timelineSequence: 5.8, createdAtMs: 1788325221761, payload: { summary: '工具完成' } },
      ],
    };
    renderFeature(traceAgentTransport({ sourceSnapshot: snapshot }), []);
    const timeline = await screen.findByRole('region', { name: '原始对话时间线' });
    await waitFor(() => expect(within(timeline).getAllByTestId('trace-agent-timeline-entry')).toHaveLength(10));
    const entries = within(timeline).getAllByTestId('trace-agent-timeline-entry');
    expect(entries.map((entry) => entry.querySelector('.trace-agent-timeline__title > span')?.textContent?.replace(/^ · /, ''))).toEqual([
      '第一轮请求', '第一轮思考', '第一轮回答', '第二轮请求', '第二轮思考', '工具开始', '工具完成', '第二轮回答', '第三轮请求', '第三轮回答',
    ]);
    expect(entries.filter((entry) => entry.getAttribute('data-kind') === 'user').map((entry) => entry.getAttribute('data-sequence'))).not.toContain('1788324850227');
  });

  it('switches between Room and recorded runs while preserving precise source deep links', async () => {
    const user = userEvent.setup();
    const routes: string[] = [];
    const transport = traceAgentTransport();
    renderFeature(transport, routes);

    await user.click(await screen.findByRole('tab', { name: 'Room 协作' }));
    const room = await screen.findByRole('listitem', { name: /失败的协作/ });
    await user.click(room);
    const selected = await screen.findByRole('region', { name: '已选择诊断对象' });
    const timeline = within(selected).getByRole('region', { name: '原始对话时间线' });
    await waitFor(() => expect(timeline).toHaveTextContent('用户 · Room 用户问题'));
    expect(timeline).toHaveTextContent('思考摘要 · 协作者核对分工');
    expect(timeline).toHaveTextContent('工具开始 · 读取 WorkItem');
    expect(timeline).toHaveTextContent('工具完成 · WorkItem 读取完成');
    expect(timeline).toHaveTextContent('助手 · Room 已发布进展');
    expect(within(selected).getByRole('heading', { name: '失败的协作' })).toBeInTheDocument();
    await user.click(within(selected).getByRole('button', { name: '打开原对话' }));
    expect(routes).toContain('/rooms?room=room-source');

    await user.click(screen.getByRole('tab', { name: '运行记录' }));
    const run = await screen.findByRole('listitem', { name: /运行失败/ });
    await user.click(run);
    const runSelected = await screen.findByRole('region', { name: '已选择诊断对象' });
    const runTimeline = within(runSelected).getByRole('region', { name: '运行事件时间线' });
    expect(runTimeline).toHaveTextContent('Tool · workspace_write');
    expect(runTimeline).toHaveTextContent('write/edit validation error: target file changed');
    await user.click(within(runSelected).getByRole('button', { name: '打开关联 Session' }));
    await user.click(within(runSelected).getByRole('button', { name: '打开关联 Room' }));
    await user.click(within(runSelected).getByRole('button', { name: '打开运行记录' }));
    expect(routes).toContain('/agent?session=session-source');
    expect(routes).toContain('/rooms?room=room-source');
    expect(routes).toContain('/observability?runId=run-source');
  });

  it('paginates truncated run Observations and prefers canonical Trace binding for deep links', async () => {
    const user = userEvent.setup();
    const initial = runObservationSnapshot([
      observationEvent(3, 'session-observation', 'room-observation'),
      observationEvent(4, 'session-observation', 'room-observation'),
    ], true);
    const older = runObservationSnapshot([
      observationEvent(1, 'session-observation', 'room-observation'),
      observationEvent(2, 'session-observation', 'room-observation'),
    ], false);
    const routes: string[] = [];
    const transport = traceAgentTransport({
      runObservationSnapshots: [
        { beforeSequence: 0, snapshot: initial },
        { beforeSequence: 3, snapshot: older },
      ],
      traceDetails: { 'trace:source': canonicalTraceResponse('session-canonical', 'room-canonical') },
    });
    renderFeature(transport, routes);

    await user.click(await screen.findByRole('tab', { name: '运行记录' }));
    await user.click(await screen.findByRole('listitem', { name: /运行失败/ }));
    const selected = await screen.findByRole('region', { name: '已选择诊断对象' });
    const timeline = within(selected).getByRole('region', { name: '运行事件时间线' });
    expect(timeline).toHaveTextContent('#3');
    expect(timeline).toHaveTextContent('#4');
    await user.click(within(timeline).getByRole('button', { name: '从 Observation 加载更早' }));

    await waitFor(() => expect(within(timeline).getAllByTestId('trace-agent-run-timeline-entry')).toHaveLength(4));
    expect(within(timeline).getAllByTestId('trace-agent-run-timeline-entry').map((entry) => entry.getAttribute('data-sequence'))).toEqual(['1', '2', '3', '4']);
    await user.click(within(selected).getByRole('button', { name: '打开关联 Session' }));
    await user.click(within(selected).getByRole('button', { name: '打开关联 Room' }));
    expect(routes).toContain('/agent?session=session-canonical');
    expect(routes).toContain('/rooms?room=room-canonical');
    const historyRequest = transport.requests.find(({ request }) => request.pathId === 'observability.snapshot' && request.query?.beforeSequence === 3);
    expect(historyRequest?.request.query).toMatchObject({ runId: 'run-source', beforeSequence: 3, limit: 100 });
  });

  it('marks run bindings unknown when canonical Trace detail fails instead of using observation fallback', async () => {
    const user = userEvent.setup();
    const conflict = runObservationSnapshot([
      observationEvent(3, 'session-a', 'room-consistent'),
      observationEvent(4, 'session-b', 'room-consistent'),
    ], false);
    const transport = traceAgentTransport({
      runObservationSnapshots: [{ beforeSequence: 0, snapshot: conflict }],
      traceDetails: { 'trace:source': null },
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('tab', { name: '运行记录' }));
    const selected = await screen.findByRole('region', { name: '已选择诊断对象' });
    expect(within(selected).queryByRole('button', { name: '打开关联 Session' })).not.toBeInTheDocument();
    expect(within(selected).queryByRole('button', { name: '打开关联 Room' })).not.toBeInTheDocument();
    expect(selected).toHaveTextContent('无法验证关联 Session / Room');
  });

  it('does not expose observation fallback links while canonical run binding is pending', async () => {
    let resolveTrace!: (value: unknown) => void;
    const pendingTrace = new Promise<unknown>((resolve) => {
      resolveTrace = resolve;
    });
    const transport = traceAgentTransport({
      traceDetails: { 'trace:source': pendingTrace },
    });
    renderFeature(transport, []);

    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '运行记录' }));
    const selected = await screen.findByRole('region', { name: '已选择诊断对象' });
    expect(within(selected).queryByRole('button', { name: '打开关联 Session' })).not.toBeInTheDocument();
    expect(within(selected).queryByRole('button', { name: '打开关联 Room' })).not.toBeInTheDocument();
    expect(selected).toHaveTextContent('正在验证关联 Session / Room');

    await act(async () => {
      resolveTrace(canonicalTraceResponse('session-canonical', 'room-canonical'));
      await Promise.resolve();
    });
    expect(await within(selected).findByRole('button', { name: '打开关联 Session' })).toBeInTheDocument();
    expect(within(selected).getByRole('button', { name: '打开关联 Room' })).toBeInTheDocument();
  });

  it('keeps the selected-object diagnostic action visible and reports its binding scope', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    renderFeature(transport, []);

    await user.click(await screen.findByRole('tab', { name: 'Room 协作' }));
    const selected = await screen.findByRole('region', { name: '已选择诊断对象' });
    const action = screen.getByTestId('trace-agent-diagnostic-action');
    expect(action).toHaveAttribute('data-sticky', 'true');
    expect(action).toHaveTextContent('当前 Room');
    expect(action).toHaveTextContent('全部行星');
    expect(action).toHaveTextContent('WorkItems');

    await user.click(within(action).getByRole('button', { name: '开始诊断' }));
    await waitFor(() => expect(action).toHaveAttribute('data-state', 'complete'));
    expect(within(action).getByRole('button', { name: '诊断已启动' })).toBeDisabled();
    expect(within(action).getByRole('button', { name: '打开诊断 Agent 对话' })).toBeInTheDocument();
  });

  it('hands a completed diagnostic to a full-automation repair Agent and can return to Trace', async () => {
    const user = userEvent.setup();
    const routes: string[] = [];
    const transport = traceAgentTransport();
    renderFeature(transport, routes);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    const repairTrigger = within(report).getByRole('button', { name: '交给 Agent 修复' });
    await user.click(repairTrigger);
    expect(screen.queryByTestId('trace-agent-repair-confirmation')).not.toBeInTheDocument();

    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    expect(within(report).getByRole('button', { name: '修复 Agent 已就绪' })).toBeDisabled();
    expect(screen.getByTestId('trace-agent-repair-ready')).toHaveTextContent('无法虚构 Replay Case');
    expect(screen.getByTestId('trace-agent-repair-ready')).toHaveTextContent('不构成同 Case 验证');

    const createRequests = transport.requests.filter(({ request }) => request.pathId === 'agent.sessions.create');
    expect(createRequests).toHaveLength(2);
    expect(createRequests[1]?.request.body).toEqual({
      title: '修复 Trace 诊断 · 失败的对话',
      surfaceKind: 'extension_app',
      ownerAppId: 'extension:trace-agent',
      surfaceKey: 'repair',
      mode: 'coordinator',
      toolProfileVersion: 'control-center-auto-approve-v1',
      executionMode: 'full_trust',
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
      workspaceRoots: ['/workspace/paw'],
      toolAllowlistMode: 'profile',
      projectContextEnabled: true,
      piSkillsEnabled: true,
      codexSkillsEnabled: true,
    });
    const modeRequests = transport.requests.filter(({ request }) => request.pathId === 'agent.session.mode.update');
    expect(modeRequests[1]?.request.body).toEqual({
      mode: 'coordinator',
      toolProfileVersion: 'control-center-auto-approve-v1',
      executionMode: 'full_trust',
      dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
      workspaceRoots: ['/workspace/paw'],
      toolAllowlistMode: 'profile',
      projectContextEnabled: true,
      piSkillsEnabled: true,
      codexSkillsEnabled: true,
    });
    expect(screen.getByTestId('trace-agent-repair-ready')).toHaveTextContent('全信任修复 Agent');
    expect(screen.getByTestId('trace-agent-repair-ready')).toHaveTextContent('所有 Tool 操作自动批准');
    const promptRequests = transport.requests.filter(({ request }) => request.pathId === 'agent.session.prompt');
    expect(promptRequests).toHaveLength(2);
    const repairPrompt = String((promptRequests[1]?.request.body as Record<string, unknown> | undefined)?.message);
    expect(repairPrompt).toContain('session-source');
    expect(repairPrompt).toContain('agent:trace-diagnostic');
    expect(repairPrompt).toContain('trace:source');
    expect(repairPrompt).toContain('sourceScope: session:session-source');
    expect(repairPrompt).toContain('failureRef: trace:observation-source');
    expect(repairPrompt).toContain('TRACE_REPAIR_EVIDENCE');
    expect(repairPrompt).toContain('服务端在证据写入后生成');
    expect(repairPrompt).not.toContain('changeReceiptId: <');
    expect(repairPrompt).not.toContain('testEvidenceId: <');
    expect(repairPrompt).toContain('control-center-auto-approve-v1');
    expect(repairPrompt).toContain('权限边界始终是 /');
    expect(repairPrompt).toContain('修复 Session 已继承来源 Project workspace binding（默认 cwd/context）：/workspace/paw');
    expect(repairPrompt).toContain('根目录 / 仍是 full_trust 的读取/Tool 能力，不是项目身份');
    expect(repairPrompt).toContain('不要再询问目录、ENABLE_FULL_TRUST、Tool 批准或任何 PAW 审批');
    expect(repairPrompt).not.toContain('待审批操作交给独立 Luna Max');
    expect(repairPrompt).not.toContain('per_action 授权');
    const requestOrder = transport.requests.map(({ request }) => request.pathId);
    expect(requestOrder.indexOf('observability.traceDiagnosticReport.repairAuthorize'))
      .toBeLessThan(requestOrder.lastIndexOf('agent.session.prompt'));

    await user.click(within(report).getByRole('button', { name: '打开修复 Session' }));
    expect(routes).toContain('/agent?session=agent%3Atrace-repair');

    await user.click(within(report).getByRole('button', { name: '回到 Trace 重跑诊断' }));
    expect(routes).toContain('/observability?traceId=trace%3Asource');
    expect(screen.getByRole('button', { name: '开始诊断' })).toBeInTheDocument();
  });

  it('resumes the authorized repair Session when prompt delivery fails', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({ repairPromptFailures: 1 });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));

    const recovery = await screen.findByTestId('trace-agent-repair-recovery');
    expect(recovery).toHaveTextContent('原修复 Session 已持久化');
    expect(within(report).getByRole('button', { name: '修复授权已保存' })).toBeDisabled();
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'agent.sessions.create'
    ))).toHaveLength(2);
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.traceDiagnosticReport.repairAuthorize'
    ))).toHaveLength(1);

    await user.click(within(recovery).getByRole('button', { name: '重新发送修复任务' }));
    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'agent.sessions.create'
    ))).toHaveLength(2);
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.traceDiagnosticReport.repairAuthorize'
    ))).toHaveLength(1);
    const repairPrompts = transport.requests.filter(({ request }) => (
      request.pathId === 'agent.session.prompt'
      && String((request.body as Record<string, unknown> | undefined)?.message ?? '').includes('Trace Agent 的候选修复交接')
    ));
    expect(repairPrompts).toHaveLength(2);
    expect((repairPrompts[0]?.request.body as Record<string, unknown>).clientMessageId)
      .toBe((repairPrompts[1]?.request.body as Record<string, unknown>).clientMessageId);
  });

  it('submits only repair references in order, then rechecks the authoritative receipt', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));
    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());

    await user.click(within(report).getByRole('button', { name: '复检修复 Trace 证据' }));

    await waitFor(() => expect(screen.getByTestId('trace-agent-eval-receipt')).toBeInTheDocument());
    const receipt = screen.getByTestId('trace-agent-eval-receipt');
    expect(receipt).toHaveTextContent('eval:trace-agent:recheck:independent');
    expect(receipt).toHaveTextContent('诊断 Trace：trace:source');
    expect(receipt).toHaveTextContent('修复 Trace：trace:turn:turn-repair');
    expect(receipt).toHaveTextContent('已记录的 Host 沙盒测试证据：passed · 1 次');
    expect(receipt).toHaveTextContent('AI Judge 复检已持久化');
    expect(receipt).toHaveTextContent('此按钮不重跑命令');
    expect(receipt).toHaveTextContent('不进行同案 Trace 回放');
    expect(receipt).toHaveTextContent('不验证 source SHA');
    expect(receipt).not.toHaveTextContent('独立复验');
    const repairSnapshotRequest = transport.requests.find(({ request }) => (
      request.pathId === 'observability.snapshot'
      && request.query?.sessionId === 'agent:trace-repair'
    ));
    expect(repairSnapshotRequest?.request.query).toMatchObject({ sessionId: 'agent:trace-repair', limit: 100 });
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.trace.get'
      && request.params?.traceId === 'trace:repair')).toBe(false);
    const repairRequests = transport.requests
      .map(({ request }) => request)
      .filter((request) => request.pathId.startsWith('observability.traceRepair'));
    expect(repairRequests.map((request) => request.pathId)).toEqual([
      'observability.traceRepair.changeEvidence',
      'observability.traceRepair.testEvidence',
      'observability.traceRepair.receipt.create',
      'observability.traceRepair.receipt.get',
      'observability.traceRepair.recheck',
    ]);
    expect(repairRequests[0]?.body).toEqual({
      schemaVersion: 'rag-ime.trace-repair-change-evidence.v1',
      repairSessionId: 'agent:trace-repair',
      repairTraceId: 'trace:turn:turn-repair',
    });
    expect(repairRequests[1]?.body).toEqual({
      schemaVersion: 'rag-ime.trace-repair-test-evidence.v1',
      repairSessionId: 'agent:trace-repair',
      repairTraceId: 'trace:turn:turn-repair',
    });
    expect(repairRequests.at(-1)?.body).toEqual({
      schemaVersion: 'rag-ime.trace-repair-recheck-request.v1',
      repairReceiptId: 'repair-receipt:server-issued',
    });
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.evals.evidence.run')).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.evals.aiJudge.run')).toBe(false);
  });

  it('freezes and rereads the same Replay Case before repair, then binds the verification decision', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({ replayEvidence: true });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));
    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    expect(screen.queryByTestId('trace-agent-repair-confirmation')).not.toBeInTheDocument();

    const caseCreateIndex = transport.requests.findIndex(({ request }) => request.pathId === 'observability.traceReplay.case.create');
    const caseGetIndex = transport.requests.findIndex(({ request }) => request.pathId === 'observability.traceReplay.case.get');
    const repairCreateIndex = transport.requests.findIndex(({ request }) => (
      request.pathId === 'agent.sessions.create'
      && (request.body as Record<string, unknown> | undefined)?.surfaceKey === 'repair'
    ));
    expect(caseCreateIndex).toBeGreaterThanOrEqual(0);
    expect(caseGetIndex).toBeGreaterThan(caseCreateIndex);
    expect(repairCreateIndex).toBeGreaterThan(caseGetIndex);
    expect(transport.requests[caseCreateIndex]?.request.body).toEqual({
      schemaVersion: 'rag-ime.trace-replay-case-create.v1',
      sourceScope: 'session:session-source',
      failureRef: 'trace:observation-source',
      sourceTraceId: 'trace:source',
      baselineEvalRunId: 'eval:baseline',
      baselineSandboxRunId: 'sandbox:baseline',
      successMetric: 'accuracy',
      successThreshold: 1,
      rollbackTarget: `source-sha256:${'b'.repeat(64)}`,
    });

    await user.click(within(report).getByRole('button', { name: '同 Case 验证候选' }));
    const verification = await screen.findByTestId('trace-agent-verification-receipt');
    expect(verification).toHaveTextContent('Keep');
    expect(verification).toHaveTextContent('trace-verification:server-issued');
    expect(verification).toHaveTextContent('accuracy');
    expect(verification).toHaveTextContent('0.25');
    expect(verification).toHaveTextContent('1');
    expect(verification).toHaveTextContent('+0.75');
    expect(verification).toHaveTextContent('120 → 90');
    expect(verification).toHaveTextContent('-30');
    expect(verification).toHaveTextContent(`source-sha256:${'b'.repeat(64)}`);

    const replayRequests = transport.requests
      .map(({ request }) => request)
      .filter((request) => request.pathId.startsWith('observability.traceReplay'));
    expect(replayRequests.map((request) => request.pathId)).toEqual([
      'observability.traceReplay.case.create',
      'observability.traceReplay.case.get',
      'observability.traceReplay.verify',
      'observability.traceReplay.verification.get',
    ]);
    expect(replayRequests[2]?.body).toEqual({
      schemaVersion: 'rag-ime.trace-verification-request.v1',
      replayCaseId: 'replay-case:server-issued',
      repairReceiptId: 'repair-receipt:server-issued',
      repairEvalRunId: 'eval:repair',
      repairSandboxRunId: 'sandbox:repair',
      regressionEvalRunIds: ['eval:regression'],
    });
    const reportVerification = transport.requests.find(({ request }) => (
      request.pathId === 'observability.traceDiagnosticReport.repairVerify'
    ))?.request;
    expect(reportVerification?.body).toMatchObject({
      repairReceiptId: 'repair-receipt:server-issued',
      verificationReceiptId: 'trace-verification:server-issued',
    });
  });

  it('does not require an assistant-authored repair evidence report', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({ repairSessionSnapshot: repairSessionSnapshot({ receipt: '' }) });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));
    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    await user.click(within(report).getByRole('button', { name: '复检修复 Trace 证据' }));

    await waitFor(() => expect(screen.getByTestId('trace-agent-eval-receipt')).toBeInTheDocument());
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.evals.aiJudge.run')).toBe(false);
  });

  it('blocks a claimed passed repair when snapshots contain no completed test evidence', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({
      serverRejectTest: true,
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));
    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    await user.click(within(report).getByRole('button', { name: '复检修复 Trace 证据' }));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('没有已通过的测试工具证据'));
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.traceRepair.changeEvidence')).toBe(true);
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.traceRepair.recheck')).toBe(false);
  });

  it('rejects a completed read/model-only Trace as change evidence', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({
      serverRejectChange: true,
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));
    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    await user.click(within(report).getByRole('button', { name: '复检修复 Trace 证据' }));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('没有已完成的修改工具证据'));
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.traceRepair.changeEvidence')).toBe(true);
  });

  it('ignores a mismatched failureRef claimed in assistant text', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({
      repairSessionSnapshot: repairSessionSnapshot({ failureRef: 'trace:other-failure' }),
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));
    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());
    await user.click(within(report).getByRole('button', { name: '复检修复 Trace 证据' }));

    await waitFor(() => expect(screen.getByTestId('trace-agent-eval-receipt')).toBeInTheDocument());
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.evals.aiJudge.run')).toBe(false);
  });

  it('does not evaluate the diagnostic Trace until the repair Session has a completed Trace', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport({
      repairTrace: false,
      repairSessionSnapshot: { ...repairSessionSnapshot(), items: [] },
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('button', { name: '开始诊断' }));
    const report = await screen.findByRole('region', { name: 'Trace 诊断报告' });
    await user.click(within(report).getByRole('button', { name: '交给 Agent 修复' }));
    await waitFor(() => expect(screen.getByTestId('trace-agent-repair-ready')).toBeInTheDocument());

    await user.click(within(report).getByRole('button', { name: '复检修复 Trace 证据' }));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('请先完成修复后再复检'));
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.evals.evidence.run')).toBe(false);
    expect(transport.requests.some(({ request }) => request.pathId === 'observability.trace.get')).toBe(false);
  });

  it('keeps more than sixty canonical events reachable in order and expands messages beyond the summary', async () => {
    const user = userEvent.setup();
    const longMessage = `完整消息开头 ${'原始内容'.repeat(50)} 完整消息结尾`;
    const sourceSnapshot = {
      ok: true,
      sessionId: 'session-source',
      status: 'idle',
      items: [{
        id: 'message-long',
        role: 'assistant',
        status: 'completed',
        timelineSequence: 66,
        createdAtMs: 66,
        blocks: [{ id: 'text-long', type: 'text', status: 'completed', data: { text: longMessage } }],
      }],
      liveEvents: Array.from({ length: 65 }, (_, index) => ({
        eventId: `event-${index}`,
        eventType: 'reasoning_summary',
        timelineSequence: index + 1,
        sequence: index + 1,
        createdAtMs: index + 1,
        payload: { summary: `原始事件 ${index}` },
      })),
    };
    renderFeature(traceAgentTransport({ sourceSnapshot }), []);

    const timeline = await screen.findByRole('region', { name: '原始对话时间线' });
    await waitFor(() => expect(within(timeline).getAllByTestId('trace-agent-timeline-entry')).toHaveLength(60));
    expect(timeline).not.toHaveTextContent('原始事件 0');
    expect(timeline).toHaveTextContent('完整消息开头');
    expect(timeline).not.toHaveTextContent('完整消息结尾');

    await user.click(within(timeline).getByRole('button', { name: '展开助手全文' }));
    expect(timeline).toHaveTextContent('完整消息结尾');
    await user.click(within(timeline).getByRole('button', { name: '加载更早 6 条' }));

    const entries = within(timeline).getAllByTestId('trace-agent-timeline-entry');
    expect(entries).toHaveLength(66);
    expect(entries[0]).toHaveAttribute('data-sequence', '1');
    expect(entries.at(-1)).toHaveAttribute('data-sequence', '66');
    expect(entries[0]).toHaveTextContent('原始事件 0');
  });

  it('loads genuinely older Room events through the history cursor and merges them in order', async () => {
    const user = userEvent.setup();
    const snapshot = roomSnapshot();
    const retainedEvents = snapshot.events.slice(2);
    const retainedSnapshot = {
      ...snapshot,
      events: retainedEvents,
      firstSequence: 3,
      lastSequence: 6,
      resumeToken: 'room-source:6',
      truncated: true,
    };
    const olderPage = {
      schemaVersion: 'rag-ime.agent-room-event-page.v1' as const,
      ok: true as const,
      roomId: 'room-source',
      items: snapshot.events.slice(0, 2),
      firstSequence: 1,
      lastSequence: 2,
      nextBeforeSequence: 0,
      hasMore: false,
      retainedFirstSequence: 1,
      retainedLastSequence: 6,
      retainedPrefixTruncated: false,
    };
    const transport = traceAgentTransport({ roomSourceSnapshot: retainedSnapshot, roomHistoryPage: olderPage });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('tab', { name: 'Room 协作' }));
    const timeline = await screen.findByRole('region', { name: '原始对话时间线' });
    expect(timeline).not.toHaveTextContent('Room 用户问题');
    await waitFor(() => expect(timeline).toHaveTextContent('Room 已发布进展'));
    const loadOlder = within(timeline).getByRole('button', { name: '从 Room history 加载更早' });
    await user.click(loadOlder);

    await waitFor(() => expect(timeline).toHaveTextContent('Room 用户问题'));
    const historyRequest = transport.requests.find(({ request }) => request.pathId === 'agent.room.history');
    expect(historyRequest?.request.params).toEqual({ roomId: 'room-source' });
    expect(historyRequest?.request.query).toEqual({ beforeSequence: 3, limit: 200 });
    const entries = within(timeline).getAllByTestId('trace-agent-timeline-entry');
    expect(entries.map((entry) => entry.getAttribute('data-sequence'))).toEqual(['1', '2', '3', '4', '5', '6']);
  });

  it('keeps the oldest loaded Room history event visible when a tail event arrives', async () => {
    const user = userEvent.setup();
    const snapshot = roomSnapshot();
    const retainedSnapshot = {
      ...snapshot,
      events: snapshot.events.slice(2),
      firstSequence: 3,
      lastSequence: 6,
      resumeToken: 'room-source:6',
      truncated: true,
    };
    const tailEvent = {
      ...snapshot.events[5],
      eventId: 'room-tail-source',
      sequence: 7,
      createdAtMs: 160,
      payload: { post: { content: 'Room 追加进展' } },
      resumeToken: 'room-source:7',
    };
    const appendedSnapshot = {
      ...retainedSnapshot,
      events: [...retainedSnapshot.events, tailEvent],
      lastSequence: 7,
      resumeToken: 'room-source:7',
    };
    const olderPage = {
      schemaVersion: 'rag-ime.agent-room-event-page.v1' as const,
      ok: true as const,
      roomId: 'room-source',
      items: snapshot.events.slice(0, 2),
      firstSequence: 1,
      lastSequence: 2,
      nextBeforeSequence: 0,
      hasMore: false,
      retainedFirstSequence: 1,
      retainedLastSequence: 6,
      retainedPrefixTruncated: false,
    };
    const transport = traceAgentTransport({
      roomSourceSnapshots: [retainedSnapshot, appendedSnapshot],
      roomHistoryPage: olderPage,
    });
    renderFeature(transport, []);

    await user.click(await screen.findByRole('tab', { name: 'Room 协作' }));
    const timeline = await screen.findByRole('region', { name: '原始对话时间线' });
    await user.click(await within(timeline).findByRole('button', { name: '从 Room history 加载更早' }));
    await waitFor(() => expect(within(timeline).getAllByTestId('trace-agent-timeline-entry')).toHaveLength(6));

    await user.click(screen.getByRole('button', { name: '刷新对象' }));
    await waitFor(() => expect(timeline).toHaveTextContent('Room 追加进展'));
    const entries = within(timeline).getAllByTestId('trace-agent-timeline-entry');
    expect(entries.map((entry) => entry.getAttribute('data-sequence'))).toEqual(['1', '2', '3', '4', '5', '6', '7']);
  });

  it('does not reinterpret legacy diagnostic Sessions as persisted reports', async () => {
    const transport = traceAgentTransport({
      sessions: [
        {
          id: 'agent:trace-diagnostic-existing',
          title: 'Trace 诊断 · 已归档的失败对话',
          status: 'idle',
          updatedAtMs: 200,
          workspaceRoots: [],
          messageCount: 8,
          lastMessagePreview: '根因与候选修复已写入 Session',
        },
      ],
    });
    renderFeature(transport, []);

    expect(await screen.findByText('没有诊断对象')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '已保存的 Trace 诊断报告' })).not.toBeInTheDocument();
    expect(screen.queryByText('Trace 诊断 · 已归档的失败对话')).not.toBeInTheDocument();
  });

  it('keeps a Session selection when adding a Room from another tab and submits the ordered target set', async () => {
    const user = userEvent.setup();
    const transport = traceAgentTransport();
    renderFeature(transport, []);

    const session = await screen.findByRole('listitem', { name: /失败的对话/ });
    expect(within(session).getByRole('checkbox')).toBeChecked();
    await user.click(screen.getByRole('tab', { name: 'Room 协作' }));
    const room = await screen.findByRole('listitem', { name: /失败的协作/ });
    await user.click(within(room).getByRole('checkbox'));
    expect(within(room).getByRole('checkbox')).toBeChecked();
    await user.click(screen.getByRole('tab', { name: 'Session 对话' }));
    expect(within(await screen.findByRole('listitem', { name: /失败的对话/ })).getByRole('checkbox')).toBeChecked();

    await user.click(screen.getByRole('button', { name: '开始诊断' }));
    const promptRequest = await waitFor(() => {
      const request = transport.requests.find(({ request }) => request.pathId === 'agent.session.prompt')?.request;
      expect(request).toBeTruthy();
      return request!;
    });
    const prompt = String((promptRequest.body as Record<string, unknown> | undefined)?.message);
    expect(prompt).toContain('本次冻结范围共 2 个对象：session:session-source、room:room-source');
    expect(prompt.indexOf('session:session-source')).toBeLessThan(prompt.indexOf('room:room-source'));
  });

  it('shows diagnosed target status and opens the persisted eight-dimension report with failure reason', async () => {
    const routes: string[] = [];
    const reportId = `trace-report:${'a'.repeat(32)}`;
    const report = persistedTraceReportFixture(reportId, 'failed');
    const transport = traceAgentTransport({
      diagnosticReports: {
        schemaVersion: 'rag-ime.trace-diagnostic-report-list.v1',
        total: 1,
        truncated: false,
        items: [{
          reportId,
          revision: report.revision,
          status: report.status,
          title: report.title,
          diagnosticSessionId: report.diagnosticSessionId,
          targetKeys: ['session:session-source'],
          targets: report.targets,
          traceIds: report.traceIds,
          failureReason: report.failureReason,
          createdAtMs: report.createdAtMs,
          updatedAtMs: report.updatedAtMs,
        }],
      },
      diagnosticReport: report,
    });
    const view = renderFeature(transport, routes);

    const target = await screen.findByRole('listitem', { name: /失败的对话/ });
    expect(target).toHaveTextContent('已诊断 · 失败');
    expect(within(target).getByRole('button', { name: '打开报告' })).toBeInTheDocument();
    await userEvent.setup().click(within(target).getByRole('button', { name: '打开报告' }));
    expect(routes).toContain(`/trace-agent?reportId=${encodeURIComponent(reportId)}`);

    view.unmount();
    renderFeature(transport, routes, [`/trace-agent?reportId=${encodeURIComponent(reportId)}`]);
    const page = await screen.findByRole('region', { name: 'Trace 诊断网页报告' });
    expect(page).toHaveTextContent('诊断未完成');
    expect(page).toHaveTextContent('报告失败原因：结构化结果缺失');
    expect(page).toHaveTextContent('系统确定性');
    expect(page).toHaveTextContent('部分冻结');
    expect(page).toHaveTextContent('尚未记录修复授权');
    expect(within(page).getByTestId('trace-diagnostic-scorecard').querySelectorAll('tbody tr')).toHaveLength(8);
    const download = within(page).getByRole('link', { name: '下载 HTML 报告' });
    expect(download).toHaveAttribute('download', 'trace-diagnostic-report.html');
    expect(download.getAttribute('href')).toMatch(/^blob:/);
  });

  it('loads older persisted diagnostic reports through the server cursor', async () => {
    const first = persistedTraceReportFixture(`trace-report:${'1'.repeat(32)}`, 'completed');
    first.title = '最近报告';
    const older = persistedTraceReportFixture(`trace-report:${'2'.repeat(32)}`, 'failed');
    older.title = '更早报告';
    const transport = traceAgentTransport({
      diagnosticReportPages: [{
        schemaVersion: 'rag-ime.trace-diagnostic-report-list.v1',
        total: 2,
        truncated: true,
        nextCursor: `200.${'1'.repeat(32)}`,
        items: [traceReportSummary(first)],
      }, {
        schemaVersion: 'rag-ime.trace-diagnostic-report-list.v1',
        total: 2,
        truncated: false,
        nextCursor: null,
        items: [traceReportSummary(older)],
      }],
    });
    const user = userEvent.setup();
    renderFeature(transport, [], ['/trace-agent']);

    const reports = await screen.findByRole('region', { name: '已保存的 Trace 诊断报告' });
    expect(reports).toHaveTextContent('最近报告');
    expect(within(reports).queryByText('更早报告')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '加载更早任务' }));
    await waitFor(() => expect(within(reports).getByText('更早报告')).toBeInTheDocument());
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.traceDiagnosticReports.list'
    )).at(-1)?.request.query).toMatchObject({ cursor: `200.${'1'.repeat(32)}` });
  });

  it('projects the complete audit closure and opens frozen Evidence in a dialog', async () => {
    const reportId = `trace-report:${'c'.repeat(32)}`;
    const report = persistedTraceReportFixture(reportId, 'completed');
    Object.assign(report.inspection as Record<string, unknown>, {
      timeline: [{
        evidenceId: 'evidence:source',
        targetKey: 'session:session-source',
        kind: 'tool_result',
        status: 'failed',
        summary: 'workspace edit 返回 stale_snapshot。',
        sequence: 2,
        createdAtMs: 120,
        sourceRef: 'trace:source:span:edit',
        traceId: 'trace:source',
      }],
      evidence: [{
        evidenceId: 'evidence:source',
        targetKey: 'session:session-source',
        sourceKind: 'trace_span',
        sourceRef: 'trace:source:span:edit',
        status: 'failed',
        summary: 'workspace edit 返回 stale_snapshot。',
        createdAtMs: 120,
        traceId: 'trace:source',
      }],
      requirements: {
        source: 'user_input',
        items: [{
          requirementId: 'requirement:write',
          statement: '写入目标文件',
          targetKey: 'session:session-source',
          sourceRef: 'session:source:message:user',
          evidenceIds: ['evidence:source'],
        }],
        truncated: false,
      },
      environment: {
        capturedAtMs: 100,
        rubricVersion: 'trace-score-v1',
        targets: [{
          targetKey: 'session:session-source',
          sourceSha256: 'b'.repeat(64),
          modelProfile: 'codex',
          toolProfileVersion: 'control-center-v1',
          executionMode: 'per_action',
          policyRevision: 9,
          workspaceScopeSha256: 'c'.repeat(64),
          shellPolicyVersion: 'workspace-v2',
          runtimeKind: 'pi',
          runtimeGeneration: 3,
          traceInputFingerprints: [`sha256:${'d'.repeat(64)}`],
          traceStatuses: ['failed'],
        }],
        limitations: [],
      },
      truncated: { timeline: false, evidence: false, traceIds: false },
    });
    Object.assign(report.result as Record<string, unknown>, {
      requirementAssessments: [{
        requirementId: 'requirement:write',
        status: 'unsatisfied',
        owner: 'Workspace Writer',
        authority: 'ai_judge_estimate',
        evidenceIds: ['evidence:source'],
        note: '没有通过写入回执。',
      }],
      causalLinks: [{
        linkId: 'causal:write',
        fromEvidenceId: 'evidence:source',
        toEvidenceId: 'evidence:source',
        relation: 'triggered',
        authority: 'ai_judge_estimate',
        confidence: 'high',
        explanation: '写入尝试触发 stale_snapshot。',
      }],
      presentation: {
        headline: '写入没有完成；Tool 使用了过期快照。',
        impact: '目标文件没有产生可验证的新版本。',
        primaryFindingId: 'finding:stale-revision',
        failureAttribution: {
          primaryLayer: 'tool',
          summary: '主要故障在 Tool / Runtime；工作流也共同影响了失败。',
          layers: [
            { layer: 'tool', verdict: 'primary', explanation: '写入 Tool 返回 stale_snapshot。', evidenceIds: ['evidence:source'] },
            { layer: 'skill', verdict: 'healthy', explanation: 'Skill 已要求重新读取后写入。', evidenceIds: ['evidence:source'] },
            { layer: 'template', verdict: 'unknown', explanation: '没有冻结模板提示证据。', evidenceIds: [] },
            { layer: 'workflow', verdict: 'contributing', explanation: '批准等待后没有刷新快照。', evidenceIds: ['evidence:source'] },
            { layer: 'model', verdict: 'not_applicable', explanation: '本次失败由确定性 Tool 错误解释。', evidenceIds: [] },
          ],
        },
        knownFacts: [{ fact: '写入 Tool 返回 stale_snapshot。', evidenceIds: ['evidence:source'] }],
        evidenceGaps: [],
        causalNodes: [],
        expectedStageCount: 1,
        recordedStageReceiptEvidenceIds: [],
      },
    });
    Object.assign(report as unknown as Record<string, unknown>, {
      repairLifecycle: {
        authorization: {
          state: 'authorized',
          authorizationKind: 'repair_handoff',
          writeAuthority: 'auto_approved_full_trust',
          authorizationId: 'repair-authorization:1',
          findingId: 'finding:stale-revision',
          sourceScope: 'session:session-source',
          sourceTraceId: 'trace:source',
          failureRef: 'evidence:source',
          repairSessionId: 'agent:repair',
          authorizedAtMs: 220,
        },
        verification: {
          state: 'pending',
          repairReceiptId: '',
          repairTraceId: '',
          evalRunId: '',
          testStatus: '',
          sandboxStatus: '',
          sandboxedTestCount: 0,
          verifiedAtMs: 0,
          comparison: {
            status: 'pending',
            reason: '已授权全信任自动批准修复交接；所有 Tool 操作无需逐项审批，等待修复 Trace 中已记录的修改与通过测试证据，以及 AI Judge 复检。',
            sourceStatus: 'failed',
            repairStatus: '',
            sourceFingerprint: `sha256:${'d'.repeat(64)}`,
            repairFingerprint: '',
            beforeMetrics: {},
            afterMetrics: {},
            deltas: {},
          },
        },
      },
    });
    const scorecard = (report.inspection as unknown as { scorecard: { dimensions: Array<Record<string, unknown>> } }).scorecard;
    const roomDimension = scorecard.dimensions.find((dimension) => dimension.dimensionId === 'room_collaboration');
    Object.assign(roomDimension ?? {}, {
      applicability: 'not_applicable',
      score: null,
      note: '当前对象没有 Room 协作边界。',
    });
    const transport = traceAgentTransport({ diagnosticReport: report });
    const routes: string[] = [];
    renderFeature(transport, routes, [`/trace-agent?reportId=${encodeURIComponent(reportId)}`]);

    const page = await screen.findByRole('region', { name: 'Trace 诊断网页报告' });
    expect(page).toHaveTextContent('用户需求完成矩阵');
    expect(page).toHaveTextContent('跨 Session / Agent 因果时间线');
    expect(page).toHaveTextContent('可复现环境快照');
    expect(page).toHaveTextContent('完整冻结');
    expect(page).toHaveTextContent('用户已确认全信任修复：全磁盘、全部 Tools 自动批准');
    expect(page).toHaveTextContent('等待修复 Trace、测试证据与 AI Judge 复检');
    expect(page).toHaveTextContent('Host 沙盒测试证据');
    expect(page).not.toHaveTextContent('沙盒回放');
    expect(page).toHaveTextContent('修复前后 Trace / Eval 对照');
    expect(page).toHaveTextContent('查看完整冻结时间线');
    expect(page).toHaveTextContent('问题出在哪一层');
    expect(page).toHaveTextContent('主要故障在 Tool / Runtime');
    await userEvent.setup().click(within(page).getByText('证据详情与技术附录'));
    expect(within(page).getByRole('listitem', { name: 'Tool / Runtime：主要责任' })).toBeInTheDocument();
    expect(within(page).getByRole('listitem', { name: 'Skill：正常' })).toBeInTheDocument();
    expect(within(page).getByRole('listitem', { name: '模板提示：未知' })).toBeInTheDocument();
    expect(within(page).getByRole('listitem', { name: '工作流：共同影响' })).toBeInTheDocument();
    expect(within(page).getByRole('listitem', { name: '模型能力：不适用' })).toBeInTheDocument();
    expect(within(page).getByTestId('trace-diagnostic-scorecard').querySelector('[aria-label="查看证据 evidence:source"]')).toBeInTheDocument();
    const roomScoreRow = within(page).getByTestId('trace-diagnostic-scorecard').querySelector('[data-dimension-id="room_collaboration"]');
    expect(roomScoreRow).toHaveTextContent('不适用');
    expect(roomScoreRow).toHaveTextContent('不评分');

    await userEvent.setup().click(within(page).getByRole('button', { name: 'trace:source' }));
    expect(routes).toContain('/observability?traceId=trace%3Asource');

    await userEvent.setup().click(within(page).getAllByRole('button', { name: '查看证据 evidence:source' })[0]);
    const dialog = await screen.findByRole('dialog', { name: 'Evidence 详情' });
    expect(dialog).toHaveTextContent('trace_span');
    expect(dialog).toHaveTextContent('workspace edit 返回 stale_snapshot');
    expect(dialog).toHaveTextContent('trace:source:span:edit');
  });

  it('loads canonical Session and Room targets beyond the initial selector windows', async () => {
    const user = userEvent.setup();
    const sessions = Array.from({ length: 201 }, (_, index) => ({
      id: `session-${index}`,
      title: index === 200 ? '最早的 Session' : `Session ${index}`,
      status: 'idle',
      updatedAtMs: 1_000 - index,
      workspaceRoots: [],
    }));
    const rooms = Array.from({ length: 101 }, (_, index) => ({
      id: `room-${index}`,
      title: index === 100 ? '最早的 Room' : `Room ${index}`,
      status: 'archived',
      updatedAtMs: 1_000 - index,
      participantCount: 2,
    }));
    const transport = traceAgentTransport({ rooms, sessions });
    renderFeature(transport, []);

    expect(await screen.findAllByRole('listitem')).toHaveLength(200);
    expect(screen.queryByRole('listitem', { name: /最早的 Session/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '加载更多 Session' }));
    expect(await screen.findByRole('listitem', { name: /最早的 Session/ })).toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.sessions.list'
      && request.query?.limit === 200
      && request.query?.beforeUpdatedAtMs === 801
      && request.query?.beforeId === 'session-199')).toBe(true);

    await user.click(screen.getByRole('tab', { name: 'Room 协作' }));
    expect(screen.queryByRole('listitem', { name: /最早的 Room/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '加载更多 Room' }));
    expect(await screen.findByRole('listitem', { name: /最早的 Room/ })).toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.rooms.list'
      && request.query?.limit === 100
      && request.query?.beforeUpdatedAtMs === 901
      && request.query?.beforeId === 'room-99')).toBe(true);
  });
});

function renderFeature(transport: MockControlTransport, routes: string[], initialEntries = ['/trace-agent?view=new']) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={client}>
          <TooltipProvider>
            <PawOsDesktopProvider openRoute={(route) => routes.push(route)} openWindow={() => undefined}>
              <TraceAgentFeature />
            </PawOsDesktopProvider>
          </TooltipProvider>
        </QueryClientProvider>
      </ControlTransportProvider>
    </MemoryRouter>,
  );
}

function traceAgentTransport(options: {
  repairTrace?: boolean;
  repairTraceHasTestEvidence?: boolean;
  repairPromptFailures?: number;
  serverRejectChange?: boolean;
  serverRejectTest?: boolean;
  rooms?: Array<Record<string, unknown>>;
  sessions?: Array<Record<string, unknown>>;
  sourceSnapshot?: unknown;
  sourceSnapshots?: unknown[];
  repairSessionSnapshot?: unknown;
  diagnosticSnapshots?: unknown[];
  diagnosticSnapshot?: () => unknown;
  diagnosticPrompt?: (request: ControlRequest) => unknown;
  diagnosticReports?: TraceDiagnosticReportListV1;
  diagnosticReportPages?: TraceDiagnosticReportListV1[];
  diagnosticReport?: TraceDiagnosticReportV1;
  diagnosticFinalizeReport?: TraceDiagnosticReportV1;
  observationSource?: unknown;
  runObservationSnapshots?: Array<{ beforeSequence: number; snapshot: unknown }>;
  traceDetails?: Record<string, unknown | null | Promise<unknown>>;
  roomSourceSnapshot?: unknown;
  roomSourceSnapshots?: unknown[];
  roomHistoryPage?: unknown;
  pickedWorkspaceRoot?: string;
  replayEvidence?: boolean;
} = {}) {
  let diagnosticSnapshotIndex = 0;
  let sourceSnapshotIndex = 0;
  let roomSourceSnapshotIndex = 0;
  let repairPromptAttempts = 0;
  let changeEvidence: Record<string, unknown> | null = null;
  let testEvidence: Record<string, unknown> | null = null;
  let repairReceipt: Record<string, unknown> | null = null;
  let diagnosticReportPageIndex = 0;
  const defaultReportId = `trace-report:${'d'.repeat(32)}`;
  let activeDiagnosticReport = options.diagnosticReport
    ?? persistedTraceReportFixture(defaultReportId, 'generating');
  const diagnosticSnapshots = options.diagnosticSnapshots ?? [diagnosticSessionSnapshot()];
  const sourceSnapshots = options.sourceSnapshots ?? [options.sourceSnapshot ?? sessionSourceSnapshot()];
  const roomSourceSnapshots = options.roomSourceSnapshots ?? [options.roomSourceSnapshot ?? roomSnapshot()];
  const sessionItems = options.sessions ?? [{
    id: 'session-source',
    title: '失败的对话',
    mode: 'assistant',
    status: 'idle',
    updatedAtMs: 100,
    workspaceRoots: ['/workspace/paw'],
    messageCount: 4,
    lastMessagePreview: 'write/edit validation error',
  }];
  const replayCase = replayCaseFixture();
  const verificationReceipt = traceVerificationReceiptFixture();
  return new MockControlTransport({
    ...(options.pickedWorkspaceRoot ? {
      pickedFiles: [{
        id: 'picked:trace-project',
        name: options.pickedWorkspaceRoot.split('/').filter(Boolean).at(-1) ?? 'project',
        mimeType: 'inode/directory',
        byteSize: 0,
        path: options.pickedWorkspaceRoot,
      }],
    } : {}),
    routes: {
      'agent.sessions.list': (request: ControlRequest) => paginatedTargetList(sessionItems, request, 200),
      'agent.rooms.list': (request: ControlRequest) => paginatedTargetList(
        options.rooms ?? [{ id: 'room-source', title: '失败的协作', status: 'active', updatedAtMs: 110, participantCount: 3, workspaceRoots: ['/workspace/room'] }],
        request,
        100,
      ),
      'observability.snapshot': (request: ControlRequest) => {
        const sessionId = request.query?.sessionId;
        if (sessionId === 'agent:trace-repair') {
          return options.repairTrace === false ? emptyObservationSnapshot() : repairObservationSnapshot();
        }
        if (request.query?.runId && options.runObservationSnapshots) {
          const beforeSequence = Number(request.query.beforeSequence ?? 0);
          const page = options.runObservationSnapshots.find((candidate) => candidate.beforeSequence === beforeSequence);
          if (page) return page.snapshot;
        }
        return options.observationSource ?? observationSnapshot();
      },
      'agent.session.snapshot': (request: ControlRequest) => {
        if (request.params?.sessionId === 'agent:trace-diagnostic') {
          if (options.diagnosticSnapshot) return options.diagnosticSnapshot();
          const index = Math.min(diagnosticSnapshotIndex++, Math.max(0, diagnosticSnapshots.length - 1));
          return diagnosticSnapshots[index] ?? emptyDiagnosticSessionSnapshot();
        }
        if (request.params?.sessionId === 'agent:trace-repair') {
          return options.repairSessionSnapshot ?? repairSessionSnapshot();
        }
        const index = Math.min(sourceSnapshotIndex++, Math.max(0, sourceSnapshots.length - 1));
        return sourceSnapshots[index] ?? emptyDiagnosticSessionSnapshot();
      },
      'observability.trace.get': (request: ControlRequest) => {
        const traceId = String(request.params?.traceId ?? 'trace:source');
        if (options.traceDetails && Object.prototype.hasOwnProperty.call(options.traceDetails, traceId)) {
          return options.traceDetails[traceId];
        }
        return traceResponse(traceId, {
          repairTraceHasTestEvidence: options.repairTraceHasTestEvidence !== false,
        });
      },
      'agent.room.snapshot': () => {
        const index = Math.min(roomSourceSnapshotIndex++, Math.max(0, roomSourceSnapshots.length - 1));
        return roomSourceSnapshots[index] ?? roomSnapshot();
      },
      'agent.room.history': () => options.roomHistoryPage ?? emptyRoomHistoryPage(),
      'agent.sessions.create': (request: ControlRequest) => ({
        ok: true,
        session: {
          id: String((request.body as Record<string, unknown> | undefined)?.title).includes('修复')
            ? 'agent:trace-repair'
            : 'agent:trace-diagnostic',
        },
      }),
      'agent.session.mode.update': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown> | undefined;
        const source = sessionItems.find((item) => item.id === request.params?.sessionId);
        if (source && Array.isArray(body?.workspaceRoots)) {
          source.mode = String(body.mode ?? source.mode ?? 'coordinator');
          source.executionMode = String(body.executionMode ?? source.executionMode ?? 'per_action');
          source.toolProfileVersion = String(body.toolProfileVersion ?? source.toolProfileVersion ?? 'control-center-v1');
          source.workspaceRoots = body.workspaceRoots.map(String);
          source.updatedAtMs = Number(source.updatedAtMs ?? 0) + 1;
        }
        return { ok: true, ...(source ? { session: source } : {}) };
      },
      'agent.session.prompt': (request: ControlRequest) => {
        if (request.params?.sessionId === 'agent:trace-diagnostic' && options.diagnosticPrompt) return options.diagnosticPrompt(request);
        const message = String((request.body as Record<string, unknown> | undefined)?.message ?? '');
        if (
          message.includes('Trace Agent 的候选修复交接')
          && repairPromptAttempts++ < (options.repairPromptFailures ?? 0)
        ) throw new Error('repair prompt transport failed');
        return { ok: true };
      },
      'observability.traceDiagnosticReports.list': () => {
        if (options.diagnosticReportPages?.length) {
          const page = options.diagnosticReportPages[
            Math.min(diagnosticReportPageIndex++, options.diagnosticReportPages.length - 1)
          ];
          if (page) return page;
        }
        return options.diagnosticReports ?? {
          schemaVersion: 'rag-ime.trace-diagnostic-report-list.v1',
          total: 0,
          truncated: false,
          nextCursor: null,
          items: [],
        };
      },
      'observability.traceDiagnosticReport.get': () => {
        if (!activeDiagnosticReport) throw new Error('No diagnostic report fixture registered');
        return activeDiagnosticReport;
      },
      'observability.traceDiagnosticReports.create': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        const requestedTargets = Array.isArray(body.targets)
          ? body.targets.map((rawTarget) => {
              const target = rawTarget as Record<string, unknown>;
              const kind = String(target.kind ?? '');
              const id = String(target.id ?? '');
              const traceIds = Array.isArray(target.traceIds)
                ? target.traceIds.map(String).filter(Boolean)
                : [];
              return {
                targetKey: `${kind}:${id}`,
                kind,
                id,
                title: String(target.title ?? id),
                traceIds: traceIds.length ? traceIds : ['trace:source'],
                sourceAvailable: true,
              };
            }) as TraceDiagnosticReportV1['targets']
          : activeDiagnosticReport.targets;
        activeDiagnosticReport = {
          ...activeDiagnosticReport,
          targets: requestedTargets,
          traceIds: [...new Set(requestedTargets.flatMap((target) => target.traceIds))],
        };
        return activeDiagnosticReport;
      },
      'observability.traceDiagnosticReport.finalize': () => {
        const finalized = options.diagnosticFinalizeReport
          ?? persistedTraceReportFixture(activeDiagnosticReport.reportId, 'completed');
        activeDiagnosticReport = options.diagnosticFinalizeReport
          ? finalized
          : {
              ...finalized,
              targets: activeDiagnosticReport.targets,
              traceIds: activeDiagnosticReport.traceIds,
            };
        return activeDiagnosticReport;
      },
      'observability.traceDiagnosticReport.repairAuthorize': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        activeDiagnosticReport = {
          ...activeDiagnosticReport,
          revision: Number(body.expectedRevision) + 1,
          repairLifecycle: {
            authorization: {
              state: 'authorized',
              authorizationKind: 'repair_handoff',
              writeAuthority: 'auto_approved_full_trust',
              authorizationId: 'repair-authorization:server-issued',
              findingId: String(body.findingId),
              sourceScope: String(body.sourceScope),
              sourceTraceId: String(body.sourceTraceId),
              failureRef: String(body.failureRef),
              repairSessionId: String(body.repairSessionId),
              authorizedAtMs: 250,
            },
            verification: {
              state: 'pending',
              repairReceiptId: '',
              repairTraceId: '',
              evalRunId: '',
              testStatus: '',
              sandboxStatus: '',
              sandboxedTestCount: 0,
              verifiedAtMs: 0,
              comparison: {
                status: 'pending',
                reason: '已授权全信任自动批准修复交接；所有 Tool 操作无需逐项审批，等待修复 Trace 中已记录的修改与通过测试证据，以及 AI Judge 复检。',
                sourceStatus: 'failed',
                repairStatus: '',
                sourceFingerprint: `sha256:${'d'.repeat(64)}`,
                repairFingerprint: '',
                beforeMetrics: {},
                afterMetrics: {},
                deltas: {},
              },
            },
          },
          updatedAtMs: 250,
        };
        return activeDiagnosticReport;
      },
      'observability.traceDiagnosticReport.repairVerify': (request: ControlRequest) => {
        if (!repairReceipt || !activeDiagnosticReport.repairLifecycle) throw new Error('repair verification arrived before authorization or receipt');
        const body = request.body as Record<string, unknown>;
        activeDiagnosticReport = {
          ...activeDiagnosticReport,
          revision: Number(body.expectedRevision) + 1,
          repairLifecycle: {
            authorization: activeDiagnosticReport.repairLifecycle.authorization,
            verification: {
              state: 'verified',
              repairReceiptId: String(body.repairReceiptId),
              repairTraceId: String(repairReceipt.repairTraceId),
              evalRunId: 'eval:trace-agent:recheck:repair',
              testStatus: 'passed',
              sandboxStatus: 'passed',
              sandboxedTestCount: Number(repairReceipt.sandboxedTestCount),
              verifiedAtMs: 320,
              comparison: {
                status: 'incomparable',
                reason: '输入 fingerprint 不同，只能并列展示，不能声称效果提升。',
                sourceStatus: 'failed',
                repairStatus: 'completed',
                sourceFingerprint: `sha256:${'d'.repeat(64)}`,
                repairFingerprint: `sha256:${'e'.repeat(64)}`,
                beforeMetrics: { task_completion: 0 },
                afterMetrics: { task_success: 1 },
                deltas: {},
              },
            },
          },
          updatedAtMs: 320,
        };
        return activeDiagnosticReport;
      },
      'observability.traceRepair.changeEvidence': (request: ControlRequest) => {
        const body = request.body as Record<string, unknown>;
        if (options.serverRejectChange) throw new Error('修复运行没有已完成的修改工具证据');
        expect(body).toEqual({
          schemaVersion: 'rag-ime.trace-repair-change-evidence.v1',
          repairSessionId: 'agent:trace-repair',
          repairTraceId: 'trace:turn:turn-repair',
        });
        changeEvidence = {
          schemaVersion: 'rag-ime.trace-repair-evidence.v1',
          evidenceId: 'change-evidence:server-issued',
          evidenceKind: 'change',
          sourceScope: 'trace-repair',
          sourceTraceId: body.repairTraceId,
          testStatus: '',
          evidence: {
            schemaVersion: 'rag-ime.trace-repair-canonical-evidence.v1',
            evidenceKind: 'change',
            repairSessionId: body.repairSessionId,
            repairTraceId: body.repairTraceId,
            eventCount: 1,
            completedCount: 1,
            toolCount: 1,
            toolNames: ['workspace_patch'],
            signalIds: ['event:edit'],
            changeCount: 1,
          },
          createdAtMs: 300,
        };
        return { schemaVersion: 'rag-ime.trace-repair-evidence-write.v1', ok: true, evidence: changeEvidence };
      },
      'observability.traceRepair.testEvidence': (request: ControlRequest) => {
        if (!changeEvidence) throw new Error('test evidence arrived before change evidence');
        if (options.serverRejectTest) throw new Error('修复运行没有已通过的测试工具证据');
        const body = request.body as Record<string, unknown>;
        expect(body).toEqual({
          schemaVersion: 'rag-ime.trace-repair-test-evidence.v1',
          repairSessionId: 'agent:trace-repair',
          repairTraceId: 'trace:turn:turn-repair',
        });
        testEvidence = {
          schemaVersion: 'rag-ime.trace-repair-evidence.v1',
          evidenceId: 'test-evidence:server-issued',
          evidenceKind: 'test',
          sourceScope: 'trace-repair',
          sourceTraceId: body.repairTraceId,
          testStatus: 'passed',
          evidence: {
            schemaVersion: 'rag-ime.trace-repair-canonical-evidence.v1',
            evidenceKind: 'test',
            repairSessionId: body.repairSessionId,
            repairTraceId: body.repairTraceId,
            eventCount: 1,
            completedCount: 1,
            toolCount: 1,
            toolNames: ['bash'],
            signalIds: ['event:test'],
            testCount: 1,
            passedCount: 1,
            failedCount: 0,
            status: 'passed',
          },
          createdAtMs: 301,
        };
        return { schemaVersion: 'rag-ime.trace-repair-evidence-write.v1', ok: true, evidence: testEvidence };
      },
      'observability.traceRepair.receipt.create': (request: ControlRequest) => {
        if (!changeEvidence || !testEvidence) throw new Error('receipt arrived before evidence');
        const body = request.body as Record<string, unknown>;
        if (body.repairSessionId !== 'agent:trace-repair') throw new Error('receipt repair Session binding mismatch');
        repairReceipt = {
          schemaVersion: 'rag-ime.trace-repair-receipt.v1',
          repairReceiptId: 'repair-receipt:server-issued',
          sourceScope: body.sourceScope,
          sourceTraceId: body.sourceTraceId,
          failureRef: body.failureRef,
          changeReceiptId: body.changeReceiptId,
          testEvidenceId: body.testEvidenceId,
          testStatus: 'passed',
          sandboxStatus: 'passed',
          sandboxedTestCount: 1,
          repairTraceId: body.repairTraceId,
          repairSessionId: body.repairSessionId,
          createdAtMs: 302,
        };
        return { schemaVersion: 'rag-ime.trace-repair-receipt-create.v1', ok: true, receipt: repairReceipt };
      },
      'observability.traceRepair.receipt.get': (request: ControlRequest) => {
        if (!repairReceipt) throw new Error('receipt was not created');
        if (request.params?.repairReceiptId !== repairReceipt.repairReceiptId) throw new Error('wrong receipt id');
        return { schemaVersion: 'rag-ime.trace-repair-receipt-get.v1', ok: true, receipt: repairReceipt };
      },
      'observability.traceRepair.recheck': (request: ControlRequest) => {
        if (!repairReceipt) throw new Error('recheck arrived before receipt');
        const body = request.body as Record<string, unknown>;
        if (Object.keys(body).sort().join(',') !== 'repairReceiptId,schemaVersion') throw new Error('recheck must be receipt-id-only');
        if (body.repairReceiptId !== repairReceipt.repairReceiptId) throw new Error('wrong receipt id');
        return {
          schemaVersion: 'rag-ime.trace-repair-recheck.v1',
          ok: true,
          receipt: repairReceipt,
          evalRun: aiJudgeRunResponse(repairReceipt),
          idempotent: false,
        };
      },
      'observability.evals.list': (request: ControlRequest) => {
        const traceId = String(request.query?.traceId ?? '');
        const items = options.replayEvidence
          ? traceId === 'trace:source'
            ? [groundTruthEvalFixture('eval:baseline', 'trace:source', 0.25, 140, 120)]
            : traceId === 'trace:turn:turn-repair'
              ? [groundTruthEvalFixture('eval:repair', 'trace:turn:turn-repair', 1, 100, 90)]
              : []
          : [];
        return {
          schemaVersion: 'rag-ime.observability-eval-list.v1',
          traceId,
          total: items.length,
          truncated: false,
          items,
        };
      },
      'observability.sandboxRuns.list': () => ({
        schemaVersion: 'rag-ime.observability-sandbox-run-list.v1',
        ok: true,
        items: options.replayEvidence
          ? [sandboxRunFixture('sandbox:repair', 'trace:turn:turn-repair', ['eval:repair', 'eval:regression'], false), sandboxRunFixture('sandbox:baseline', 'trace:source', ['eval:baseline'], false)]
          : [],
        total: options.replayEvidence ? 2 : 0,
      }),
      'observability.sandboxRun.get': (request: ControlRequest) => {
        const sandboxRunId = String(request.params?.sandboxRunId ?? '');
        if (sandboxRunId === 'sandbox:baseline') return sandboxRunFixture('sandbox:baseline', 'trace:source', ['eval:baseline'], true);
        if (sandboxRunId === 'sandbox:repair') return sandboxRunFixture('sandbox:repair', 'trace:turn:turn-repair', ['eval:repair', 'eval:regression'], true);
        throw new Error('sandbox run not found');
      },
      'observability.traceReplay.case.create': () => ({
        schemaVersion: 'rag-ime.trace-replay-case-create.v1',
        ok: true,
        replayCase,
      }),
      'observability.traceReplay.case.get': (request: ControlRequest) => {
        if (request.params?.replayCaseId !== replayCase.replayCaseId) throw new Error('wrong replay case id');
        return { schemaVersion: 'rag-ime.trace-replay-case-get.v1', ok: true, replayCase };
      },
      'observability.traceReplay.verify': () => ({
        schemaVersion: 'rag-ime.trace-verification-receipt-create.v1',
        ok: true,
        verificationReceipt,
      }),
      'observability.traceReplay.verification.get': (request: ControlRequest) => {
        if (request.params?.verificationReceiptId !== verificationReceipt.verificationReceiptId) throw new Error('wrong verification receipt id');
        return { schemaVersion: 'rag-ime.trace-verification-receipt-get.v1', ok: true, verificationReceipt };
      },
      'observability.evals.evidence.run': () => ({
        schemaVersion: 'rag-ime.eval-run.v1',
        evalRunId: 'eval:trace-agent:recheck:repair',
        traceIds: ['trace:repair'],
        mode: 'ground_truth',
        metricAuthority: 'ground_truth',
        truth: { status: 'human', datasetId: 'trace-agent:recheck', labelRevision: 'repair:agent:trace-repair' },
        evaluator: { provider: 'deterministic', model: 'labels', thinking: 'none', displayName: 'Human labels' },
        metrics: { precision: 1, recall: 1, f1: 1 },
        status: 'completed',
        createdAtMs: 120,
        updatedAtMs: 120,
      }),
      'observability.evals.aiJudge.run': () => aiJudgeRunResponse(),
    },
  });
}

function paginatedTargetList(
  items: Array<Record<string, unknown>>,
  request: ControlRequest,
  defaultLimit: number,
) {
  const beforeId = String(request.query?.beforeId ?? '');
  const cursorIndex = beforeId ? items.findIndex((item) => item.id === beforeId) : -1;
  const start = cursorIndex >= 0 ? cursorIndex + 1 : 0;
  const limit = Number(request.query?.limit ?? defaultLimit);
  const pageItems = items.slice(start, start + limit);
  const hasMore = start + pageItems.length < items.length;
  const last = pageItems.at(-1);
  return {
    ok: true,
    items: pageItems,
    hasMore,
    nextBeforeUpdatedAtMs: hasMore ? Number(last?.updatedAtMs ?? 0) : null,
    nextBeforeId: hasMore ? String(last?.id ?? '') : null,
  };
}

function observationSnapshot() {
  const item = {
    schemaVersion: 'rag-ime.observation-event.v1' as const,
    eventType: 'observation' as const,
    eventId: 'observation-source',
    sequence: 1,
    resumeToken: 'observation:1',
    traceId: 'trace:source',
    spanId: 'span:source',
    parentSpanId: '',
    sessionId: 'session-source',
    roomId: 'room-source',
    turnId: 'turn-source',
    runId: 'run-source',
    category: 'tool' as const,
    phase: 'tool_finished',
    name: 'workspace_write',
    status: 'failed' as const,
    summary: 'write/edit validation error: target file changed',
    createdAtMs: 100,
    startedAtMs: 100,
    endedAtMs: 120,
    durationMs: 20,
    privacyClass: 'metadata' as const,
    metrics: {},
    attributes: {},
    refs: [{ kind: 'session', id: 'session-source', label: '失败的对话' }],
  };
  return {
    schemaVersion: 'rag-ime.observation-snapshot.v1' as const,
    generatedAtMs: 120,
    firstSequence: 1,
    lastSequence: 1,
    resumeToken: 'observation:1',
    truncated: false,
    filters: {},
    counts: { total: 1, byCategory: { tool: 1 }, byStatus: { failed: 1 } },
    items: [item],
  };
}

function sessionSourceSnapshot() {
  return {
    ok: true,
    sessionId: 'session-source',
    status: 'idle',
    items: [
      {
        id: 'message-user-source',
        sessionId: 'session-source',
        turnId: 'turn-source',
        role: 'user',
        status: 'completed',
        timelineSequence: 1,
        createdAtMs: 101,
        blocks: [{ id: 'user-text-source', type: 'text', status: 'completed', data: { text: '检查当前实现' } }],
      },
      {
        id: 'message-assistant-source',
        sessionId: 'session-source',
        turnId: 'turn-source',
        role: 'assistant',
        status: 'completed',
        timelineSequence: 5,
        createdAtMs: 150,
        blocks: [{ id: 'assistant-text-source', type: 'text', status: 'completed', data: { content: '发现写入版本冲突' } }],
      },
      {
        id: 'message-error-source',
        sessionId: 'session-source',
        turnId: 'turn-source',
        role: 'assistant',
        status: 'failed',
        createdAtMs: 155,
        blocks: [{ id: 'error-source', type: 'error', status: 'failed', data: { content: 'write/edit validation error: target file changed' } }],
      },
    ],
    liveEvents: [
      {
        eventId: 'reasoning-source',
        eventType: 'reasoning_summary',
        timelineSequence: 2,
        sequence: 2,
        createdAtMs: 110,
        payload: { summary: '核对失败证据' },
      },
      {
        eventId: 'tool-started-source',
        eventType: 'tool_started',
        timelineSequence: 3,
        sequence: 3,
        createdAtMs: 120,
        payload: { summary: '读取目标文件', toolId: 'workspace', operation: 'read' },
      },
      {
        eventId: 'tool-finished-source',
        eventType: 'tool_finished',
        timelineSequence: 4,
        sequence: 4,
        createdAtMs: 140,
        payload: { summary: '读取目标文件完成', toolId: 'workspace', operation: 'read' },
      },
    ],
  };
}

function diagnosticSessionSnapshot() {
  return {
    ok: true,
    sessionId: 'agent:trace-diagnostic',
    status: 'idle',
    items: [
      {
        id: 'message-user-diagnostic',
        sessionId: 'agent:trace-diagnostic',
        turnId: 'turn-diagnostic',
        role: 'user',
        status: 'completed',
        timelineSequence: 1,
        createdAtMs: 201,
        blocks: [{ id: 'user-text-diagnostic', type: 'text', status: 'completed', data: { text: '请诊断当前失败' } }],
      },
      {
        id: 'message-assistant-diagnostic',
        sessionId: 'agent:trace-diagnostic',
        turnId: 'turn-diagnostic',
        role: 'assistant',
        status: 'completed',
        timelineSequence: 3,
        createdAtMs: 230,
        blocks: [{ id: 'assistant-text-diagnostic', type: 'text', status: 'completed', data: { text: [
          '根因是资源版本回执不合法；建议重新读取后再编辑。',
          '现象：写入文件失败。',
          '影响：本轮操作未完成。',
          'Trace/span/run 证据：trace:source。',
          '可能根因：资源版本回执不合法。',
          '置信度/未知边界：中等，尚未重放。',
          '候选修复：重新读取后再编辑。',
          '如何用沙盒或 Eval 验证：运行最小回归测试。',
          '可回跳的 Trace/Session/Room/文件：trace:source。',
          '--- TRACE_DIAGNOSTIC_RESULT_V1 ---',
          JSON.stringify({
            schemaVersion: 'rag-ime.trace-diagnostic-result.v1',
            summary: '根因是资源版本回执不合法；建议重新读取后再编辑。',
            hardGates: [],
            judgeScores: [],
            findings: [],
          }),
          '--- END_TRACE_DIAGNOSTIC_RESULT_V1 ---',
        ].join('\n') } }],
      },
    ],
    liveEvents: [{
      eventId: 'tool-finished-diagnostic',
      eventType: 'tool_finished',
      timelineSequence: 2,
      sequence: 2,
      createdAtMs: 220,
      payload: { summary: '已核对 Trace 与原始对话', toolId: 'trace', operation: 'inspect' },
    }],
  };
}

function repairSessionSnapshot(options: { receipt?: string; failureRef?: string; actualTestEvidence?: boolean; actualChangeEvidence?: boolean } = {}) {
  const failureRef = options.failureRef ?? 'trace:observation-source';
  const receipt = options.receipt ?? [
    'TRACE_REPAIR_EVIDENCE',
    JSON.stringify({
      sourceScope: 'session:session-source',
      sourceTraceId: 'trace:source',
      failureRef,
      repairTraceId: 'trace:repair',
      changeEvidence: { files: ['control-center-web/src/example.ts'], operations: ['write'] },
      testEvidence: { commands: ['pnpm test --filter trace-repair'], results: [{ status: 'completed', exitCode: 0 }] },
      testStatus: 'passed',
      // These are deliberately attacker-controlled-looking claims. The
      // frontend must ignore them and use IDs returned by the server.
      changeReceiptId: 'attacker-change-id',
      testEvidenceId: 'attacker-test-id',
    }, null, 2),
  ].join('\n');
  const blocks = [
    ...(options.actualChangeEvidence === false ? [] : [{
      id: 'tool-edit-repair',
      type: 'tool',
      status: 'completed',
      data: { name: 'write file', operation: 'write', status: 'completed' },
    }]),
    ...(options.actualTestEvidence === false ? [] : [{
      id: 'tool-test-repair',
      type: 'command',
      status: 'completed',
      data: { command: 'pnpm test --filter trace-repair', status: 'completed', exitCode: 0 },
    }]),
    { id: 'assistant-text-repair', type: 'text', status: 'completed', data: { text: receipt } },
  ];
  return {
    ok: true,
    sessionId: 'agent:trace-repair',
    status: 'idle',
    items: [{
      id: 'message-assistant-repair',
      sessionId: 'agent:trace-repair',
      turnId: 'turn-repair',
      role: 'assistant',
      status: 'completed',
      timelineSequence: 3,
      createdAtMs: 260,
      blocks,
    }],
    liveEvents: [],
  };
}

function streamingDiagnosticSessionSnapshot() {
  const snapshot = diagnosticSessionSnapshot();
  return {
    ...snapshot,
    status: 'busy',
    items: snapshot.items.map((item) => item.role === 'assistant'
      ? {
        ...item,
        status: 'streaming',
        blocks: item.blocks.map((block) => ({ ...block, status: 'running' })),
      }
      : item),
  };
}

function emptyDiagnosticSessionSnapshot() {
  return {
    ok: true,
    sessionId: 'agent:trace-diagnostic',
    status: 'busy',
    items: [],
    liveEvents: [],
  };
}

function longSourceSnapshot(count: number, eventPrefix: string) {
  return {
    ok: true,
    sessionId: 'session-source',
    status: 'idle',
    items: [{
      id: 'message-long',
      sessionId: 'session-source',
      turnId: 'turn-long',
      role: 'assistant',
      status: 'completed',
      timelineSequence: count,
      createdAtMs: count,
      blocks: [{
        id: 'text-long',
        type: 'text',
        status: 'completed',
        data: { text: `完整消息开头 ${'原始内容'.repeat(50)} 完整消息结尾` },
      }],
    }],
    liveEvents: Array.from({ length: Math.max(0, count - 1) }, (_, index) => ({
      eventId: `event-long-${index + 1}`,
      eventType: 'reasoning_summary',
      timelineSequence: index + 1,
      sequence: index + 1,
      createdAtMs: index + 1,
      payload: { summary: `${eventPrefix} ${index + 1}` },
    })),
  };
}

function observationEvent(sequence: number, sessionId: string, roomId: string) {
  const base = observationSnapshot().items[0];
  return {
    ...base,
    eventId: `observation-source-${sequence}-${sessionId}`,
    sequence,
    resumeToken: `observation:${sequence}`,
    sessionId,
    roomId,
    traceId: 'trace:source',
    spanId: `span:source:${sequence}`,
    name: `workspace_write_${sequence}`,
    summary: `run event ${sequence}`,
    createdAtMs: sequence * 100,
    startedAtMs: sequence * 100,
    endedAtMs: sequence * 100 + 20,
  };
}

function runObservationSnapshot(items: Array<ReturnType<typeof observationEvent>>, truncated: boolean) {
  const base = observationSnapshot();
  return {
    ...base,
    generatedAtMs: Math.max(...items.map((item) => item.createdAtMs), 0),
    firstSequence: Math.min(...items.map((item) => item.sequence), 0),
    lastSequence: Math.max(...items.map((item) => item.sequence), 0),
    resumeToken: `observation:run:${Math.max(...items.map((item) => item.sequence), 0)}`,
    truncated,
    counts: {
      total: items.length,
      byCategory: { tool: items.length },
      byStatus: { failed: items.length },
    },
    items,
  };
}

function canonicalTraceResponse(sessionId: string, roomId: string) {
  const response = traceResponse('trace:source');
  return {
    ...response,
    trace: {
      ...response.trace,
      binding: { ...response.trace.binding, sessionId, roomId, runId: 'run-source' },
    },
  };
}

function repairObservationSnapshot() {
  const base = observationSnapshot();
  const item = {
    ...base.items[0],
    eventId: 'observation-repair',
    sequence: 2,
    resumeToken: 'observation:repair:2',
    traceId: 'trace:repair',
    spanId: 'span:repair',
    sessionId: 'agent:trace-repair',
    summary: 'repair completed',
    status: 'completed' as const,
    createdAtMs: 200,
    startedAtMs: 180,
    endedAtMs: 200,
    durationMs: 20,
    refs: [{ kind: 'session', id: 'agent:trace-repair', label: '修复 Session' }],
  };
  return {
    ...base,
    generatedAtMs: 200,
    firstSequence: 2,
    lastSequence: 2,
    resumeToken: 'observation:repair:2',
    counts: { total: 1, byCategory: { tool: 1 }, byStatus: { completed: 1 } },
    items: [item],
  };
}

function emptyObservationSnapshot() {
  const base = observationSnapshot();
  return {
    ...base,
    generatedAtMs: 200,
    firstSequence: 0,
    lastSequence: 0,
    resumeToken: 'observation:repair:0',
    counts: { total: 0, byCategory: {}, byStatus: {} },
    items: [],
  };
}

function traceResponse(traceId: string, options: { repairTraceHasTestEvidence?: boolean } = {}) {
  const isRepair = traceId === 'trace:repair';
  const sessionId = isRepair ? 'agent:trace-repair' : 'session-source';
  const evidenceId = isRepair ? 'knowledge:repair' : 'knowledge:source';
  return {
    schemaVersion: 'rag-ime.observability-trace-get.v1' as const,
    traceId,
    trace: {
      schemaVersion: 'rag-ime.trace-envelope.v1' as const,
      traceId,
      sourceKind: 'agent' as const,
      status: 'completed' as const,
      binding: { sessionId, roomId: 'room-source', runId: isRepair ? 'run-repair' : 'run-source' },
      input: { fingerprint: 'sha256:' + '0'.repeat(64), contentPolicy: 'hash_only' as const, normalization: 'test' },
      spans: isRepair && options.repairTraceHasTestEvidence !== false ? [{
        spanId: 'span:repair:test',
        name: 'tool.call',
        parentSpanId: null,
        status: 'completed' as const,
        startedAtMs: 200,
        endedAtMs: 210,
        durationMs: 10,
        recorded: true,
        unavailableReason: '',
        metrics: {},
        attributes: { toolName: 'workspace_shell', command: 'pnpm test --filter trace-repair', exitCode: 0 },
      }] : [],
      evidence: [{
        evidenceId,
        sourceKind: 'knowledge' as const,
        sourceRef: `knowledge://${isRepair ? 'repair' : 'source'}`,
        sourceLane: 'test',
        evidenceStage: 'retrieval_output',
        disposition: 'included' as const,
        scores: {},
        rankBefore: null,
        rankAfter: null,
        omissionReason: '',
      }],
      artifacts: [],
      createdAtMs: isRepair ? 200 : 100,
      updatedAtMs: isRepair ? 220 : 120,
    },
    truncated: false,
    projectionSource: 'trace_store' as const,
    observationWindow: {
      firstSequence: isRepair ? 2 : 1,
      lastSequence: isRepair ? 2 : 1,
      resumeToken: `trace-store:${traceId}`,
      nextBeforeSequence: null,
    },
  };
}

function aiJudgeRunResponse(receipt?: Record<string, unknown> | null) {
  return {
    schemaVersion: 'rag-ime.eval-run.v1' as const,
    evalRunId: 'eval:trace-agent:recheck:independent',
    traceIds: ['trace:repair'],
    mode: 'ai_judge' as const,
    metricAuthority: 'ai_judge_estimate' as const,
    truth: { status: 'none' as const, datasetId: 'trace-eval-ai-judge', labelRevision: 'trace-eval-ai-judge-v1' },
    evaluator: { provider: 'openai-codex', model: 'gpt-5.6-luna', thinking: 'max', displayName: 'Trace recheck' },
    metrics: { groundedness: 1, confidence: 1 },
    status: 'completed' as const,
    ...(receipt ? {
      sourceTraceId: receipt.sourceTraceId,
      repairTraceId: receipt.repairTraceId,
      sourceScope: receipt.sourceScope,
      failureRef: receipt.failureRef,
      repairReceiptId: receipt.repairReceiptId,
      changeReceiptId: receipt.changeReceiptId,
      testEvidenceId: receipt.testEvidenceId,
      testStatus: 'passed' as const,
    } : {}),
    createdAtMs: 120,
    updatedAtMs: 120,
  };
}

const REPLAY_COHORT = {
  suiteId: 'trace-suite',
  suiteRevision: 'suite-v1',
  caseId: 'case-one',
  inputFingerprint: `sha256:${'1'.repeat(64)}`,
  environmentFingerprint: `sha256:${'2'.repeat(64)}`,
  configFingerprint: `sha256:${'3'.repeat(64)}`,
  modelProfileFingerprint: `sha256:${'4'.repeat(64)}`,
  toolProfileFingerprint: `sha256:${'5'.repeat(64)}`,
  skillProfileFingerprint: `sha256:${'6'.repeat(64)}`,
};

function groundTruthEvalFixture(
  evalRunId: string,
  traceId: string,
  accuracy: number,
  _latencyMs: number,
  _totalTokens: number,
) {
  return {
    evalRunId,
    mode: 'ground_truth' as const,
    metricAuthority: 'ground_truth' as const,
    truthStatus: 'frozen' as const,
    datasetId: 'trace-replay',
    labelRevision: 'labels-v1',
    evaluatorDisplayName: 'Frozen labels',
    suiteBinding: { suiteId: REPLAY_COHORT.suiteId, suiteRevision: REPLAY_COHORT.suiteRevision },
    metrics: { accuracy },
    status: 'completed' as const,
    createdAtMs: traceId === 'trace:source' ? 100 : 300,
    updatedAtMs: traceId === 'trace:source' ? 110 : 310,
  };
}

function sandboxRunFixture(
  sandboxRunId: string,
  traceId: string,
  evalRunIds: string[],
  includeCohort: boolean,
) {
  return {
    schemaVersion: 'rag-ime.sandbox-run.v1' as const,
    sandboxRunId,
    appId: 'extension:trace-agent',
    status: 'completed' as const,
    policy: {
      workspaceBindingId: 'workspace:paw',
      workspaceFingerprint: `sha256:${'7'.repeat(64)}`,
      mutationMode: 'staged' as const,
      network: 'blocked' as const,
      productionWriteBlocked: true as const,
    },
    ...(includeCohort ? { replayCohort: REPLAY_COHORT } : {}),
    traceIds: traceId === 'trace:source' ? [traceId] : [traceId, 'trace:regression'],
    evalRunIds,
    createdAtMs: traceId === 'trace:source' ? 100 : 300,
    updatedAtMs: traceId === 'trace:source' ? 110 : 310,
  };
}

function replayCaseFixture() {
  return {
    schemaVersion: 'rag-ime.trace-replay-case.v1' as const,
    replayCaseId: 'replay-case:server-issued',
    sourceScope: 'session:session-source',
    failureRef: 'trace:observation-source',
    sourceTraceId: 'trace:source',
    baselineEvalRunId: 'eval:baseline',
    baselineSandboxRunId: 'sandbox:baseline',
    replayCohort: REPLAY_COHORT,
    successCriterion: { metric: 'accuracy', threshold: 1, direction: 'at_least' as const },
    baselineMetricValue: 0.25,
    rollbackTarget: `source-sha256:${'b'.repeat(64)}`,
    createdAtMs: 200,
  };
}

function traceVerificationReceiptFixture() {
  return {
    schemaVersion: 'rag-ime.trace-verification-receipt.v1' as const,
    verificationReceiptId: 'trace-verification:server-issued',
    replayCaseId: 'replay-case:server-issued',
    repairReceiptId: 'repair-receipt:server-issued',
    sourceTraceId: 'trace:source',
    repairTraceId: 'trace:turn:turn-repair',
    baselineEvalRunId: 'eval:baseline',
    repairEvalRunId: 'eval:repair',
    baselineSandboxRunId: 'sandbox:baseline',
    repairSandboxRunId: 'sandbox:repair',
    regressionEvalRunIds: ['eval:regression'],
    replayCohort: REPLAY_COHORT,
    successCriterion: { metric: 'accuracy', threshold: 1, direction: 'at_least' as const },
    repairPassed: true,
    regression: { count: 1, passed: true, failedEvalRunIds: [] },
    comparison: { status: 'available' as const, metric: 'accuracy', before: 0.25, after: 1, absoluteDelta: 0.75, relativeDelta: 3 },
    efficiency: {
      latencyMs: { before: 140, after: 100, delta: -40 },
      totalTokens: { before: 120, after: 90, delta: -30 },
    },
    decision: 'kept' as const,
    rollbackTarget: `source-sha256:${'b'.repeat(64)}`,
    createdAtMs: 400,
  };
}

function persistedTraceReportFixture(
  reportId: string,
  status: TraceDiagnosticReportV1['status'] = 'completed',
): TraceDiagnosticReportV1 {
  const dimensions = [
    'task_completion',
    'evidence_diagnosis',
    'tool_runtime',
    'context',
    'room_collaboration',
    'memory_rag',
    'efficiency',
    'repair_quality',
  ].map((dimensionId) => ({
    dimensionId,
    title: dimensionId,
    applicability: 'measured',
    authority: 'deterministic',
    score: 72,
    scoreMax: 100,
    metrics: [],
    evidenceIds: ['evidence:source'],
    note: 'fixture',
  }));
  return {
    schemaVersion: 'rag-ime.trace-diagnostic-report.v1',
    reportId,
    revision: 2,
    status,
    title: 'Trace 诊断 · 失败的对话',
    diagnosticSessionId: 'agent:trace-diagnostic',
    targets: [{
      targetKey: 'session:session-source',
      kind: 'session',
      id: 'session-source',
      title: '失败的对话',
      traceIds: ['trace:source'],
      sourceAvailable: true,
    }],
    traceIds: ['trace:source'],
    inspectionSha256: 'a'.repeat(64),
    inspection: {
      timeline: [{
        evidenceId: 'trace:observation-source',
        targetKey: 'session:session-source',
        kind: 'tool_result',
        status: 'failed',
        summary: 'write/edit validation error: target file changed',
        sequence: 2,
        createdAtMs: 120,
        sourceRef: 'observation:observation-source',
        traceId: 'trace:source',
      }],
      evidence: [{
        evidenceId: 'trace:observation-source',
        targetKey: 'session:session-source',
        sourceKind: 'observation',
        sourceRef: 'observation:observation-source',
        status: 'failed',
        summary: 'write/edit validation error: target file changed',
        createdAtMs: 120,
        traceId: 'trace:source',
      }],
      requirements: { source: 'unknown', items: [], truncated: false },
      environment: {
        capturedAtMs: 100,
        rubricVersion: 'trace-score-v1',
        targets: [{
          targetKey: 'session:session-source',
          sourceSha256: 'b'.repeat(64),
          modelProfile: 'codex',
          toolProfileVersion: 'control-center-v1',
          executionMode: 'per_action',
          policyRevision: 1,
          workspaceScopeSha256: 'c'.repeat(64),
          shellPolicyVersion: 'workspace-v2',
          runtimeKind: 'pi',
          runtimeGeneration: 1,
          traceInputFingerprints: [`sha256:${'d'.repeat(64)}`],
          traceStatuses: ['failed'],
        }],
        limitations: ['fixture is partial'],
      },
      truncated: { timeline: false, evidence: false, traceIds: false },
      scorecard: {
        rubricVersion: 'trace-score-v1',
        dimensions,
        hardGates: [],
        comparison: { eligible: false, status: 'unknown', reason: 'fixture' },
      },
    },
    result: status === 'completed' ? {
      schemaVersion: 'rag-ime.trace-diagnostic-result.v1',
      summary: 'fixture report',
      hardGates: [],
      judgeScores: [],
      requirementAssessments: [],
      causalLinks: [],
      findings: [{
        findingId: 'finding:source-failure',
        dimensionId: 'tool_runtime',
        severity: 'high',
        observation: 'write/edit validation error: target file changed',
        hypothesis: 'source revision changed',
        conclusion: 'workspace write used stale source state',
        confidence: 'high',
        evidenceIds: ['trace:observation-source'],
        candidateRepair: 're-read and prepare the write again',
        verification: 'new Trace and tests must pass',
      }],
      presentation: {
        headline: '写入没有完成；Tool 使用了过期快照。',
        impact: '目标文件没有产生可验证的新版本。',
        primaryFindingId: 'finding:source-failure',
        failureAttribution: {
          primaryLayer: 'tool',
          summary: '主要故障在 Tool / Runtime；工作流也共同影响了失败。',
          layers: [
            { layer: 'tool', verdict: 'primary', explanation: '写入 Tool 使用了过期状态。', evidenceIds: ['trace:observation-source'] },
            { layer: 'skill', verdict: 'healthy', explanation: 'Skill 正确要求刷新状态。', evidenceIds: ['trace:observation-source'] },
            { layer: 'template', verdict: 'unknown', explanation: '没有冻结模板提示证据。', evidenceIds: [] },
            { layer: 'workflow', verdict: 'contributing', explanation: '工作流没有在写入前刷新状态。', evidenceIds: ['trace:observation-source'] },
            { layer: 'model', verdict: 'not_applicable', explanation: '确定性 Tool 错误已解释失败。', evidenceIds: [] },
          ],
        },
        knownFacts: [{ fact: '写入 Tool 返回 stale_snapshot。', evidenceIds: ['trace:observation-source'] }],
        evidenceGaps: [],
        causalNodes: [],
        expectedStageCount: 1,
        recordedStageReceiptEvidenceIds: [],
      },
    } : null,
    failureReason: status === 'failed' ? '结构化结果缺失' : '',
    createdAtMs: 100,
    updatedAtMs: 200,
  };
}

function traceReportSummary(
  report: TraceDiagnosticReportV1,
): TraceDiagnosticReportListV1['items'][number] {
  return {
    reportId: report.reportId,
    revision: report.revision,
    status: report.status,
    title: report.title,
    diagnosticSessionId: report.diagnosticSessionId,
    targetKeys: report.targets.map((target) => target.targetKey) as TraceDiagnosticReportListV1['items'][number]['targetKeys'],
    targets: report.targets,
    traceIds: report.traceIds,
    repairState: report.repairLifecycle?.verification?.state === 'verified'
      ? 'verified'
      : report.repairLifecycle?.verification?.state === 'failed'
        ? 'failed'
        : report.repairLifecycle?.authorization?.state === 'authorized'
          ? 'authorized'
          : 'not_recorded',
    failureReason: report.failureReason,
    createdAtMs: report.createdAtMs,
    updatedAtMs: report.updatedAtMs,
  };
}

function emptyRoomHistoryPage() {
  return {
    schemaVersion: 'rag-ime.agent-room-event-page.v1' as const,
    ok: true as const,
    roomId: 'room-source',
    items: [],
    firstSequence: 0,
    lastSequence: 0,
    nextBeforeSequence: 0,
    hasMore: false,
    retainedFirstSequence: 1,
    retainedLastSequence: 0,
    retainedPrefixTruncated: false,
  };
}

function roomSnapshot() {
  const participant = (id: string, ordinal: number, sessionId: string) => ({
    schemaVersion: 'rag-ime.agent-participant.v1' as const,
    id,
    roomId: 'room-source',
    sessionId,
    roleId: `role-${id}`,
    roleVersion: '1',
    displayName: id,
    collaborationRole: ordinal === 1 ? 'coordinator' as const : 'reviewer' as const,
    status: 'active' as const,
    ordinal,
    createdAtMs: 1,
    lastSpokeAtMs: 100,
  });
  return {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1' as const,
    ok: true as const,
    room: {
      schemaVersion: 'rag-ime.agent-room.v1' as const,
      id: 'room-source',
      title: '失败的协作',
      status: 'active' as const,
      executionMode: 'read_only' as const,
      roomKind: 'collaboration' as const,
      routingPolicy: 'moderator' as const,
      moderatorParticipantId: 'participant-1',
      workspaceRoots: [] as [],
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1' as const,
        room: { executionMode: 'read_only' as const },
        partner: { executionMode: 'inherit' as const },
        toolAgent: { executionMode: 'inherit' as const },
      },
      createdAtMs: 1,
      updatedAtMs: 110,
      lastEventSequence: 6,
      participants: [participant('participant-1', 1, 'session-source'), participant('participant-2', 2, 'session-secondary')],
      topics: [],
      artifacts: [],
      workItems: [],
    },
    events: [
      {
        schemaVersion: 'rag-ime.agent-room-event.v1' as const,
        eventId: 'room-user-source',
        roomId: 'room-source',
        sequence: 1,
        turnId: 'turn-source',
        eventType: 'user_message' as const,
        participantId: null,
        sourceSessionId: 'session-source',
        createdAtMs: 101,
        payload: { text: 'Room 用户问题' },
        resumeToken: 'room-source:1',
      },
      {
        schemaVersion: 'rag-ime.agent-room-event.v1' as const,
        eventId: 'room-reasoning-source',
        roomId: 'room-source',
        sequence: 2,
        turnId: 'turn-source',
        eventType: 'participant_activity' as const,
        participantId: 'participant-1',
        sourceSessionId: 'session-source',
        createdAtMs: 110,
        payload: { sourceEventType: 'reasoning_summary', data: { summary: '协作者核对分工' } },
        resumeToken: 'room-source:2',
      },
      {
        schemaVersion: 'rag-ime.agent-room-event.v1' as const,
        eventId: 'room-tool-started-source',
        roomId: 'room-source',
        sequence: 3,
        turnId: 'turn-source',
        eventType: 'participant_activity' as const,
        participantId: 'participant-1',
        sourceSessionId: 'session-source',
        createdAtMs: 120,
        payload: { sourceEventType: 'tool_started', data: { toolName: 'read', summary: '读取 WorkItem' } },
        resumeToken: 'room-source:3',
      },
      {
        schemaVersion: 'rag-ime.agent-room-event.v1' as const,
        eventId: 'room-tool-finished-source',
        roomId: 'room-source',
        sequence: 4,
        turnId: 'turn-source',
        eventType: 'participant_activity' as const,
        participantId: 'participant-1',
        sourceSessionId: 'session-source',
        createdAtMs: 130,
        payload: { sourceEventType: 'tool_finished', data: { toolName: 'read', summary: 'WorkItem 读取完成' } },
        resumeToken: 'room-source:4',
      },
      {
        schemaVersion: 'rag-ime.agent-room-event.v1' as const,
        eventId: 'room-post-source',
        roomId: 'room-source',
        sequence: 5,
        turnId: 'turn-source',
        eventType: 'room_post' as const,
        participantId: 'participant-1',
        sourceSessionId: 'session-source',
        createdAtMs: 140,
        payload: { post: { content: 'Room 已发布进展' } },
        resumeToken: 'room-source:5',
      },
      {
        schemaVersion: 'rag-ime.agent-room-event.v1' as const,
        eventId: 'room-event-source',
        roomId: 'room-source',
        sequence: 6,
        turnId: 'turn-source',
        eventType: 'turn_failed' as const,
        participantId: 'participant-1',
        sourceSessionId: 'session-source',
        createdAtMs: 150,
        payload: { error: '协作失败：重复分派后未能收口' },
        resumeToken: 'room-source:6',
      },
    ],
    firstSequence: 1,
    lastSequence: 6,
    resumeToken: 'room-source:6',
    truncated: false,
  };
}
