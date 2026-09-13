export function previewConfigurationValues(): Record<string, unknown> {
  return {
    identity: {
      productName: 'PAW',
      assistantName: 'Agent',
      tagline: '记得你，也陪你做事',
    },
    interaction: {
      postCommit: {
        enabled: true,
        idleTriggerMs: 220,
        minDeltaChars: 2,
        maxCallsPer10s: 6,
        cooldownMs: 1500,
        panelTtlMs: 4000,
        modelBudgetMs: 900,
        tabAction: 'accept_top_prediction',
        optionNumber: 'select_prediction_by_ordinal',
      },
    },
    display: {
      maxPostCommitCandidates: 5,
      panelStyle: 'compact',
      showDiagnosticsInline: false,
    },
    models: {
      modelId: 'minimind-ime-v2',
      hot: 'minimind_ime_v2',
      path: '/Library/Application Support/RAG-IME/models/minimind-ime-v2',
      promptMode: 'base-completion',
      maxTokens: 8,
      temperature: 0.15,
      topP: 0.85,
    },
    activeRag: {
      enabled: true,
      quickModel: 'deepseek/deepseek-v4-flash',
      quickThinkingLevel: 'high',
      defaultPlacement: 'replace_selection',
      latencyBudgetMs: 8000,
      allowRemoteModel: true,
    },
    pinyin: { fuzzyProfile: 'sichuan-mild' },
    lexiconOrganization: { enabled: true, runsPerDay: 2 },
    rag: { lanes: { tagMemo: true, timeDailyBook: true } },
    memory: {
      enabled: true,
      retentionDays: 30,
      automaticOrganization: {
        enabled: true,
        runsPerDay: 2,
        model: 'gpt/gpt-5.6-luna',
        thinkingLevel: 'max',
      },
      dreaming: {
        enabled: true,
        runsPerDay: 2,
        model: 'gpt/gpt-5.6-luna',
        thinkingLevel: 'max',
      },
      recall: { detailLevel: 'compact', timelineEnabled: true },
      catalogConsolidation: { enabled: true, cadenceDays: 7 },
    },
    context: {
      recentInputBaseline: 20,
      tokenBudget: 4096,
      temporalRecall: true,
    },
    planning: { enabled: true, injectIntoContext: true },
    agent: { pi: { enabled: true, idleTimeoutSeconds: 900, resumeLastSession: true } },
    voice: {
      provider: 'native_streaming',
      hotkey: 'middle_mouse',
      hotwordsEnabled: true,
      hotwords: ['伙伴工具'],
      refinementModel: 'inherit',
      refinementThinkingLevel: 'off',
    },
    privacy: {
      traceIncludeText: false,
      debugIncludeText: false,
      debugContextDirectory: '/Volumes/External/Cheng/context-snapshots',
      debugContextMaxGiB: 5,
      debugContextMaxCallsPerTurn: 128,
      redactSecrets: true,
    },
  };
}

export function previewConfigurationSettings(
  settings: Record<string, unknown> = previewConfigurationValues(),
  runtimeRevision = 12,
): Record<string, unknown> {
  const settingsRevision = `sha256:preview-settings-${runtimeRevision}`;
  return {
    ok: true,
    configured: true,
    settingsHash: settingsRevision,
    runtimeRevision,
    runtimeConfig: { runtimeRevision, settingsRevision },
    settings,
  };
}

export function applyPreviewConfigurationChanges(
  current: Record<string, unknown>,
  changes: Record<string, unknown>,
): Record<string, unknown> {
  const next = { ...current };
  for (const [path, value] of Object.entries(changes)) {
    const segments = path.split('.').filter(Boolean);
    if (segments.length === 0) continue;
    let target = next;
    for (const segment of segments.slice(0, -1)) {
      const currentChild = target[segment];
      const child = {
        ...(typeof currentChild === 'object' && currentChild !== null && !Array.isArray(currentChild)
          ? currentChild as Record<string, unknown>
          : {}),
      };
      target[segment] = child;
      target = child;
    }
    target[segments[segments.length - 1]!] = value;
  }
  return next;
}

