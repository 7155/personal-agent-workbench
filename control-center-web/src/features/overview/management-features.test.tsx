import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentType } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { ConfigurationFeature } from '@/features/configuration';
import { DiagnosticsFeature } from '@/features/diagnostics';
import { HistoryFeature } from '@/features/history';
import { InputMethodFeature } from '@/features/input-method';
import { KnowledgeFeature } from '@/features/knowledge';
import { MemoryFeature } from '@/features/memory';
import { PluginsFeature } from '@/features/plugins';
import { ApprovalsFeature } from '@/features/approvals';
import { VoiceFeature } from '@/features/voice';
import type { ControlPathId } from '@/platform/routes';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import { StubControlTransport } from '@/test/stub-control-transport';
import { WorkflowAction, publicErrorText } from './management-ui';
import { parseManagementWorkPreview } from './management-mutation';

const now = 1_752_499_200_000;

const routeFixtures: Partial<Record<ControlPathId, MockRouteHandler>> = {
  'system.health': { ok: true, status: 'ready' },
  'overview.get': {
    ok: true,
    profile: '标准模式',
    aiPaused: false,
    components: {
      inputMethod: { id: 'inputMethod', ok: true, status: 'ready', detail: '已选择' },
      predictor: { id: 'predictor', ok: true, status: 'ready', detail: 'MLX resident' },
    },
    memory: { eventCount: 18, memoryItemCount: 14, memoryBookCount: 4, memoryAtomCount: 10, retrievalDocCount: 22, pendingCompileEvents: 2 },
    lastPrediction: { visibleCandidate: '继续完成控制中心迁移', triggerReason: 'post_commit', contextSource: 'foreground', totalLatencyMs: 124, sourceTypes: ['model', 'rag'] },
  },
  'agent.runtime.get': {
    schemaVersion: 'rag-ime.agent-runtime.v1',
    enabled: true,
    managed: true,
    status: 'ready',
    driverId: 'pi-managed',
    runtimeKind: 'pi',
    runtimeVersion: 'test',
    piVersion: 'test',
    idleTimeoutSeconds: 900,
    activeSessionId: null,
    lastError: '',
    capabilities: {},
  },
  'agent.tools.list': {
    schemaVersion: 'rag-ime.capability-catalog.v1',
    ok: true,
    revision: `sha256:${'a'.repeat(64)}`,
    effectiveAtMs: 1,
    projectScope: {
      supported: false,
      identityKind: 'none',
      reason: 'stable_project_identity_unavailable',
    },
    items: [{
      id: 'memory.search',
      canonicalId: 'tool:memory.search',
      kind: 'tool',
      displayName: '记忆检索',
      description: '搜索本地记忆',
      source: { kind: 'product', label: 'Personal Agent Workbench' },
      status: 'online',
      risk: 'R0',
      requiredPermissions: [],
      authorization: { state: 'not_applicable', reason: 'session_context_required' },
      disclosure: {
        preference: 'inherit',
        effective: 'enabled',
        state: 'disclosed',
        reason: 'inherited_built_in_default',
      },
      effectiveScope: 'built_in_default',
      reasons: ['inherited_built_in_default'],
      revision: 'tool-spec:1',
      effectiveAtMs: 1,
      domain: 'memory',
      category: 'memory',
      operations: ['search'],
      availability: 'online',
    }],
  },
  'agent.approvals.list': { ok: true, items: [] },
  'agent.sessions.list': { ok: true, items: [] },
  'input.source.get': {
    ok: true,
    inputSourceId: 'im.rime.inputmethod.Squirrel.Rime',
    typingReady: true,
    selected: true,
    readinessState: 'ready',
    readinessMessage: 'foreground typing ready',
  },
  'planning.dashboard': {
    ok: true,
    runtimeRevision: 7,
    date: '2026-07-14',
    plan: { intention: '完成 Web 迁移', notes: '保持接口边界', reflection: '', project: 'wisdom-weasel-rag-ime' },
    tasks: [{ id: 'task-1', title: '完成管理页', detail: '运行测试并截图', status: 'in_progress', source: 'manual' }],
    goals: [{ id: 'goal-1', title: '统一控制中心', detail: 'React 可见端', status: 'active', targetDate: '2026-07-15' }],
    pendingCompletionSuggestions: [],
    recentDetectedCompletion: null,
    summary: { taskCount: 1, openTaskCount: 1, completedTaskCount: 0, goalCount: 1, progress: 0 },
    assistant: { message: '先完成可验证的迁移闭环。', tone: 'focus' },
  },
  'memory.summary': { ok: true, eventCount: 18, memoryItemCount: 14, memoryBookCount: 4, memoryAtomCount: 10, pendingCompileEvents: 2 },
  'memory.pages': {
    ok: true,
    items: [{ id: 'book-1', title: '控制中心迁移', summary: 'React 页面与受控 API', source: 'user', status: 'active' }],
    nextCursor: '',
    limit: 50,
  },
  'history.page': {
    ok: true,
    items: [{ id: 81, createdAtMs: now, source: 'rime_commit', app: 'TextEdit', project: 'wisdom-weasel-rag-ime', textPreview: '完成了...', textChars: 6, contextHash: 'sha256:test' }],
    nextCursor: '',
    limit: 50,
    rawTextVisible: false,
  },
  'knowledge.routeStatus': {
    ok: true,
    deepseekReady: true,
    notion: { submitConfigured: true, pollConfigured: false, ready: false, pollMode: 'worker' },
  },
  'knowledge.status': { ok: false, status: 'missing', error: 'sessionId is required', evidence: [], sources: [] },
  'knowledgeBases.list': { ok: true, items: [], total: 0 },
  'knowledgeWorker.health': { ok: true, available: true, status: 'ready' },
  'knowledgeParsers.list': {
    ok: true,
    items: [
      { id: 'auto', name: '自动', available: true },
      { id: 'builtin', name: '内置解析', available: true },
      { id: 'mineru_local_http', name: 'MinerU', available: false },
    ],
  },
  'diagnostics.runtime': {
    ok: true,
    components: {
      sidecar: { id: 'sidecar', ok: true, status: 'ready', detail: '运行中' },
      sqlite: { id: 'sqlite', ok: true, status: 'ready', detail: 'FTS5 ready' },
    },
  },
  'diagnostics.predictor': { ok: true, predictor: { status: 'ready', provider: 'mlx', model: 'qwen3-0.6b' } },
  'diagnostics.models': { ok: true, schemaVersion: 'rag-ime.models-status.v3', predictor: { status: 'ready', provider: 'mlx' }, activeRagRoute: { remoteReady: true, skipReason: '' } },
  'configuration.settings': {
    ok: true,
    settingsRevision: 'settings-test',
    runtimeRevision: 4,
    settings: {
      interaction: { postCommit: { enabled: true } },
      voice: { provider: 'native-streaming', tokenConfigured: true, hotkey: 'middle-mouse', hotwordsEnabled: false },
      security: { apiToken: 'top-secret-must-not-render' },
    },
  },
  'configuration.schema': {
    ok: true,
    schemaVersion: 'rag-ime.settings-schema.v1',
    sections: [
      {
        id: 'interaction',
        label: '交互',
        fields: [
          { key: 'interaction.postCommit.enabled', type: 'boolean', label: '提交后预测', description: '提交后调度助手候选', applyMode: 'live', expert: false },
          { key: 'security.apiToken', type: 'secret', label: 'API Token', description: 'Keychain secret', applyMode: 'restart_sidecar', expert: false },
        ],
      },
      { id: 'display', label: '显示', fields: [] },
      { id: 'activeRag', label: 'RAG', fields: [] },
      { id: 'pinyin', label: '拼音', fields: [] },
      { id: 'voice', label: '语音', fields: [] },
    ],
  },
};

