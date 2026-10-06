import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import { InputLexiconFeature, InputMethodFeature } from './index';
import {
  completionLaneFacts,
  inferInputMode,
  inputModeChanges,
  modeFactChips,
  modeSettingLabel,
  predictionLiveTraceSummaries,
  presetInputModes,
  recallLaneFacts,
  secondsLabel,
  suggestionPanel,
} from './input-method-presentation';
import inputMethodCss from './input-method.css?raw';

const settings = {
  ok: true,
  settings: { interaction: { postCommit: { enabled: true } } },
};

const schema = {
  ok: true,
  sections: [{
    id: 'interaction',
    label: '交互',
    fields: [{
      key: 'interaction.postCommit.enabled',
      type: 'boolean',
      label: '提交后预测',
      description: '提交后调度助手候选',
      applyMode: 'live',
    }],
  }],
};

const emptyReview = {
  schemaVersion: 'rag-ime.rime-lexicon-review.v1',
  ok: true,
  project: 'wisdom-weasel-rag-ime',
  entryCount: 0,
  entries: [],
  reviewToken: 'a'.repeat(64),
  confirmText: 'APPLY_REVIEWED_RIME_LEXICON',
  applySupported: true,
  reviewRequired: true,
  organization: {
    schemaVersion: 'rag-ime.lexicon-organization-status.v1',
    owner: 'maintenance_poll',
    decoderOwner: 'rime',
    enabled: true,
    runsPerDay: 2,
    intervalMs: 43_200_000,
    candidateLimit: 200,
    lastRunAtMs: 1_783_000_000_000,
    lastSucceededAtMs: 1_783_000_000_000,
    nextRunAtMs: 1_783_043_200_000,
    due: false,
    lastRun: {
      runId: 'lexorg-1',
      status: 'succeeded',
      startedAtMs: 1_783_000_000_000,
      completedAtMs: 1_783_000_000_000,
      candidateCount: 4,
      filteredEntryCount: 2,
      errorCode: '',
      error: '',
    },
  },
};

const review = {
  ...emptyReview,
  entryCount: 1,
  entries: [{
    reviewKey: '表情包\tbiao qing bao',
    text: '表情包',
    pinyin: 'biao qing bao',
    weight: 180,
    positiveCount: 3,
    negativeCount: 0,
    reasons: ['accepted'],
    reviewSource: 'usage',
    reviewReason: '来自真实选词反馈',
    selected: true,
  }],
};

afterEach(cleanup);