export function previewLexiconReview(): Record<string, unknown> {
  const nowMs = Date.now();
  return {
    schemaVersion: 'rag-ime.rime-lexicon-review.v1',
    ok: true,
    project: 'wisdom-weasel-rag-ime',
    entryCount: 2,
    entries: [
      {
        reviewKey: 'preview-lexicon-01',
        text: '长期协作',
        pinyin: 'chang qi xie zuo',
        weight: 12,
        positiveCount: 4,
        negativeCount: 0,
        reasons: ['多次主动选择'],
        reviewSource: 'local_feedback',
        reviewReason: '跨日重复使用，建议保留',
        selected: true,
        defaultSelected: true,
        riskLabel: '低风险',
      },
      {
        reviewKey: 'preview-lexicon-02',
        text: '前台验收',
        pinyin: 'qian tai yan shou',
        weight: 8,
        positiveCount: 2,
        negativeCount: 0,
        reasons: ['近期重复输入'],
        reviewSource: 'local_feedback',
        reviewReason: '等待你确认是否写入词库',
        selected: false,
        defaultSelected: false,
        riskLabel: '需确认',
      },
    ],
    reviewToken: 'a'.repeat(64),
    confirmText: 'APPLY REVIEWED RIME LEXICON',
    applySupported: true,
    reviewRequired: true,
    filteredEntryCount: 1,
    selectionPolicy: 'review_required',
    organization: {
      schemaVersion: 'rag-ime.lexicon-organization-status.v1',
      owner: 'maintenance_poll',
      decoderOwner: 'rime',
      enabled: true,
      runsPerDay: 2,
      intervalMs: 43_200_000,
      candidateLimit: 200,
      lastRunAtMs: nowMs - 43_200_000,
      lastSucceededAtMs: nowMs - 43_200_000,
      nextRunAtMs: nowMs + 43_200_000,
      due: false,
      lastRun: {
        runId: 'lexicon-run-preview',
        status: 'succeeded',
        startedAtMs: nowMs - 43_201_250,
        completedAtMs: nowMs - 43_200_000,
        candidateCount: 3,
        filteredEntryCount: 1,
        errorCode: '',
        error: '',
      },
    },
  };
}