const pages: readonly [string, ComponentType, string, ControlPathId][] = [
  ['input', InputMethodFeature, '输入法', 'input.source.get'],
  ['plugins', PluginsFeature, '插件管理', 'agent.tools.list'],
  ['approvals', ApprovalsFeature, '审批中心', 'agent.approvals.list'],
  ['voice', VoiceFeature, '语音输入', 'configuration.settings'],
  ['memory', MemoryFeature, '我的记忆', 'memory.pages'],
  ['knowledge', KnowledgeFeature, '知识库', 'knowledgeBases.list'],
  ['history', HistoryFeature, '输入记录', 'history.page'],
  ['diagnostics', DiagnosticsFeature, '问题排查', 'diagnostics.runtime'],
  ['configuration', ConfigurationFeature, '设置', 'configuration.schema'],
];

afterEach(cleanup);

describe('management features', () => {
  it('keeps implementation details out of user-facing errors', () => {
    expect(publicErrorText(new Error('角色名称已存在'))).toBe('角色名称已存在');
    expect(publicErrorText(new Error('POST /api/memory/action failed: payloadSha256 mismatch')))
      .toBe('操作未完成，请刷新状态后重试。');
    expect(publicErrorText(new Error('sqlite3.OperationalError at /tmp/service.py:41')))
      .toBe('操作未完成，请刷新状态后重试。');
    expect(publicErrorText(new Error('template service unavailable'), '暂时无法读取。'))
      .toBe('暂时无法读取。');
    expect(publicErrorText(new Error('profileVersion 不匹配，请重试')))
      .toBe('操作未完成，请刷新状态后重试。');
  });

  it('keeps WorkContract identifiers out of confirmation summaries', () => {
    const preview = parseManagementWorkPreview({
      ok: true,
      previewToken: 'preview-test',
      pathId: 'planning.task.save',
      payloadSha256: 'sha256:test',
      expectedRevision: { runtimeRevision: 4 },
      expiresAtMs: Date.now() + 60_000,
      requiredConfirm: 'apply',
      summary: {
        title: 'pathId operationId 绑定结果',
        items: ['记录 ID: 81', '只更新你选择的任务。'],
        risk: 'R1',
      },
    }, 'planning.task.save', {});

    expect(preview.summary.title).toBe('确认本次变更');
    expect(preview.summary.items).toEqual(['只更新你选择的任务。']);
  });

  it.each(pages)('%s renders from its allowlisted read boundary', async (_id, Feature, heading, expectedPathId) => {
    const transport = renderFeature(Feature);
    expect(await screen.findByRole('heading', { name: heading, level: 1 })).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.some((call) => call.request.pathId === expectedPathId)).toBe(true));
    expect(screen.queryByText('top-secret-must-not-render')).not.toBeInTheDocument();
  });

  it('uses the live runtime revision without exposing internal hashes', async () => {
    renderFeature(ConfigurationFeature, {
      ...routeFixtures,
      'configuration.settings': {
        schemaVersion: 'rag-ime.management-settings.v3',
        ok: true,
        settingsHash: 'sha256:live-settings-hash',
        settings: { interaction: { postCommit: { enabled: true } } },
        runtimeConfig: {
          runtimeRevision: 554,
          settingsRevision: 'sha256:effective-settings',
        },
      },
      'configuration.schema': {
        ok: true,
        schemaVersion: 'rag-ime.settings-schema.v1',
        sections: [{
          id: 'agent',
          label: 'Agent',
          fields: [{
            key: 'agent.pi.enabled',
            type: 'boolean',
            label: '启用 Pi',
            description: '启用 Agent 对话运行时',
            applyMode: 'live',
            expert: false,
          }],
        }],
      },
    });

    expect(await screen.findByText('调整称呼、本机模型、上下文和各项功能。普通设置可直接保存；涉及重启、部署或权限的更改会先说明影响。')).toBeInTheDocument();
    expect(screen.queryByText('sha256:live-settings-hash')).not.toBeInTheDocument();
    expect(screen.queryByText('sha256:effective-settings')).not.toBeInTheDocument();
    expect(screen.queryByText('554')).not.toBeInTheDocument();
  });

  it('renders the live predictor capability without exposing provider implementation names', async () => {
    const user = userEvent.setup();
    renderFeature(DiagnosticsFeature, {
      ...routeFixtures,
      'diagnostics.predictor': {
        schemaVersion: 'rag-ime.predictor-status.v1',
        ok: true,
        predictor: {
          providerName: 'local-mlx',
          model: '/Models/minimind-ime-v2',
          modelInfo: { hiddenSize: 768 },
          capabilities: {},
        },
      },
    });

    expect((await screen.findAllByText('本机模型')).length).toBeGreaterThan(0);
    expect(screen.queryByText('local-mlx')).not.toBeInTheDocument();
    await user.click(screen.getByText('高级：诊断详情'));
    expect(screen.getByText('768')).toBeInTheDocument();
  });

  it('keeps Voice explicitly unavailable when the backend exposes no voice state', async () => {
    renderFeature(VoiceFeature, {
      ...routeFixtures,
      'configuration.settings': {
        schemaVersion: 'rag-ime.management-settings.v3',
        ok: true,
        settings: { interaction: { postCommit: { enabled: true } } },
        runtimeConfig: { runtimeRevision: 554 },
      },
      'diagnostics.runtime': {
        schemaVersion: 'rag-ime.management.v1',
        ok: true,
        components: {
          sidecar: { id: 'sidecar', ok: true, status: 'healthy', detail: '运行中' },
        },
      },
    });

    expect(await screen.findByText('网页端不能启动听写或打开系统授权；请回到已安装的PAW。')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: '实时听写' })).toBeInTheDocument();
    expect(screen.queryByText(/middle-mouse/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '尚不可预览' })).not.toBeInTheDocument();
    expect(screen.queryByText('unavailable')).not.toBeInTheDocument();
  });

  it('never offers a rehearsal receipt in the native host', () => {
    const transport = new StubControlTransport('native', {});
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={client}>
          <WorkflowAction
            actionId="memory.archive"
            description="归档一条记忆"
            mutationKey={['memory', 'archive']}
            preview={['memoryId: memory-1']}
            title="归档记忆"
          />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    expect(screen.queryByRole('button', { name: '当前不可用' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '查看示例' })).not.toBeInTheDocument();
  });
});

function renderFeature(
  Feature: ComponentType,
  routes: Partial<Record<ControlPathId, MockRouteHandler>> = routeFixtures,
): MockControlTransport {
  const transport = new MockControlTransport({ routes });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <MemoryRouter>
      <TooltipProvider delayDuration={0}>
        <ControlTransportProvider transport={transport}>
          <QueryClientProvider client={client}>
            <Feature />
          </QueryClientProvider>
        </ControlTransportProvider>
      </TooltipProvider>
    </MemoryRouter>,
  );
  return transport;
}