describe('InputMethodFeature', () => {
  it('keeps input settings refresh available while the independent live Trace is still loading', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
      'overview.get': { ok: true, profile: '标准模式' },
      'configuration.settings': settings,
      'configuration.schema': schema,
      'diagnostics.models': { ok: true },
      'input.prediction.liveTrace': () => new Promise(() => {}),
    } });
    renderFeature(transport);
    await screen.findByText('正在等待本机最近请求的摘要。');
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled());
    const before = transport.requests.filter(({ request }) => request.pathId === 'configuration.settings').length;
    await user.click(screen.getByRole('button', { name: '刷新' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'configuration.settings')).toHaveLength(before + 1));
  });

  it.each(['empty', 'failed'] as const)('preserves the applied lexicon receipt and exact rollback after a refreshed review is %s', async (refreshResult) => {
    const user = userEvent.setup();
    let refreshed = false;
    const transport = new MockControlTransport({ routes: {
      'input.lexicon.review': () => {
        if (!refreshed) return review;
        if (refreshResult === 'failed') throw new Error('review unavailable');
        return { ...emptyReview, reviewToken: 'b'.repeat(64) };
      },
      'input.lexicon.apply': {
        schemaVersion: 'rag-ime.rime-lexicon-review.v1', ok: true,
        applied: true, entryCount: 1, rollbackId: 'saved-lexicon-write', requiresRedeploy: true,
      },
      'input.lexicon.rollback': {
        schemaVersion: 'rag-ime.rime-lexicon-review.v1', ok: true,
        rolledBack: true, rollbackId: 'saved-lexicon-write', requiresRedeploy: true,
      },
    } });
    renderLexiconFeature(transport);
    await user.click(await screen.findByRole('button', { name: '加入所选词条' }));
    await screen.findByText('词条已加入用户词库');
    refreshed = true;
    await user.click(screen.getByRole('button', { name: '刷新审阅' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新审阅' })).toBeEnabled());
    expect(screen.getByText('词条已加入用户词库')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '撤销这次更新' }));
    await screen.findByText('词库更新已撤销');
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'input.lexicon.review')).toHaveLength(3));
    expect(screen.getByText('词库更新已撤销')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'input.lexicon.apply')).toHaveLength(1);
    expect(transport.requests.find(({ request }) => request.pathId === 'input.lexicon.rollback')?.request.body).toEqual({ rollbackId: 'saved-lexicon-write' });
  });

  it('keeps an in-flight lexicon write bound to its original review while a new review arrives', async () => {
    const user = userEvent.setup();
    let resolve!: (value: unknown) => void;
    const pendingApply = new Promise((done) => { resolve = done; });
    let refreshed = false;
    const transport = new MockControlTransport({ routes: {
      'input.lexicon.review': () => refreshed ? {
        ...review, reviewToken: 'b'.repeat(64), entries: [{ ...review.entries[0], text: '新词条', reviewKey: 'next-entry' }],
      } : review,
      'input.lexicon.apply': () => pendingApply,
    } });
    renderLexiconFeature(transport);
    await user.click(await screen.findByRole('button', { name: '加入所选词条' }));
    refreshed = true;
    await user.click(screen.getByRole('button', { name: '刷新审阅' }));
    expect(await screen.findByRole('checkbox', { name: '选择 新词条' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '加入所选词条' })).toBeDisabled();
    await act(async () => resolve({
      schemaVersion: 'rag-ime.rime-lexicon-review.v1', ok: true,
      applied: true, entryCount: 1, rollbackId: 'original-review-write', requiresRedeploy: true,
    }));
    expect(await screen.findByText('词条已加入用户词库')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '继续审阅' }));
    expect(screen.getByRole('checkbox', { name: '选择 新词条' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '撤销这次更新' })).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => request.pathId === 'input.lexicon.apply')).toHaveLength(1);
    expect(transport.requests.find(({ request }) => request.pathId === 'input.lexicon.apply')?.request.body).toMatchObject({
      reviewToken: review.reviewToken, selectedKeys: [review.entries[0].reviewKey],
    });
  });

  it('does not classify unread settings as a custom input preset', async () => {
    renderFeature(new MockControlTransport({
      routes: {
        'configuration.settings': () => new Promise(() => {}),
        'configuration.schema': schema,
        'overview.get': { ok: true, profile: '标准模式' },
      },
    }));

    expect(screen.queryByText('当前设置不属于任何预设。')).not.toBeInTheDocument();
    expect(await screen.findByText('正在读取当前设置，暂不判断使用方式。')).toBeInTheDocument();
  });

  it('lets mode descriptions grow under Dynamic Type instead of clipping them', () => {
    expect(inputMethodCss).toContain(
      ".input-mode-card__note {\n  display: block;\n  overflow: visible;",
    );
    expect(inputMethodCss).not.toMatch(/\.input-mode-card__note \{[^}]*-webkit-line-clamp: 2;[^}]*\}/);
  });

  it('shows only safe input trace facts and links the latest stable request', async () => {
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true },
        'overview.get': { ok: true, profile: '调试模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.prediction.liveTrace': {
          schemaVersion: 'rag-ime.prediction-live-trace.v1',
          ok: true,
          rawTextVisible: true,
          frames: [
            {
              sessionId: 'input-session',
              requestSeq: 7,
              recordedAtMs: 123,
              input: { text: '绝不显示' },
              traceEvents: [{ reason: '绝不显示' }],
              ragLane: { called: true, predictionCount: 2, elapsedMs: 12 },
              modelLane: { called: true, suggestionCount: 1, elapsedMs: 28 },
            },
          ],
        },
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByText('最近请求')).toBeInTheDocument();
    expect(screen.getByText('会话 input-session · 请求 #7')).toBeInTheDocument();
    expect(screen.getByText('查看标准 Trace')).toHaveAttribute(
      'href',
      '#/observability?traceId=trace%3Aprediction%3Ainput-session%3Arequest-7',
    );
    expect(screen.getByText('知识召回')).toBeInTheDocument();
    expect(screen.getByText('2 条 · 12 ms')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('绝不显示');
  });

  it('updates the read-only current mode when the live overview arrives asynchronously', async () => {
    let resolveOverview!: (value: unknown) => void;
    const overview = new Promise<unknown>((resolve) => { resolveOverview = resolve; });
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': () => overview,
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByText('正在读取模式')).toBeInTheDocument();
    resolveOverview({ ok: true, profile: '安全模式' });

    await waitFor(() => expect(screen.getByText('安全模式')).toBeInTheDocument());
    expect(screen.getByText('保存使用方式')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '当前不可用' })).not.toBeInTheDocument();
  });

  it('does not relabel an unknown live runtime profile as 标准模式', async () => {
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: 'foreground-rag-proof' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByText('自定义模式')).toBeInTheDocument();
    expect(screen.queryByText('foreground-rag-proof')).not.toBeInTheDocument();
  });

  it('humanizes schema labels and enum values instead of exposing implementation fields', async () => {
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': { ok: true, settings: { interaction: { postCommit: { optionNumber: 'provider_internal_v3' } } } },
        'configuration.schema': {
          ok: true,
          sections: [{
            id: 'interaction',
            label: 'interaction',
            fields: [{
              key: 'interaction.postCommit.optionNumber',
              type: 'enum',
              label: 'pathId profileVersion',
              description: '/api/internal/schema',
              applyMode: 'live',
            }],
          }],
        },
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByText('Option+数字行为')).toBeInTheDocument();
    expect(screen.getAllByText('自定义设置').length).toBeGreaterThan(0);
    expect(document.body).not.toHaveTextContent('pathId');
    expect(document.body).not.toHaveTextContent('provider_internal_v3');
    expect(document.body).not.toHaveTextContent('/api/internal/schema');
    expect(modeSettingLabel('rag.lanes.timeDailyBook')).toBe('时间与日记召回');
  });

  it('groups settings by backend schema responsibility and keeps controls disabled without the write contract', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': {
          ok: true,
          inputSourceId: 'im.rime.inputmethod.Squirrel.Hans',
          selected: true,
          typingReady: true,
          readinessState: 'ready',
        },
        'overview.get': {
          ok: true,
          profile: '标准模式',
          components: {
            sidecar: { ok: true, status: 'ready', detail: '后台服务已连接' },
            predictor: { ok: true, status: 'ready', detail: '本机模型已加载' },
            foregroundContext: { ok: false, status: 'degraded', detail: '等待前台应用' },
          },
        },
        'configuration.settings': {
          ok: true,
          settings: {
            interaction: { postCommit: { enabled: true, panelTtlMs: 4000 } },
            display: { maxPostCommitCandidates: 5 },
            activeRag: { latencyBudgetMs: 8000 },
          },
        },
        'configuration.schema': {
          ok: true,
          sections: [
            {
              ...schema.sections[0],
              fields: [
                ...schema.sections[0].fields,
                {
                  key: 'interaction.postCommit.panelTtlMs',
                  type: 'integer',
                  label: '预测面板 TTL',
                  description: '生成结果出现后自动关闭前的停留时间；不会缩短正在生成状态',
                  min: 500,
                  max: 30000,
                  step: 500,
                  unit: 'ms',
                  applyMode: 'restart_input_method',
                },
              ],
            },
            {
              id: 'display',
              label: '显示',
              fields: [{
                key: 'display.maxPostCommitCandidates',
                type: 'integer',
                label: '续写候选数量',
                description: '限制输入完成后显示的候选数',
                applyMode: 'reload',
              }],
            },
            {
              id: 'activeRag',
              label: '主动知识生成',
              fields: [{
                key: 'activeRag.latencyBudgetMs',
                type: 'integer',
                label: '生成框最长等待',
                description: '等待框自动收起前的最长时间',
                min: 2000,
                max: 30000,
                step: 1000,
                unit: 'ms',
                applyMode: 'live',
              }],
            },
          ],
        },
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByRole('heading', { level: 3, name: '输入体验' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 3, name: '候选界面' })).toBeInTheDocument();
    const interactionTrigger = screen.getByRole('button', { name: /^输入体验/ });
    const displayTrigger = screen.getByRole('button', { name: /^候选界面/ });
    // 设置台视口：分组默认全部收起，首屏只读分组索引与待保存状态。
    expect(interactionTrigger).toHaveAttribute('aria-expanded', 'false');
    expect(displayTrigger).toHaveAttribute('aria-expanded', 'false');
    const interactionPanel = document.getElementById(interactionTrigger.getAttribute('aria-controls')!);
    const displayPanel = document.getElementById(displayTrigger.getAttribute('aria-controls')!);
    expect(interactionPanel).toHaveAttribute('inert');
    expect(displayPanel).toHaveAttribute('inert');
    await user.click(interactionTrigger);
    expect(interactionTrigger).toHaveAttribute('aria-expanded', 'true');
    expect(interactionPanel).not.toHaveAttribute('inert');
    expect(screen.getByRole('switch', { name: '上屏后联想' })).toBeDisabled();
    expect(screen.getByLabelText('联想候选停留时间')).toHaveValue(4000);
    await user.click(displayTrigger);
    expect(displayTrigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByLabelText('联想候选数量')).toHaveValue(5);
    await user.click(interactionTrigger);
    expect(interactionTrigger).toHaveAttribute('aria-expanded', 'false');
    expect(interactionPanel).toHaveAttribute('inert');
    expect(document.querySelectorAll(".input-settings-group[data-open='true']")).toHaveLength(1);
    // 台账（状态 + 保存）排在分组索引之前：关键动作不落在折叠内容之下。
    expect(document.querySelector('.input-settings-ledger')?.compareDocumentPosition(document.querySelector('.input-settings-grid')!))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByRole('list', { name: '输入链路状态' }).children).toHaveLength(2);
    expect(screen.getByText('本机模型已加载')).toBeInTheDocument();
    expect(screen.getByText('降级')).toBeInTheDocument();
    expect(screen.getByLabelText('生成框最长等待')).toHaveValue(8000);
    expect(screen.getByLabelText('联想候选数量').closest('.input-setting-editor-row')).not.toBeNull();
    expect(screen.getByText(/需重新载入/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '尚不可预览' })).not.toBeInTheDocument();
  });

  it('keeps the advanced model path in a reversible nested disclosure', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          settings: { models: { modelId: 'local-model', path: '/Models/local.gguf' } },
        },
        'configuration.schema': {
          ok: true,
          sections: [{
            id: 'models',
            fields: [
              { key: 'models.modelId', type: 'string', applyMode: 'reload' },
              { key: 'models.path', type: 'string', applyMode: 'reload' },
            ],
          }],
        },
      },
    }));

    const modelsTrigger = await screen.findByRole('button', { name: /^本机联想/ });
    expect(modelsTrigger).toHaveAttribute('aria-expanded', 'false');
    await user.click(modelsTrigger);
    const advancedTrigger = screen.getByRole('button', { name: '高级：模型文件位置' });
    expect(advancedTrigger).toHaveAttribute('aria-expanded', 'false');
    const advancedPanel = document.getElementById(advancedTrigger.getAttribute('aria-controls')!);
    expect(advancedPanel).toHaveAttribute('inert');
    await user.click(advancedTrigger);
    expect(advancedTrigger).toHaveAttribute('aria-expanded', 'true');
    expect(advancedPanel).not.toHaveAttribute('inert');
    await user.click(advancedTrigger);
    expect(advancedTrigger).toHaveAttribute('aria-expanded', 'false');
    expect(advancedPanel).toHaveAttribute('inert');
    expect(inputMethodCss).toMatch(
      /\.input-settings-disclosure\s*\{[^}]*grid-template-rows:\s*0fr;[^}]*grid-template-rows var\(--motion-disclose\)/s,
    );
    expect(inputMethodCss).toMatch(
      /\.input-settings-disclosure\[data-open='true'\]\s*\{[^}]*grid-template-rows:\s*1fr;/s,
    );
  });

  it('switches runtime mode only after preview and explicit approval, then supports rollback', async () => {
    const user = userEvent.setup();
    const payloadSha256 = 'c'.repeat(64);
    const transport = new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: 'v1-proof' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 9,
          settings: {
            interaction: { postCommit: { enabled: true } },
            memory: { enabled: true },
            activeRag: { allowRemoteModel: true },
            rag: { lanes: { tagMemo: true, timeDailyBook: true } },
          },
        },
        'configuration.schema': { ok: true, sections: [] },
        'configuration.settings.preview': {
          ok: true,
          pathId: 'configuration.settings.apply',
          previewToken: 'preview-safe-mode',
          payloadSha256,
          requiredConfirm: 'apply',
          expiresAtMs: Date.now() + 60_000,
          expectedRevision: { runtimeRevision: 9 },
          summary: { title: '应用控制中心设置', items: ['更新运行模式'], risk: 'R2' },
        },
        'configuration.settings.apply': {
          ok: true,
          pathId: 'configuration.settings.apply',
          receiptId: 'receipt-safe-mode',
          payloadSha256,
          appliedAtMs: Date.now(),
          rollbackAvailable: true,
          rollbackToken: 'rollback-safe-mode',
        },
        'configuration.settings.rollback': {
          ok: true,
          pathId: 'configuration.settings.rollback',
          receiptId: 'receipt-safe-mode-rollback',
          payloadSha256,
          appliedAtMs: Date.now(),
          rollbackAvailable: false,
          rollbackToken: '',
        },
        'input.lexicon.review': emptyReview,
      },
    });
    renderFeature(transport);

    await user.click(await screen.findByRole('radio', { name: '安全' }));
    expect(screen.getByText('改用安全模式将改动 3 项')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '保存使用方式' }));

    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'configuration.settings.preview')).toBe(true));
    const previewRequest = transport.requests.find(({ request }) => request.pathId === 'configuration.settings.preview')?.request;
    expect(previewRequest?.body).toEqual({
      changes: {
        'interaction.postCommit.enabled': false,
        'memory.enabled': false,
        'activeRag.allowRemoteModel': false,
      },
      expectedRuntimeRevision: 9,
    });

    expect(await screen.findByText('已保存')).toBeInTheDocument();
    const applyRequest = transport.requests.find(({ request }) => request.pathId === 'configuration.settings.apply')?.request;
    expect(applyRequest?.body).toEqual({
      changes: {
        'interaction.postCommit.enabled': false,
        'memory.enabled': false,
        'activeRag.allowRemoteModel': false,
      },
      expectedRuntimeRevision: 9,
      previewToken: 'preview-safe-mode',
      payloadSha256,
      confirmText: 'apply',
    });

    await user.click(screen.getByRole('button', { name: '撤销' }));
    expect(await screen.findByText('已恢复到更改前')).toBeInTheDocument();
    const rollbackRequest = transport.requests.find(({ request }) => request.pathId === 'configuration.settings.rollback')?.request;
    expect(rollbackRequest?.body).toEqual({
      receiptId: 'receipt-safe-mode',
      rollbackToken: 'rollback-safe-mode',
      payloadSha256,
      confirmText: 'rollback',
    });
  });

  it('separates 记忆增强 from 标准模式 through the real recall settings and shows a readable delta', async () => {
    const user = userEvent.setup();
    const payloadSha256 = 'd'.repeat(64);
    const transport = new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 12,
          settings: {
            interaction: { postCommit: { enabled: true } },
            memory: { enabled: true, recall: { detailLevel: 'compact', timelineEnabled: false } },
            rag: { lanes: { tagMemo: true, timeDailyBook: true } },
          },
        },
        'configuration.schema': { ok: true, sections: [] },
        'configuration.settings.preview': {
          ok: true,
          pathId: 'configuration.settings.apply',
          previewToken: 'preview-memory-mode',
          payloadSha256,
          requiredConfirm: 'apply',
          expiresAtMs: Date.now() + 60_000,
          expectedRevision: { runtimeRevision: 12 },
          summary: { title: '应用控制中心设置', items: ['更新运行模式'], risk: 'R3' },
        },
        'input.lexicon.review': emptyReview,
      },
    });
    renderFeature(transport);

    // 曾经 记忆增强 与 标准模式 共用同一份变更，从标准设置出发时选它是零变化的死选项。
    await user.click(await screen.findByRole('radio', { name: '记忆增强' }));
    // 保存之前，差异台账已经逐行给出具体变化，而不是只报一个数字。
    expect(screen.getByText('改用记忆增强将改动 2 项')).toBeInTheDocument();
    expect(screen.getByText('召回详细程度：紧凑 → 详尽')).toBeInTheDocument();
    expect(screen.getByText('按需召回时间线：已关闭 → 已启用')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '保存使用方式' }));

    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'configuration.settings.preview')).toBe(true));
    const previewRequest = transport.requests.find(({ request }) => request.pathId === 'configuration.settings.preview')?.request;
    expect(previewRequest?.body).toEqual({
      changes: {
        'memory.recall.detailLevel': 'detailed',
        'memory.recall.timelineEnabled': true,
      },
      expectedRuntimeRevision: 12,
    });

    // R3 影响面板与差异台账说同一句话。
    expect(await screen.findAllByText('召回详细程度：紧凑 → 详尽')).toHaveLength(2);
    expect(screen.getAllByText('按需召回时间线：已关闭 → 已启用')).toHaveLength(2);
    expect(document.body).not.toHaveTextContent('memory.recall.detailLevel');
  });

  it('previews, explicitly approves, applies, and rolls back common input settings through the real settings routes', async () => {
    const user = userEvent.setup();
    const payloadSha256 = 'b'.repeat(64);
    const editableSchema = {
      ok: true,
      sections: [
        {
          id: 'interaction',
          fields: [{
            key: 'interaction.postCommit.optionNumber',
            type: 'enum',
            label: 'Option+数字',
            options: ['select_prediction_by_ordinal', 'disabled'],
            applyMode: 'live',
          }],
        },
        {
          id: 'display',
          fields: [{
            key: 'display.maxPostCommitCandidates',
            type: 'integer',
            label: 'Post-commit 候选数量',
            min: 1,
            max: 8,
            applyMode: 'restart_input_method',
          }],
        },
      ],
    };
    const transport = new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 7,
          settings: {
            interaction: { postCommit: { optionNumber: 'select_prediction_by_ordinal' } },
            display: { maxPostCommitCandidates: 5 },
          },
        },
        'configuration.schema': editableSchema,
        'configuration.settings.preview': {
          ok: true,
          pathId: 'configuration.settings.apply',
          previewToken: 'preview-input-settings',
          payloadSha256,
          requiredConfirm: 'apply',
          expiresAtMs: Date.now() + 60_000,
          expectedRevision: { runtimeRevision: 7 },
          summary: { title: '应用控制中心设置', items: ['更新输入设置'], risk: 'R2' },
        },
        'configuration.settings.apply': {
          ok: true,
          pathId: 'configuration.settings.apply',
          receiptId: 'receipt-input-settings',
          payloadSha256,
          appliedAtMs: Date.now(),
          rollbackAvailable: true,
          rollbackToken: 'rollback-input-settings',
        },
        'configuration.settings.rollback': {
          ok: true,
          pathId: 'configuration.settings.rollback',
          receiptId: 'receipt-input-settings-rollback',
          payloadSha256,
          appliedAtMs: Date.now(),
          rollbackAvailable: false,
          rollbackToken: '',
        },
        'input.lexicon.review': emptyReview,
      },
    });
    renderFeature(transport);

    await user.click(await screen.findByRole('button', { name: /^输入体验/ }));
    await user.click(screen.getByRole('combobox', { name: 'Option+数字行为' }));
    await user.click(await screen.findByRole('option', { name: '关闭' }));
    await user.click(screen.getByRole('button', { name: /^候选界面/ }));
    const candidateCount = screen.getByLabelText('联想候选数量');
    await user.clear(candidateCount);
    await user.type(candidateCount, '6');
    await user.click(screen.getByRole('button', { name: '保存输入体验设置' }));

    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'configuration.settings.preview')).toBe(true));
    const previewRequest = transport.requests.find(({ request }) => request.pathId === 'configuration.settings.preview')?.request;
    expect(previewRequest?.body).toEqual({
      changes: {
        'interaction.postCommit.optionNumber': 'disabled',
        'display.maxPostCommitCandidates': 6,
      },
      expectedRuntimeRevision: 7,
    });

    expect(await screen.findByText('已保存')).toBeInTheDocument();
    const applyRequest = transport.requests.find(({ request }) => request.pathId === 'configuration.settings.apply')?.request;
    expect(applyRequest?.body).toEqual({
      changes: {
        'interaction.postCommit.optionNumber': 'disabled',
        'display.maxPostCommitCandidates': 6,
      },
      expectedRuntimeRevision: 7,
      previewToken: 'preview-input-settings',
      payloadSha256,
      confirmText: 'apply',
    });

    await user.click(screen.getByRole('button', { name: '撤销' }));
    expect(await screen.findByText('已恢复到更改前')).toBeInTheDocument();
    const rollbackRequest = transport.requests.find(({ request }) => request.pathId === 'configuration.settings.rollback')?.request;
    expect(rollbackRequest?.body).toEqual({
      receiptId: 'receipt-input-settings',
      rollbackToken: 'rollback-input-settings',
      payloadSha256,
      confirmText: 'rollback',
    });
  });

  it('renders model and interaction controls and previews only their validated changes', async () => {
    const user = userEvent.setup();
    const payloadSha256 = 'c'.repeat(64);
    const transport = new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'diagnostics.models': {
          ok: true,
          schemaVersion: 'rag-ime.models-status.v4',
          configurationPending: false,
          activeConfig: {
            modelId: 'minimind-ime-v2',
            profileId: 'minimind_ime_v2',
            promptMode: 'base-completion',
            maxTokens: 8,
          },
          availableModels: [{
            id: 'minimind-ime-v2',
            path: '/tmp/minimind-ime-v2',
            profileId: 'minimind_ime_v2',
            promptMode: 'base-completion',
            maxTokens: 8,
            temperature: 0.15,
            topP: 0.85,
            active: true,
          }],
          healthAgreement: { ok: true },
        },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 9,
          settings: {
            interaction: { postCommit: { modelBudgetMs: 900, optionNumber: 'select_prediction_by_ordinal' } },
            models: {
              modelId: '',
              hot: 'minimind_ime_v2',
              path: '',
              promptMode: 'base-completion',
              maxTokens: 8,
              temperature: 0.15,
              topP: 0.85,
            },
          },
        },
        'configuration.schema': {
          ok: true,
          sections: [
            {
              id: 'interaction',
              fields: [
                { key: 'interaction.postCommit.modelBudgetMs', type: 'integer', min: 300, max: 12000, applyMode: 'live' },
                { key: 'interaction.postCommit.optionNumber', type: 'enum', options: ['select_prediction_by_ordinal', 'disabled'], applyMode: 'live' },
              ],
            },
            {
              id: 'models',
              fields: [
                { key: 'models.modelId', type: 'string', maxLength: 128, applyMode: 'restart_predictor' },
                { key: 'models.hot', type: 'enum', options: ['minimind_ime_v2', 'qwen3_06b_ime_hot'], applyMode: 'restart_predictor' },
                { key: 'models.path', type: 'string', maxLength: 1024, applyMode: 'restart_predictor' },
                { key: 'models.promptMode', type: 'enum', options: ['base-completion', 'chat-json'], applyMode: 'restart_predictor' },
                { key: 'models.maxTokens', type: 'integer', min: 1, max: 64, applyMode: 'restart_predictor' },
                { key: 'models.temperature', type: 'number', min: 0, max: 2, step: 0.05, applyMode: 'restart_predictor' },
                { key: 'models.topP', type: 'number', min: 0.05, max: 1, step: 0.05, applyMode: 'restart_predictor' },
              ],
            },
          ],
        },
        'configuration.settings.preview': {
          ok: true,
          pathId: 'configuration.settings.apply',
          previewToken: 'preview-model-settings',
          payloadSha256,
          requiredConfirm: 'apply',
          expiresAtMs: Date.now() + 60_000,
          expectedRevision: { runtimeRevision: 9 },
          summary: { title: '应用控制中心设置', items: ['更新本机模型设置'], risk: 'R2' },
        },
      },
    });
    renderFeature(transport);

    expect(await screen.findByText('配置已生效')).toBeInTheDocument();
    expect(screen.getAllByText('minimind-ime-v2').length).toBeGreaterThan(0);
    expect(screen.queryByLabelText('普通数字键')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^本机联想/ }));
    await user.click(screen.getByRole('combobox', { name: '本机联想模型' }));
    await user.click(screen.getByRole('option', { name: 'minimind-ime-v2' }));
    expect(screen.getByLabelText('本机模型目录')).toHaveValue('/tmp/minimind-ime-v2');
    const maxTokens = screen.getByLabelText('单次联想长度');
    await user.clear(maxTokens);
    await user.type(maxTokens, '12');
    await user.click(screen.getByRole('button', { name: /^输入体验/ }));
    const modelBudget = screen.getByLabelText('本机模型最长等待');
    await user.clear(modelBudget);
    await user.type(modelBudget, '1200');
    await user.click(screen.getByRole('button', { name: '保存输入体验设置' }));

    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'configuration.settings.preview')).toBe(true));
    const previewRequest = transport.requests.find(({ request }) => request.pathId === 'configuration.settings.preview')?.request;
    expect(previewRequest?.body).toEqual({
      changes: {
        'interaction.postCommit.modelBudgetMs': 1200,
        'models.modelId': 'minimind-ime-v2',
        'models.path': '/tmp/minimind-ime-v2',
        'models.maxTokens': 12,
      },
      expectedRuntimeRevision: 9,
    });
  });

  it('keeps pending status visible while allowing a changed group to collapse and reopen', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 12,
          settings: { display: { maxPostCommitCandidates: 5 } },
        },
        'configuration.schema': {
          ok: true,
          sections: [{
            id: 'display',
            fields: [{
              key: 'display.maxPostCommitCandidates',
              type: 'integer',
              min: 1,
              max: 8,
              applyMode: 'reload',
            }],
          }],
        },
        'input.lexicon.review': emptyReview,
      },
    }));

    const group = (await screen.findByRole('heading', { level: 3, name: '候选界面' })).closest('.input-settings-group')!;
    const trigger = screen.getByRole('button', { name: /^候选界面/ });
    const panel = document.getElementById(trigger.getAttribute('aria-controls')!)!;
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await user.click(trigger);
    const candidateCount = screen.getByLabelText('联想候选数量');
    await user.clear(candidateCount);
    await user.type(candidateCount, '12');

    expect(group).toHaveAttribute('data-open', 'true');
    expect(trigger).toHaveTextContent('1 项需修正');
    expect(screen.getByRole('alert')).toHaveTextContent('请输入 1 到 8 之间的数值。');
    expect(candidateCount).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('需要修正')).toBeInTheDocument();

    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(group).toHaveAttribute('data-open', 'false');
    expect(panel).toHaveAttribute('inert');
    expect(panel).toHaveTextContent('请输入 1 到 8 之间的数值。');

    await user.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('alert')).toBeVisible();

    await user.clear(candidateCount);
    await user.type(candidateCount, '6');
    expect(trigger).toHaveTextContent('1 项待保存');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await user.click(trigger);
    expect(group).toHaveAttribute('data-open', 'false');
    expect(panel).toHaveAttribute('inert');
  });

  it('does not enable input setting writes when any settings work-contract route is missing', async () => {
    renderFeature(new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
        routeIds: [
          'input.source.get',
          'overview.get',
          'configuration.settings',
          'configuration.schema',
          'configuration.settings.preview',
          'configuration.settings.apply',
        ],
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 4,
          settings: { interaction: { postCommit: { optionNumber: 'select_prediction_by_ordinal' } } },
        },
        'configuration.schema': {
          ok: true,
          sections: [{
            id: 'interaction',
            fields: [{
              key: 'interaction.postCommit.optionNumber',
              type: 'enum',
              options: ['select_prediction_by_ordinal', 'disabled'],
            }],
          }],
        },
      },
    }));

    expect(await screen.findByLabelText('Option+数字行为')).toBeDisabled();
    expect(screen.getByRole('radio', { name: '安全' })).toBeDisabled();
    expect(screen.getAllByText(/当前应用不支持安全保存这项设置/)).toHaveLength(2);
    expect(screen.queryByRole('button', { name: '尚不可预览' })).not.toBeInTheDocument();
  });

  it('keeps settings visible when the input source query fails', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': () => { throw new Error('source probe unavailable'); },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByText('读取失败')).toBeInTheDocument();
    expect(screen.getByRole('listitem', { name: '上屏后联想' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^输入体验/ }));
    expect(screen.getByRole('switch', { name: '上屏后联想' })).toBeChecked();
  });

  it('keeps an expanded settings field and its draft through a failed refresh and recovery', async () => {
    const user = userEvent.setup();
    let failed = false;
    const transport = new MockControlTransport({
      capabilities: { features: { managementWorkContract: true, configurationSettingsWorkContract: true } },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': () => {
          if (failed) throw new Error('settings unavailable');
          return { ok: true, runtimeRevision: 9, settings: { activeRag: { latencyBudgetMs: 8000 } } };
        },
        'configuration.schema': { ok: true, sections: [{ id: 'activeRag', fields: [
          { key: 'activeRag.latencyBudgetMs', type: 'number', min: 1, max: 30000, applyMode: 'live' },
        ] }] },
      },
    });
    renderFeature(transport);
    await user.click(await screen.findByRole('button', { name: /^知识建议/ }));
    const input = screen.getByRole('spinbutton', { name: '生成框最长等待' });
    await user.clear(input);
    await user.type(input, '9100');
    failed = true;
    await user.click(screen.getByRole('button', { name: '刷新' }));
    await screen.findByText('无法读取当前设置，请刷新后重试。');
    expect(screen.getByRole('spinbutton', { name: '生成框最长等待' })).toBe(input);
    expect(input).toHaveValue(9100);
    expect(screen.queryByRole('button', { name: '保存输入体验设置' })).not.toBeInTheDocument();
    failed = false;
    await user.click(screen.getByRole('button', { name: '刷新' }));
    await screen.findByRole('button', { name: '保存输入体验设置' });
    expect(screen.getByRole('spinbutton', { name: '生成框最长等待' })).toBe(input);
    expect(input).toHaveValue(9100);
  });

  it('refreshes the input page queries without pulling the separate lexicon page', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': emptyReview,
      },
    });
    renderFeature(transport);

    await screen.findByRole('listitem', { name: '上屏后联想' });
    const before = new Map(
      ['input.source.get', 'overview.get', 'configuration.settings', 'configuration.schema']
        .map((pathId) => [pathId, transport.requests.filter((call) => call.request.pathId === pathId).length]),
    );
    await user.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => {
      for (const [pathId, count] of before) {
        expect(transport.requests.filter((call) => call.request.pathId === pathId).length).toBeGreaterThan(count);
      }
    });
    expect(transport.requests.some((call) => call.request.pathId === 'input.lexicon.review')).toBe(false);
  });

  it('offers a focused retry when the local model status cannot be read', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': emptyReview,
      },
    });
    renderFeature(transport);

    expect(await screen.findByText('模型状态暂不可用')).toBeInTheDocument();
    expect(screen.getByText(/已保存的选择保持不变/)).toBeInTheDocument();
    const before = transport.requests.filter(({ request }) => request.pathId === 'diagnostics.models').length;
    await user.click(screen.getByRole('button', { name: '重试模型检查' }));
    await waitFor(() => expect(
      transport.requests.filter(({ request }) => request.pathId === 'diagnostics.models').length,
    ).toBeGreaterThan(before));
  });

  it('fails closed when the host does not expose all lexicon pathIds', async () => {
    renderLexiconFeature(new MockControlTransport({
      capabilities: {
        routeIds: ['input.source.get', 'overview.get', 'configuration.settings', 'configuration.schema'],
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
      },
    }));

    expect(await screen.findByText(/当前版本缺少完整的审阅、写入与撤销能力/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '查看已选词条' })).not.toBeInTheDocument();
    expect(screen.queryByText('演练 / 未执行')).not.toBeInTheDocument();
  });

  it('reports a capability read failure instead of presenting it as an empty lexicon', async () => {
    const transport = new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
      },
    });
    vi.spyOn(transport, 'capabilities').mockRejectedValue(new Error('能力接口不可用'));
    renderLexiconFeature(transport);

    expect(await screen.findByText('无法确认词库能力')).toBeInTheDocument();
    expect(screen.getByText('能力接口不可用')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '查看已选词条' })).not.toBeInTheDocument();
  });

  it('projects the persisted cadence and a real failure receipt without changing Rime ownership', async () => {
    renderLexiconFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': {
          ...emptyReview,
          organization: {
            ...emptyReview.organization,
            lastRun: {
              ...emptyReview.organization.lastRun,
              status: 'failed',
              candidateCount: 0,
              errorCode: 'sqlite_error',
              error: '本机数据库暂时不可写。',
            },
          },
        },
      },
    }));

    expect(await screen.findByText('已启用 · 每天约 2 次')).toBeInTheDocument();
    expect(screen.getByText('运行失败')).toBeInTheDocument();
    expect(screen.getByText('上次定期整理失败')).toBeInTheDocument();
    expect(screen.getByText(/本机数据库暂时不可写。/)).toBeInTheDocument();
    const failureNotice = screen.getByText('上次定期整理失败').closest('.mgmt-notice');
    expect(failureNotice?.textContent?.match(/本机数据库暂时不可写。/gu)).toHaveLength(1);
  });

  it('uses the live review token, renders an unapplied redeploy state, and rolls back by receipt', async () => {
    const user = userEvent.setup();
    const apply = vi.fn(async () => ({
      schemaVersion: 'rag-ime.rime-lexicon-review.v1',
      ok: true,
      applied: true,
      entryCount: 1,
      rollbackId: 'rollback-lexicon-1',
      requiresRedeploy: true,
    }));
    const rollback = vi.fn(async () => ({
      schemaVersion: 'rag-ime.rime-lexicon-review.v1',
      ok: true,
      rolledBack: true,
      rollbackId: 'rollback-lexicon-1',
      requiresRedeploy: true,
    }));
    const transport = new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': review,
        'input.lexicon.apply': apply,
        'input.lexicon.rollback': rollback,
      },
    });
    renderLexiconFeature(transport);

    expect(await screen.findByRole('checkbox', { name: '选择 表情包' })).toBeChecked();
    expect(screen.getByText(/待审 1 条 · 已选 1 条/)).toBeInTheDocument();
    expect(screen.getByText(/保存后仍需重载输入法/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '加入所选词条' }));

    expect(await screen.findByText('已加入 · 等待重载')).toBeInTheDocument();
    expect(screen.getByText('词条已加入用户词库')).toBeInTheDocument();
    expect(screen.getByText(/重载后用实际输入与选词结果确认效果/)).toBeInTheDocument();
    const applyRequest = transport.requests.find((call) => call.request.pathId === 'input.lexicon.apply')?.request;
    expect(applyRequest?.body).toEqual({
      reviewToken: 'a'.repeat(64),
      selectedKeys: ['表情包\tbiao qing bao'],
      confirmText: 'APPLY_REVIEWED_RIME_LEXICON',
      project: 'wisdom-weasel-rag-ime',
      limit: 200,
    });

    await user.click(screen.getByRole('button', { name: '撤销这次更新' }));
    expect(await screen.findByText('已撤销 · 等待重载')).toBeInTheDocument();
    const rollbackRequest = transport.requests.find((call) => call.request.pathId === 'input.lexicon.rollback')?.request;
    expect(rollbackRequest?.body).toEqual({ rollbackId: 'rollback-lexicon-1' });
  });

  it('surfaces the backend risk verdict per entry and reviews in bulk with 全选/清除', async () => {
    const user = userEvent.setup();
    renderLexiconFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': {
          ...emptyReview,
          entryCount: 2,
          selectionPolicy: '只默认选择有至少 3 次真实反馈的常用词；模型整理词必须人工勾选',
          entries: [
            { ...review.entries[0], defaultSelected: true, riskLabel: '真实选词反馈' },
            {
              reviewKey: '知识蒸馏\tzhi shi zheng liu',
              text: '知识蒸馏',
              pinyin: 'zhi shi zheng liu',
              weight: 60,
              positiveCount: 0,
              negativeCount: 0,
              reasons: ['dsv4_offline_review'],
              reviewSource: 'dsv4',
              reviewReason: 'DSV4 根据历史证据整理',
              selected: false,
              defaultSelected: false,
              riskLabel: '模型建议，需人工确认',
            },
          ],
        },
      },
    }));

    // 真实的风险归类与后端策略原文都要到达用户，而不是统一的"待你判断"。
    expect(await screen.findByText('待审 2 条 · 已选 1 条')).toBeInTheDocument();
    expect(screen.getByText(/模型建议，需人工确认/)).toBeInTheDocument();
    expect(screen.getByText(/真实选词反馈/)).toBeInTheDocument();
    expect(screen.getByText('模型整理')).toBeInTheDocument();
    expect(screen.getByText(/只默认选择有至少 3 次真实反馈的常用词/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: '选择 知识蒸馏' })).not.toBeChecked();

    await user.click(screen.getByRole('button', { name: '全选' }));
    expect(screen.getByText('待审 2 条 · 已选 2 条')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '全选' })).toBeDisabled();

    await user.click(screen.getByRole('button', { name: '清除' }));
    expect(screen.getByText('待审 2 条 · 已选 0 条')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '清除' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '加入所选词条' })).toBeDisabled();
  });

  it('drops the reload claim from the receipt when the backend says no redeploy is required', async () => {
    const user = userEvent.setup();
    renderLexiconFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': review,
        'input.lexicon.apply': {
          schemaVersion: 'rag-ime.rime-lexicon-review.v1',
          ok: true,
          applied: true,
          entryCount: 1,
          rollbackId: 'rollback-lexicon-2',
          requiresRedeploy: false,
        },
        'input.lexicon.rollback': {},
      },
    }));

    await screen.findByRole('checkbox', { name: '选择 表情包' });
    await user.click(screen.getByRole('button', { name: '加入所选词条' }));

    expect(await screen.findByText('词条已加入用户词库')).toBeInTheDocument();
    expect(screen.getByText('已加入')).toBeInTheDocument();
    expect(screen.queryByText('已加入 · 等待重载')).not.toBeInTheDocument();
    expect(screen.queryByText('尚未生效')).not.toBeInTheDocument();
    // 不宣称前台已生效：仍要求用实际输入确认。
    expect(screen.getByText('等待实测确认')).toBeInTheDocument();
  });

  it('presents the generation stage as a native-suggestion schematic plus three real-setting lanes', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': {
          ok: true,
          profile: '记忆增强',
          components: {
            sidecar: { ok: true, status: 'ready', detail: '后台服务已连接' },
            predictor: { ok: true, status: 'ready', detail: '本机模型已载入' },
            foregroundContext: { ok: true, status: 'ready', detail: '前台上下文按授权读取' },
          },
        },
        'diagnostics.models': {
          ok: true,
          schemaVersion: 'rag-ime.models-status.v4',
          configurationPending: false,
          activeConfig: { modelId: 'minimind-ime-v2', profileId: 'minimind_ime_v2', promptMode: 'base-completion', maxTokens: 8 },
          availableModels: [],
          healthAgreement: { ok: true },
        },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 3,
          settings: {
            interaction: {
              postCommit: {
                enabled: true,
                idleTriggerMs: 220,
                panelTtlMs: 4000,
                tabAction: 'accept_top_prediction',
                optionNumber: 'select_prediction_by_ordinal',
              },
            },
            display: { maxPostCommitCandidates: 5, panelStyle: 'compact' },
            memory: { enabled: true, recall: { detailLevel: 'detailed', timelineEnabled: true } },
            rag: { lanes: { tagMemo: true, timeDailyBook: true } },
            context: { recentInputBaseline: 20, temporalRecall: true },
          },
        },
        'configuration.schema': { ok: true, sections: [] },
        'input.lexicon.review': emptyReview,
      },
    }));

    // 管线默认收起成一行真实状态；示意与车道细节按需展开。
    const pipelineToggle = await screen.findByRole('button', { name: '生成步骤详情' });
    expect(pipelineToggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('上下文获取 · 就绪')).toBeInTheDocument();
    expect(screen.getByText('记忆召回 · 已启用')).not.toHaveAttribute('data-off');
    expect(screen.getByText('本机联想 · 就绪')).toBeInTheDocument();
    await user.click(pipelineToggle);
    expect(pipelineToggle).toHaveAttribute('aria-expanded', 'true');

    // 面板示意只按真实设置绘制：候选数量、紧凑样式、采纳方式与停留时长。
    const figure = await screen.findByRole('figure', { name: '上屏后智能候选面板示意' });
    expect(figure).toHaveAttribute('data-enabled', 'true');
    expect(figure).toHaveAttribute('data-panel', 'compact');
    expect(figure.querySelectorAll('.input-suggest-preview__slot')).toHaveLength(5);
    expect(figure).toHaveTextContent('与原生候选并排出现、样式可见区分');
    const hints = within(figure).getByRole('list', { name: '采纳方式' });
    expect(within(hints).getByText('Tab 采纳第 1 条')).toBeInTheDocument();
    expect(within(hints).getByText('Option+数字 选对应候选')).toBeInTheDocument();
    expect(within(hints).getByText('约 4 秒后自动收起')).toBeInTheDocument();

    // 三条车道按管线顺序展示：上下文获取 → 记忆召回 → 本机联想。
    const steps = screen.getByRole('list', { name: '智能候选的生成步骤' });
    const lanes = Array.from(steps.children);
    expect(lanes).toHaveLength(3);
    const [contextLane, recallLane, completionLane] = lanes;
    expect(contextLane).toHaveTextContent('上下文获取');
    expect(contextLane).toHaveTextContent('最近 20 段输入作基线');
    expect(contextLane).toHaveTextContent('前台上下文按授权读取');
    expect(recallLane).toHaveTextContent('记忆召回');
    expect(recallLane).toHaveTextContent('标签记忆');
    expect(recallLane).toHaveTextContent('时间与日记');
    expect(recallLane).toHaveTextContent('详尽召回');
    expect(recallLane).toHaveTextContent('按需展开时间线');
    expect(completionLane).toHaveTextContent('本机联想');
    expect(completionLane).toHaveTextContent('minimind-ime-v2');
    expect(completionLane).toHaveTextContent('每次最多 5 条');
    expect(completionLane).toHaveTextContent('停顿 0.2 秒后生成');
    expect(completionLane).toHaveTextContent('配置已生效');
    expect(completionLane).toHaveTextContent('本机模型已载入');

    // 展示层不得泄露实现字段或配置键名。
    expect(document.body).not.toHaveTextContent('postCommit');
    expect(document.body).not.toHaveTextContent('panelStyle');
    expect(document.body).not.toHaveTextContent('maxPostCommitCandidates');
    expect(document.body).not.toHaveTextContent('base-completion');
    // 模型配置健康时不出现多余的应用控件。
    expect(screen.queryByText('应用联想模型')).not.toBeInTheDocument();
  });

  it('separates saved prediction choices from effective runtime clamps and exposes the live MLX model', async () => {
    const transport = new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': {
          ok: true,
          profile: '安全模式',
          runtimeConfig: {
            composition: { aiEnabled: false, showOnlyRime: true },
            postCommit: { enabled: true },
            safetyClamps: ['composition_ai_disabled_by_profile', 'composition_rime_only'],
          },
        },
        'diagnostics.models': {
          ok: true,
          schemaVersion: 'rag-ime.models-status.v4',
          configurationPending: false,
          predictor: {
            providerName: 'local-mlx',
            capabilityProbe: { ok: true, modelLoaded: true },
          },
          activeConfig: {
            modelId: 'minimind-ime-100m-user-daily-core-v1',
            profileId: 'minimind_ime_v2',
          },
          availableModels: [],
          healthAgreement: { ok: true, mlxMatchesRegistry: true },
        },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 14,
          settings: {
            interaction: {
              composition: { showPrediction: true, showOnlyRime: false },
              postCommit: { enabled: true, showPendingStatus: true },
            },
          },
        },
        'configuration.schema': {
          ok: true,
          sections: [{
            id: 'interaction',
            fields: [
              { key: 'interaction.composition.showPrediction', type: 'boolean', applyMode: 'reload_input_method' },
              { key: 'interaction.composition.showOnlyRime', type: 'boolean', applyMode: 'reload_input_method' },
              { key: 'interaction.postCommit.showPendingStatus', type: 'boolean', applyMode: 'live' },
              { key: 'interaction.postCommit.enabled', type: 'boolean', applyMode: 'live' },
            ],
          }],
        },
      },
    });
    renderFeature(transport);

    const facts = await screen.findByRole('list', { name: '输入预测运行事实' });
    const composition = within(facts).getByRole('listitem', { name: '输入拼音时预测' });
    expect(composition).toHaveTextContent('已保存开启');
    expect(composition).toHaveTextContent('运行未生效');
    expect(composition).toHaveTextContent('当前运行模式关闭了 AI 拼音候选');
    expect(composition).toHaveTextContent('Rime 独占拼音候选');
    const postCommit = within(facts).getByRole('listitem', { name: '上屏后联想' });
    expect(postCommit).toHaveTextContent('已保存开启');
    expect(postCommit).toHaveTextContent('运行已生效');
    const mlx = within(facts).getByRole('listitem', { name: 'MLX 本机模型' });
    expect(mlx).toHaveTextContent('local-mlx');
    expect(mlx).toHaveTextContent('MiniMind IME 100M User Daily Core v1');
    expect(mlx).toHaveTextContent('模型已加载');
    expect(mlx).toHaveTextContent('注册配置一致');
    expect(facts).toHaveTextContent('Rime 负责拼音解码与原生候选');
    expect(document.body).not.toHaveTextContent('/Models/');

    await userEvent.click(screen.getByRole('button', { name: /^输入体验/ }));
    expect(screen.getByRole('switch', { name: '输入拼音时预测' })).toBeChecked();
    expect(screen.getByRole('switch', { name: '输入拼音时只显示 Rime 候选' })).not.toBeChecked();
    expect(screen.getByRole('switch', { name: '预测开始时显示状态' })).toBeChecked();

    await waitFor(() => {
      for (const pathId of [
        'input.source.get',
        'overview.get',
        'diagnostics.models',
        'configuration.settings',
        'configuration.schema',
      ]) {
        expect(transport.requests.filter(({ request }) => request.pathId === pathId)).toHaveLength(1);
      }
    });
  });

  it('keeps prediction facts and setting switches readable across desktop and narrow app widths', () => {
    expect(inputMethodCss).toMatch(
      /\.input-prediction-facts\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/s,
    );
    expect(inputMethodCss).toMatch(
      /@media \(max-width: 980px\)[\s\S]*?\.input-prediction-facts\s*\{[^}]*repeat\(2, minmax\(0, 1fr\)\)/s,
    );
    expect(inputMethodCss).toMatch(
      /@media \(max-width: 620px\)[\s\S]*?\.input-prediction-facts\s*\{[^}]*minmax\(0, 1fr\)/s,
    );
    expect(inputMethodCss).toMatch(
      /@container paw-window \(max-width: 900px\)[\s\S]*?\.input-prediction-facts\s*\{[^}]*repeat\(2, minmax\(0, 1fr\)\)/s,
    );
    expect(inputMethodCss).toMatch(
      /@container paw-window \(max-width: 560px\)[\s\S]*?\.input-prediction-facts\s*\{[^}]*minmax\(0, 1fr\)/s,
    );
    expect(inputMethodCss).toMatch(/\.input-prediction-fact\s*\{[^}]*min-width:\s*0/s);
    expect(inputMethodCss).toMatch(/\.input-prediction-fact__copy\s*\{[^}]*overflow-wrap:\s*anywhere/s);
    expect(inputMethodCss).toMatch(/\.input-setting-editor-row \.ui-switch-field__copy\s*\{[^}]*min-width:\s*0/s);
    expect(inputMethodCss).toMatch(/\.input-setting-editor-row \.ui-switch\s*\{[^}]*flex-shrink:\s*0/s);
  });

  it('keeps the generation stage honest when completion and recall are switched off', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '安全模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 6,
          settings: {
            interaction: { postCommit: { enabled: false, panelTtlMs: 4000 } },
            memory: { enabled: false },
            activeRag: { allowRemoteModel: false },
          },
        },
        'configuration.schema': { ok: true, sections: [] },
        'input.lexicon.review': emptyReview,
      },
    }));

    // 收起态的一行概览同样诚实：关闭的车道不冒充在跑。
    expect(await screen.findByText('记忆召回 · 已关闭')).toHaveAttribute('data-off');
    expect(screen.getByText('本机联想 · 已关闭')).toHaveAttribute('data-off');
    await user.click(screen.getByRole('button', { name: '生成步骤详情' }));

    const figure = await screen.findByRole('figure', { name: '上屏后智能候选面板示意' });
    expect(figure).toHaveAttribute('data-enabled', 'false');
    expect(figure).toHaveTextContent('已关闭：上屏后不出现智能候选，输入完全交回系统输入法。');
    expect(figure.querySelectorAll('.input-suggest-preview__slot')).toHaveLength(0);
    expect(within(figure).queryByRole('list', { name: '采纳方式' })).not.toBeInTheDocument();

    const steps = screen.getByRole('list', { name: '智能候选的生成步骤' });
    const lanes = Array.from(steps.children);
    expect(lanes).toHaveLength(3);
    expect(lanes[1]).toHaveTextContent('不查找个人记忆，候选只依据眼前的上下文。');
    expect(lanes[1]).toHaveAttribute('data-dimmed');
    expect(lanes[2]).toHaveTextContent('上屏后不再生成智能候选，输入完全交回系统输入法。');
    expect(lanes[2]).toHaveAttribute('data-dimmed');
    // 联想关闭时不出现模型应用控件，也不展示健康徽章。
    expect(screen.queryByText('配置已生效')).not.toBeInTheDocument();
    expect(screen.queryByText('应用联想模型')).not.toBeInTheDocument();
  });

  it('presents the four input modes as fact-signed cards instead of a segmented strip', async () => {
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
      },
    }));

    expect(await screen.findByRole('radiogroup', { name: '使用方式' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: '记忆增强' })).toBeInTheDocument();
    expect(screen.getByText('召回更详尽、可展开时间线，其余与标准一致。')).toBeInTheDocument();
    expect(document.querySelectorAll('.input-mode-deck .input-mode-card')).toHaveLength(4);
    expect(document.querySelector('.input-mode-console .ui-segmented')).toBeNull();

    // 每张卡片带真实写入的事实签名；标准与记忆增强的分界键在两侧都被强调。
    const memoryCard = screen.getByRole('radio', { name: '记忆增强' });
    expect(within(memoryCard).getByText('详尽召回')).toHaveAttribute('data-highlight');
    expect(within(memoryCard).getByText('按需时间线')).toHaveAttribute('data-highlight');
    const standardCard = screen.getByRole('radio', { name: '标准' });
    expect(within(standardCard).getByText('紧凑召回')).toHaveAttribute('data-highlight');
    expect(within(standardCard).getByText('本机联想')).not.toHaveAttribute('data-highlight');
    const safeCard = screen.getByRole('radio', { name: '安全' });
    expect(within(safeCard).getByText('联想关闭')).toHaveAttribute('data-off');
    expect(within(safeCard).getByText('远程生成关闭')).toHaveAttribute('data-off');
  });

  it('keeps the mode decision console first and the detail ledgers behind it', async () => {
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByRole('radiogroup', { name: '使用方式' })).toBeInTheDocument();
    await screen.findByRole('list', { name: '输入链路状态' });

    // 主决策（模式选择 + 保存）是第一段内容，不需要往下翻。
    const body = document.querySelector('.mgmt-page__body')!;
    expect(body.firstElementChild!.querySelector('.input-mode-deck')).not.toBeNull();
    expect(body.firstElementChild!.textContent).toContain('保存使用方式');
    const modeConsole = document.querySelector('.input-mode-console')!;
    for (const selector of ['.input-pipeline', '.input-status-grid', '.input-settings-ledger']) {
      const later = document.querySelector(selector);
      expect(later).not.toBeNull();
      expect(modeConsole.compareDocumentPosition(later!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    }

    // 紧凑模式选择：宽窗横向四联，窗口收窄退到 2×2，不再是巨卡叠墙。
    expect(inputMethodCss).toMatch(/\.input-mode-deck\s*\{[^}]*grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\)/s);
    expect(inputMethodCss).toMatch(/@media \(max-width: 980px\)[\s\S]*?\.input-mode-deck\s*\{\s*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/);
    expect(inputMethodCss).toMatch(/@container paw-window \(max-width: 900px\)[\s\S]*?\.input-mode-deck\s*\{[^}]*repeat\(2, minmax\(0, 1fr\)\)/);
    // OS 控件语言：沿用 paw-os-controls 的尺度令牌；不透明表面，无渐变泡泡卡。
    expect(inputMethodCss).toMatch(/--paw-control-h/);
    expect(inputMethodCss).toMatch(/--paw-control-radius/);
    expect(inputMethodCss).toMatch(/--paw-panel-radius/);
    expect(inputMethodCss).not.toMatch(/linear-gradient/);
  });

  it('folds the mode diff ledger on demand while the count and save stay in view', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 5,
          settings: {
            interaction: { postCommit: { enabled: true } },
            memory: { enabled: true, recall: { detailLevel: 'compact', timelineEnabled: false } },
            rag: { lanes: { tagMemo: true, timeDailyBook: true } },
          },
        },
        'configuration.schema': { ok: true, sections: [] },
        'input.lexicon.review': emptyReview,
      },
    }));

    await user.click(await screen.findByRole('radio', { name: '记忆增强' }));
    const toggle = screen.getByRole('button', { name: '差异清单' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const panel = document.getElementById(toggle.getAttribute('aria-controls')!)!;
    expect(panel).not.toHaveAttribute('inert');
    expect(screen.getByText('召回详细程度：紧凑 → 详尽')).toBeInTheDocument();

    await user.click(toggle);

    // 收起后逐行清单折叠，但改动数量与保存动作仍然同屏在场。
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(panel).toHaveAttribute('inert');
    expect(screen.getByText('改用记忆增强将改动 2 项')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存使用方式' })).toBeInTheDocument();
  });

  it('keeps the settings save action fixed while the diff table folds', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 12,
          settings: { display: { maxPostCommitCandidates: 5 } },
        },
        'configuration.schema': {
          ok: true,
          sections: [{
            id: 'display',
            fields: [{
              key: 'display.maxPostCommitCandidates',
              type: 'integer',
              min: 1,
              max: 8,
              applyMode: 'reload',
            }],
          }],
        },
        'input.lexicon.review': emptyReview,
      },
    }));

    await user.click(await screen.findByRole('button', { name: /^候选界面/ }));
    const candidateCount = screen.getByLabelText('联想候选数量');
    await user.clear(candidateCount);
    await user.type(candidateCount, '6');

    const ledger = document.body.querySelector<HTMLElement>('.input-settings-ledger');
    expect(ledger).not.toBeNull();
    expect(within(ledger!).getByText('1 项待保存')).toBeInTheDocument();
    const toggle = within(ledger!).getByRole('button', { name: '差异清单' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const panel = document.getElementById(toggle.getAttribute('aria-controls')!)!;
    expect(panel).not.toHaveAttribute('inert');

    await user.click(toggle);

    // 差异表折叠后，待保存计数与保存动作仍然直接可见。
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(panel).toHaveAttribute('inert');
    expect(within(ledger!).getByText('1 项待保存')).toBeInTheDocument();
    expect(within(ledger!).getByRole('button', { name: '保存输入体验设置' })).toBeInTheDocument();
  });

  it('keeps the 当前 marker on the active mode while another card is only a draft', async () => {
    const user = userEvent.setup();
    renderFeature(new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 5,
          settings: {
            interaction: { postCommit: { enabled: true } },
            memory: { enabled: true, recall: { detailLevel: 'compact', timelineEnabled: false } },
            rag: { lanes: { tagMemo: true, timeDailyBook: true } },
          },
        },
        'configuration.schema': { ok: true, sections: [] },
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByText('当前设置与「标准模式」一致。')).toBeInTheDocument();
    const standardCard = screen.getByRole('radio', { name: '标准' });
    expect(standardCard).toHaveAttribute('data-current');
    expect(standardCard).toHaveAttribute('data-state', 'checked');

    await user.click(screen.getByRole('radio', { name: '记忆增强' }));

    // 草稿勾选移动，但“当前”标记留在真实生效的模式上。
    const memoryCard = screen.getByRole('radio', { name: '记忆增强' });
    expect(memoryCard).toHaveAttribute('data-state', 'checked');
    expect(memoryCard).not.toHaveAttribute('data-current');
    expect(standardCard).toHaveAttribute('data-current');
    expect(standardCard).toHaveAttribute('data-state', 'unchecked');
    expect(within(standardCard).getByText('当前')).toBeInTheDocument();
  });

  it('returns the checked card to the applied mode after a successful save', async () => {
    const user = userEvent.setup();
    const payloadSha256 = 'e'.repeat(64);
    let persistedRecall: Record<string, unknown> = { detailLevel: 'compact', timelineEnabled: false };
    const transport = new MockControlTransport({
      capabilities: {
        features: {
          managementWorkContract: true,
          configurationSettingsWorkContract: true,
        },
      },
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': () => ({
          ok: true,
          runtimeRevision: 8,
          settings: {
            interaction: { postCommit: { enabled: true } },
            memory: { enabled: true, recall: { ...persistedRecall } },
            rag: { lanes: { tagMemo: true, timeDailyBook: true } },
          },
        }),
        'configuration.schema': { ok: true, sections: [] },
        'configuration.settings.preview': {
          ok: true,
          pathId: 'configuration.settings.apply',
          previewToken: 'preview-memory-apply',
          payloadSha256,
          requiredConfirm: 'apply',
          expiresAtMs: Date.now() + 60_000,
          expectedRevision: { runtimeRevision: 8 },
          summary: { title: '应用控制中心设置', items: ['更新运行模式'], risk: 'R2' },
        },
        'configuration.settings.apply': () => {
          persistedRecall = { detailLevel: 'detailed', timelineEnabled: true };
          return {
            ok: true,
            pathId: 'configuration.settings.apply',
            receiptId: 'receipt-memory-apply',
            payloadSha256,
            appliedAtMs: Date.now(),
            rollbackAvailable: true,
            rollbackToken: 'rollback-memory-apply',
          };
        },
        'input.lexicon.review': emptyReview,
      },
    });
    renderFeature(transport);

    await user.click(await screen.findByRole('radio', { name: '记忆增强' }));
    await user.click(screen.getByRole('button', { name: '保存使用方式' }));

    expect(await screen.findByText('已保存')).toBeInTheDocument();
    // 保存成功后草稿清空：勾选与“当前”一起回到真实生效的模式。
    await waitFor(() => {
      const memoryCard = screen.getByRole('radio', { name: '记忆增强' });
      expect(memoryCard).toHaveAttribute('data-current');
      expect(memoryCard).toHaveAttribute('data-state', 'checked');
    });
    expect(screen.getByText('当前设置与「记忆增强」一致。')).toBeInTheDocument();
    // 回执与撤销入口在草稿清空后仍然保留。
    expect(screen.getByRole('button', { name: '撤销' })).toBeInTheDocument();
  });

  it('reports a runtime profile that disagrees with the saved settings instead of blending them', async () => {
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '安全模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 3,
          settings: {
            interaction: { postCommit: { enabled: true } },
            memory: { enabled: true },
            rag: { lanes: { tagMemo: true, timeDailyBook: true } },
          },
        },
        'configuration.schema': { ok: true, sections: [] },
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByText('运行端与已保存设置不一致')).toBeInTheDocument();
    expect(screen.getByText(/运行端仍报告「安全模式」，已保存设置对应「标准模式」/)).toBeInTheDocument();
  });

  it('stays quiet when the runtime profile matches the saved settings', async () => {
    renderFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': {
          ok: true,
          runtimeRevision: 3,
          settings: {
            interaction: { postCommit: { enabled: true } },
            memory: { enabled: true },
            rag: { lanes: { tagMemo: true, timeDailyBook: true } },
          },
        },
        'configuration.schema': { ok: true, sections: [] },
        'input.lexicon.review': emptyReview,
      },
    }));

    expect(await screen.findByRole('radiogroup', { name: '使用方式' })).toBeInTheDocument();
    expect(screen.queryByText('运行端与已保存设置不一致')).not.toBeInTheDocument();
  });

  it('keeps lexicon review rows and the first app viewport readable at wide and narrow widths', async () => {
    const transport = new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': review,
      },
    });
    renderLexiconFeature(transport);

    const checkbox = await screen.findByRole('checkbox', { name: '选择 表情包' });
    const row = checkbox.closest('label');
    expect(row).toHaveAttribute('data-input-lexicon-row', 'true');
    expect(row?.children).toHaveLength(3);
    expect(row?.querySelector('.mgmt-list__content')).toBeInTheDocument();
    expect(row?.querySelector('.mgmt-status')).toBeInTheDocument();
    expect(row?.querySelector('.mgmt-list__content strong')).toHaveAttribute('title', '表情包');
    expect(row?.querySelector('.mgmt-list__content span')).toHaveAttribute('title', expect.stringContaining('被采用 3 次'));
    expect(screen.getAllByRole('button', { name: '刷新审阅' })).toHaveLength(1);
    expect(transport.requests.some(({ request }) => [
      'input.source.get',
      'overview.get',
      'diagnostics.models',
      'configuration.settings',
      'configuration.schema',
    ].includes(request.pathId))).toBe(false);

    expect(inputMethodCss).toMatch(
      /:is\(main, section\):is\(\[data-route-id='input'\], \[data-route-id='input-lexicon'\]\)\[data-paw-os-app\] > \.mgmt-page__body\s*\{[^}]*padding-top:/s,
    );
    expect(inputMethodCss).toMatch(
      /@container paw-window \(max-width: 860px\)[\s\S]*?\.input-lexicon-review__row\s*\{[^}]*grid-template-columns:\s*minmax\(20px, max-content\) minmax\(0, 1fr\) max-content/s,
    );
    expect(inputMethodCss).toMatch(
      /@container paw-window \(max-width: 560px\)[\s\S]*?\.input-lexicon-review__row\s*\{[\s\S]*?grid-template-areas:/s,
    );
  });

  it('does not render a write receipt when the server rejects a stale review token', async () => {
    const user = userEvent.setup();
    renderLexiconFeature(new MockControlTransport({
      routes: {
        'input.source.get': { ok: true, typingReady: true, readinessState: 'ready' },
        'overview.get': { ok: true, profile: '标准模式' },
        'configuration.settings': settings,
        'configuration.schema': schema,
        'input.lexicon.review': review,
        'input.lexicon.apply': {
          schemaVersion: 'rag-ime.rime-lexicon-review.v1',
          ok: false,
          applied: false,
          reason: 'review_token_stale',
        },
        'input.lexicon.rollback': {},
      },
    }));

    await screen.findByRole('checkbox', { name: '选择 表情包' });
    await user.click(screen.getByRole('button', { name: '加入所选词条' }));

    expect(await screen.findByText('词库审阅已变化，请刷新后重新选择。')).toBeInTheDocument();
    expect(screen.queryByText('已加入 · 等待重载')).not.toBeInTheDocument();
  });
});