export function previewConfigurationSchema(): Record<string, unknown> {
  return {
    ok: true,
    schemaVersion: 'rag-ime.settings-schema.v3',
    sections: [
      {
        id: 'identity',
        label: '称呼与外观',
        fields: [
          {
            key: 'identity.productName',
            type: 'string',
            label: '应用名称',
            description: '显示在侧栏和窗口标题中；不会改变安装包文件名',
            minLength: 1,
            maxLength: 24,
            applyMode: 'live',
          },
          {
            key: 'identity.assistantName',
            type: 'string',
            label: '通用伙伴称呼',
            description: '没有指向某位具体伙伴时使用；自建伙伴可以单独命名，内置伙伴复制后也能调整',
            minLength: 1,
            maxLength: 24,
            applyMode: 'live',
          },
          {
            key: 'identity.tagline',
            type: 'string',
            label: '侧栏短句',
            description: '应用名称下方的一句短介绍',
            minLength: 1,
            maxLength: 48,
            applyMode: 'live',
          },
        ],
      },
      {
        id: 'interaction',
        label: '输入交互',
        fields: [
          { key: 'interaction.postCommit.enabled', type: 'boolean', label: '启用提交后预测', applyMode: 'live' },
          { key: 'interaction.postCommit.idleTriggerMs', type: 'integer', label: '停顿触发时间', min: 40, max: 1000, step: 20, unit: 'ms', applyMode: 'restart_input_method' },
          { key: 'interaction.postCommit.minDeltaChars', type: 'integer', label: '最少新增字符数', min: 1, max: 32, unit: '字符', applyMode: 'live' },
          { key: 'interaction.postCommit.maxCallsPer10s', type: 'integer', label: '10 秒最大模型调用', min: 0, max: 10, unit: '次', applyMode: 'live' },
          { key: 'interaction.postCommit.cooldownMs', type: 'integer', label: '空结果冷却', min: 0, max: 10000, step: 100, unit: 'ms', applyMode: 'live' },
          { key: 'interaction.postCommit.panelTtlMs', type: 'integer', label: '生成结果停留时间', min: 500, max: 30000, step: 500, unit: 'ms', applyMode: 'restart_input_method' },
          { key: 'interaction.postCommit.modelBudgetMs', type: 'integer', label: '本机模型最长等待', min: 300, max: 12000, step: 100, unit: 'ms', applyMode: 'live' },
          { key: 'interaction.postCommit.tabAction', type: 'enum', label: 'Tab 行为', options: ['accept_top_prediction', 'rime_default', 'disabled'], applyMode: 'live' },
          { key: 'interaction.postCommit.optionNumber', type: 'enum', label: 'Option+数字', options: ['select_prediction_by_ordinal', 'disabled'], applyMode: 'live' },
        ],
      },
      {
        id: 'models',
        label: '本机即时预测',
        fields: [
          { key: 'models.modelId', type: 'string', label: '注册模型', maxLength: 128, applyMode: 'restart_predictor' },
          { key: 'models.hot', type: 'enum', label: '推理 Profile', options: ['minimind_ime_v2', 'qwen3_06b_ime_hot'], applyMode: 'restart_predictor' },
          { key: 'models.path', type: 'string', label: '本机模型目录', maxLength: 1024, applyMode: 'restart_predictor' },
          { key: 'models.promptMode', type: 'enum', label: 'Prompt 模式', options: ['base-completion', 'chat-json'], applyMode: 'restart_predictor' },
          { key: 'models.maxTokens', type: 'integer', label: '最大生成 Token', min: 1, max: 64, applyMode: 'restart_predictor' },
          { key: 'models.temperature', type: 'number', label: '生成随机度', min: 0, max: 2, step: 0.05, applyMode: 'restart_predictor' },
          { key: 'models.topP', type: 'number', label: '候选采样范围', min: 0, max: 1, step: 0.05, applyMode: 'restart_predictor' },
        ],
      },
      {
        id: 'activeRag',
        label: 'Active RAG',
        fields: [
          { key: 'activeRag.enabled', type: 'boolean', label: '启用深度生成', applyMode: 'live' },
          { key: 'activeRag.quickModel', type: 'pi-model', label: '闪电生成模型', applyMode: 'live' },
          { key: 'activeRag.quickThinkingLevel', type: 'pi-thinking', modelKey: 'activeRag.quickModel', label: '闪电生成思考', applyMode: 'live' },
          { key: 'activeRag.defaultPlacement', type: 'enum', label: '插入方式', options: ['replace_selection', 'insert_after_selection', 'show_only'], applyMode: 'live' },
          { key: 'activeRag.latencyBudgetMs', type: 'integer', label: '生成框最长等待', min: 2000, max: 30000, step: 1000, unit: 'ms', applyMode: 'live' },
        ],
      },
      {
        id: 'display',
        label: '候选面板',
        fields: [
          { key: 'display.maxPostCommitCandidates', type: 'integer', label: '智能候选数量', min: 1, max: 8, unit: '项', applyMode: 'restart_input_method' },
          { key: 'display.panelStyle', type: 'enum', label: '候选界面', options: ['compact', 'expanded'], applyMode: 'restart_input_method' },
        ],
      },
      {
        id: 'pinyin',
        label: '拼音习惯',
        fields: [
          { key: 'pinyin.fuzzyProfile', type: 'enum', label: '模糊音配置', options: ['sichuan-mild', 'none'], applyMode: 'redeploy_rime' },
        ],
      },
      {
        id: 'lexiconOrganization',
        label: '词库定期整理',
        fields: [
          { key: 'lexiconOrganization.enabled', type: 'boolean', label: '启用定期整理', applyMode: 'live' },
          { key: 'lexiconOrganization.runsPerDay', type: 'integer', label: '每天整理次数', min: 1, max: 24, unit: '次/天', applyMode: 'live' },
        ],
      },
      {
        id: 'memory',
        label: 'Memory',
        fields: [
          { key: 'memory.enabled', type: 'boolean', label: '启用长期记忆', applyMode: 'live' },
          { key: 'memory.retentionDays', type: 'integer', label: '原始输入保留天数', min: 1, max: 3650, applyMode: 'live' },
          { key: 'memory.automaticOrganization.model', type: 'pi-model', label: '自动整理模型', applyMode: 'live' },
          { key: 'memory.automaticOrganization.thinkingLevel', type: 'pi-thinking', modelKey: 'memory.automaticOrganization.model', label: '自动整理思考', applyMode: 'live' },
          { key: 'memory.dreaming.model', type: 'pi-model', label: '做梦模型', applyMode: 'live' },
          { key: 'memory.dreaming.thinkingLevel', type: 'pi-thinking', modelKey: 'memory.dreaming.model', label: '做梦思考', applyMode: 'live' },
          { key: 'memory.recall.detailLevel', type: 'enum', label: '召回正文密度', options: ['compact', 'expanded'], applyMode: 'live' },
          { key: 'memory.recall.timelineEnabled', type: 'boolean', label: '允许时间线召回', applyMode: 'live' },
        ],
      },
      {
        id: 'context',
        label: 'Context',
        fields: [
          { key: 'context.recentInputBaseline', type: 'integer', label: '近期输入基线', min: 0, max: 80, applyMode: 'live' },
          { key: 'context.tokenBudget', type: 'integer', label: '上下文容量', min: 512, max: 32768, applyMode: 'live' },
          { key: 'context.temporalRecall', type: 'boolean', label: '理解时间表达', applyMode: 'live' },
        ],
      },
      {
        id: 'planning',
        label: 'Planning',
        fields: [
          { key: 'planning.enabled', type: 'boolean', label: '启用任务规划', applyMode: 'live' },
          { key: 'planning.injectIntoContext', type: 'boolean', label: '向 Agent 提供当前任务', applyMode: 'live' },
        ],
      },
      {
        id: 'agent',
        label: 'Agent',
        fields: [
          { key: 'agent.pi.enabled', type: 'boolean', label: '连接 Pi', applyMode: 'live' },
          { key: 'agent.pi.idleTimeoutSeconds', type: 'integer', label: '空闲退出时间', min: 0, max: 86400, applyMode: 'live' },
          { key: 'agent.pi.systemProxy', type: 'boolean', label: '远程模型跟随系统代理', applyMode: 'restart_agent_gateway' },
          { key: 'agent.pi.resumeLastSession', type: 'boolean', label: '恢复上次对话', applyMode: 'live' },
        ],
      },
      {
        id: 'privacy',
        label: 'Privacy',
        fields: [
          {
            key: 'privacy.debugIncludeText',
            type: 'boolean',
            label: '保存并查看本机上下文快照',
            applyMode: 'restart_agent_gateway',
          },
          {
            key: 'privacy.debugContextDirectory',
            type: 'string',
            label: '上下文快照目录（支持外置硬盘）',
            applyMode: 'restart_agent_gateway',
            maxLength: 1024,
          },
          {
            key: 'privacy.debugContextMaxGiB',
            type: 'integer',
            label: '上下文快照容量',
            min: 1,
            max: 64,
            unit: 'GiB',
            applyMode: 'restart_agent_gateway',
          },
          {
            key: 'privacy.debugContextMaxCallsPerTurn',
            type: 'integer',
            label: '每回合保留的模型调用',
            min: 1,
            max: 256,
            unit: '次/回合',
            applyMode: 'restart_agent_gateway',
            expert: true,
          },
          { key: 'privacy.traceIncludeText', type: 'boolean', label: '诊断记录包含正文', applyMode: 'live', expert: true },
          { key: 'privacy.redactSecrets', type: 'boolean', label: '诊断中隐藏秘密', applyMode: 'live' },
        ],
      },
    ],
  };
}