describe('inferInputMode', () => {
  function settingsWithRecall(recall: Record<string, unknown>): Record<string, unknown> {
    return {
      interaction: { postCommit: { enabled: true } },
      memory: { enabled: true, recall },
      rag: { lanes: { tagMemo: true, timeDailyBook: true } },
    };
  }

  it('separates the two memory presets by recall depth and timeline, honestly falling back to custom', () => {
    expect(inferInputMode(settingsWithRecall({ detailLevel: 'compact', timelineEnabled: false }))).toBe('标准模式');
    expect(inferInputMode(settingsWithRecall({ detailLevel: 'detailed', timelineEnabled: true }))).toBe('记忆增强');
    // 详尽召回但关掉时间线不属于任何预设；均衡档是手动中间档。
    expect(inferInputMode(settingsWithRecall({ detailLevel: 'detailed', timelineEnabled: false }))).toBe('');
    expect(inferInputMode(settingsWithRecall({ detailLevel: 'balanced', timelineEnabled: true }))).toBe('');
    // 老的设置负载没有召回键时仍按基础预设读作标准模式。
    expect(inferInputMode(settingsWithRecall({}))).toBe('标准模式');
  });

  it('keeps the debug and safe presets ahead of the memory split', () => {
    expect(inferInputMode({
      interaction: { postCommit: { enabled: false } },
      memory: { enabled: false },
      activeRag: { allowRemoteModel: false },
    })).toBe('安全模式');
    expect(inferInputMode({
      diagnostics: { liveTrace: true, candidateExplain: true },
      display: { showDiagnosticsInline: true },
    })).toBe('调试模式');
  });
});

describe('modeFactChips', () => {
  it('signs every card with exactly the keys the mode really writes', () => {
    for (const mode of presetInputModes) {
      const chips = modeFactChips(mode);
      // 一键一枚：事实签名不能多于、也不能少于真实写入。
      expect(chips.map((chip) => chip.key)).toEqual(Object.keys(inputModeChanges[mode]));
      for (const chip of chips) {
        expect(chip.off).toBe(inputModeChanges[mode][chip.key] === false);
      }
    }
  });

  it('highlights only the honest split between 标准 and 记忆增强', () => {
    const highlighted = (mode: (typeof presetInputModes)[number]) =>
      modeFactChips(mode).filter((chip) => chip.highlight).map((chip) => chip.label);
    expect(highlighted('标准模式')).toEqual(['紧凑召回']);
    expect(highlighted('记忆增强')).toEqual(['详尽召回', '按需时间线']);
    expect(highlighted('安全模式')).toEqual([]);
    expect(highlighted('调试模式')).toEqual([]);
  });
});

describe('generation stage presentation', () => {
  it('drops frames without adapter-compatible identity and never projects private fields', () => {
    expect(predictionLiveTraceSummaries({
      frames: [
        { sessionId: '', requestSeq: 2, recordedAtMs: 1 },
        { sessionId: 's', requestSeq: 0, recordedAtMs: 1 },
        {
          sessionId: 's',
          requestSeq: 2,
          recordedAtMs: 3,
          input: { text: 'private' },
          preedit: { text: 'private' },
          modelLane: { called: true, suggestionCount: 0, elapsedMs: 0 },
        },
      ],
    })).toEqual([{
      sessionId: 's',
      requestSeq: 2,
      recordedAtMs: 3,
      traceId: 'trace:prediction:s:request-2',
      ragLane: { status: 'not_reported', count: null, elapsedMs: null },
      modelLane: { status: 'completed', count: 0, elapsedMs: 0 },
    }]);
    expect(JSON.stringify(predictionLiveTraceSummaries({ frames: [] }))).not.toContain('private');
  });

  it('derives the suggestion panel schematic only from real settings', () => {
    // 没有配置时不假装知道数量或采纳方式；开关默认沿用产品默认（开启）。
    expect(suggestionPanel({})).toEqual({ enabled: true, candidateCount: 0, expanded: false, hints: [] });
    expect(suggestionPanel({
      interaction: {
        postCommit: {
          enabled: true,
          tabAction: 'accept_top_prediction',
          optionNumber: 'select_prediction_by_ordinal',
          panelTtlMs: 1500,
        },
      },
      display: { maxPostCommitCandidates: 6, panelStyle: 'expanded' },
    })).toEqual({
      enabled: true,
      candidateCount: 6,
      expanded: true,
      hints: ['Tab 采纳第 1 条', 'Option+数字 选对应候选', '约 1.5 秒后自动收起'],
    });
    // 关闭后不再宣称任何采纳方式。
    expect(suggestionPanel({ interaction: { postCommit: { enabled: false, panelTtlMs: 4000 } } })).toEqual({
      enabled: false,
      candidateCount: 0,
      expanded: false,
      hints: [],
    });
    // 超界数量不进入示意，避免画出并不存在的候选。
    expect(suggestionPanel({ display: { maxPostCommitCandidates: 40 } }).candidateCount).toBe(0);
    expect(secondsLabel(220)).toBe('0.2 秒');
    expect(secondsLabel(4000)).toBe('4 秒');
  });

  it('keeps recall and completion lanes honest about disabled and partial settings', () => {
    const recallOff = recallLaneFacts({ memory: { enabled: false } });
    expect(recallOff.enabled).toBe(false);
    expect(recallOff.facts).toEqual([]);
    expect(recallLaneFacts({
      memory: { enabled: true, recall: { detailLevel: 'compact' } },
      rag: { lanes: { tagMemo: true, timeDailyBook: false } },
    }).facts).toEqual(['标签记忆', '紧凑召回']);

    expect(completionLaneFacts({
      interaction: { postCommit: { enabled: true, idleTriggerMs: 220 } },
      display: { maxPostCommitCandidates: 5 },
    }, 'minimind-ime-v2').facts).toEqual(['minimind-ime-v2', '每次最多 5 条', '停顿 0.2 秒后生成']);
    const completionOff = completionLaneFacts({ interaction: { postCommit: { enabled: false } } }, 'minimind-ime-v2');
    expect(completionOff.enabled).toBe(false);
    expect(completionOff.facts).toEqual([]);
  });
});

function renderFeature(transport: MockControlTransport) {
  renderInputFeature(transport, <InputMethodFeature />);
}

function renderLexiconFeature(transport: MockControlTransport) {
  renderInputFeature(transport, <InputLexiconFeature />);
}

function renderInputFeature(transport: MockControlTransport, feature: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <TooltipProvider delayDuration={0}>
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={client}>
          {feature}
        </QueryClientProvider>
      </ControlTransportProvider>
    </TooltipProvider>,
  );
}
