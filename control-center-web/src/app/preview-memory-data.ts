import type { ControlRequest } from '@/platform/transport';
import type {
  MemoryReferenceV1,
  Reference,
  ReferenceKind,
} from '@/contracts/generated/memory-reference.v1';

const previewGovernanceTopic = {
  id: 'book:preview-memory-governance',
  title: '记忆治理与桌面上下文',
  summary: '按用户、角色和项目隔离证据；每日整理先生成可审阅草案。',
};

export function previewMemoryPage(
  request: ControlRequest,
  _evidenceDisposition: string,
): Record<string, unknown> {
  const kind = stringValue(record(request.params).kind);
  if (kind === 'evidence') {
    return {
      ok: true,
      items: [
        {
          id: 'source:preview-input-method',
          title: '候选选择后，退格表示这次候选不合适。',
          detail: '已整理进长期记忆',
          status: 'consolidated',
          disposition: 'consolidated',
          sourceChannel: 'input_method',
          transportSource: 'squirrel_rime_commit_burst',
          source: { type: 'input_event', id: 'event:10001' },
          ref: { type: 'evidence', id: 'source:preview-input-method' },
          evidenceRefs: [{ kind: 'event', referenceId: 'event:10001' }],
          type: 'user_final',
          ownerKind: 'user',
          ownerId: 'default',
          updatedAtMs: Date.now() - 86_400_000,
        },
        {
          id: 'source:preview-voice',
          title: '语音输入适合记录较长的完整想法。',
          detail: '已整理进长期记忆',
          status: 'consolidated',
          disposition: 'consolidated',
          sourceChannel: 'voice',
          transportSource: 'voice_streaming_asr',
          source: { type: 'input_event', id: 'event:10002' },
          ref: { type: 'evidence', id: 'source:preview-voice' },
          evidenceRefs: [{ kind: 'event', referenceId: 'event:10002' }],
          type: 'user_final',
          ownerKind: 'user',
          ownerId: 'default',
          updatedAtMs: Date.now() - 7_200_000,
        },
        {
          id: 'evidence:preview-agent-capture',
          title: '桌面上下文默认读取 Accessibility Tree，截图仅作兜底。',
          detail: '伙伴主动记录',
          status: 'active',
          sourceChannel: 'agent_capture',
          source: { type: 'agent_memory_evidence', id: 'evidence:preview-agent-capture' },
          ref: { type: 'evidence', id: 'evidence:preview-agent-capture' },
          evidenceRefs: [{
            kind: 'event',
            referenceId: 'event:10003',
            title: '伙伴主动记录的长期记忆边界',
          }],
          type: 'session_digest',
          ownerKind: 'agent',
          ownerId: 'companion-present-v1',
          updatedAtMs: Date.now() - 3_600_000,
        },
      ],
      nextCursor: '',
      limit: 50,
    };
  }
  if (kind === 'books') {
    return {
      ok: true,
      items: [
        {
          ...previewGovernanceTopic,
          status: 'active',
          source: { type: 'memory_book', id: 'book:preview-memory-governance' },
          ref: { type: 'book', id: 'book:preview-memory-governance' },
          evidenceRefs: [
            { kind: 'atom', referenceId: 'atom:input-boundary', title: '输入段封口规则' },
            { kind: 'atom', referenceId: 'atom:timeline-governance', title: '时间线必须审批后参与召回' },
          ],
          type: 'topic',
          ownerKind: 'user',
          ownerId: 'default',
          tags: ['记忆治理', '桌面上下文'],
          updatedAtMs: Date.now() - 7_200_000,
        },
      ],
      nextCursor: '',
      limit: 50,
    };
  }
  if (kind === 'timelines') {
    const date = new Date().toISOString().slice(0, 10);
    const timeline = previewActivityTimeline(date, 'approved');
    return {
      ok: true,
      items: [{
        id: timeline.timelineId,
        title: `${date} 语义任务时间线`,
        summary: timeline.summary,
        status: timeline.status,
        type: 'daily_activity_timeline',
        taskCount: timeline.segmentCount,
        eventCount: timeline.eventCount,
        source: timeline.source,
        ref: timeline.ref,
        evidenceRefs: (timeline.segments as Record<string, unknown>[])
          .flatMap((segment) => segment.evidenceRefs as Record<string, unknown>[]),
        updatedAtMs: timeline.updatedAtMs,
      }],
      nextCursor: '',
      limit: 50,
    };
  }
  return previewMemoryCatalogPage(kind);
}

export function previewMemoryReference(kind: string, referenceId: string): MemoryReferenceV1 {
  const now = Date.now();
  const referenceKind: ReferenceKind = (
    ['event', 'evidence', 'atom', 'book', 'timeline', 'role_book_revision'] as const
  ).includes(kind as ReferenceKind)
    ? kind as ReferenceKind
    : 'role_book_revision';
  const base = {
    schemaVersion: 'rag-ime.memory-reference.v1' as const,
    settingsRevision: 'settings:preview',
    runtimeRevision: 1,
    ok: true as const,
    kind: referenceKind,
    referenceId,
  };
  const makeReference = (
    targetKind: ReferenceKind,
    targetId: string,
    label?: string,
  ): Reference => ({
    kind: targetKind,
    id: targetId,
    referenceKind: targetKind,
    referenceId: targetId,
    ...(label ? { label } : {}),
  });

  if (referenceKind === 'event') {
    const text = referenceId.endsWith('10001')
      ? '嗯嗯那个这个'
      : '输入框最终文本已在提交时形成可追溯证据。';
    return {
      ...base,
      source: { kind: 'input_event', sourceKind: 'squirrel_input_segment', id: referenceId },
      ref: makeReference('event', referenceId),
      item: {
        id: referenceId,
        title: '原始完整输入',
        text,
        textPreview: text,
        status: 'active',
        sourceKind: 'squirrel_input_segment',
        app: referenceId.endsWith('10001') ? 'com.mitchellh.ghostty' : 'com.openai.codex',
        ownerKind: 'user',
        ownerId: 'default',
        occurredAtMs: now - 3_600_000,
      },
      evidenceRefs: [],
    };
  }
  if (referenceKind === 'evidence') {
    return {
      ...base,
      source: { kind: 'agent_memory_evidence', id: referenceId },
      ref: makeReference('evidence', referenceId),
      item: {
        id: referenceId,
        title: '伙伴对话整理依据',
        detail: '桌面上下文默认读取 Accessibility Tree，截图仅在语义不足时兜底。',
        status: 'active',
        ownerKind: 'agent',
        ownerId: 'companion-present-v1',
        updatedAtMs: now - 2_400_000,
      },
      evidenceRefs: [makeReference('event', 'event:10002', '原始对话输入')],
    };
  }
  if (referenceKind === 'atom') {
    const timelineBoundary = referenceId.includes('timeline');
    return {
      ...base,
      source: { kind: 'memory_atom', id: referenceId },
      ref: makeReference('atom', referenceId),
      item: {
        id: referenceId,
        title: timelineBoundary ? '时间线联想边界' : '输入段封口规则',
        text: timelineBoundary
          ? '已批准时间线可用于活动背景，但不能单独证明稳定事实。'
          : '输入框最终文本优先，Enter 或切换 App 后才形成完整输入段。',
        status: 'active',
        claimState: 'current',
        ownerKind: 'user',
        ownerId: 'default',
        updatedAtMs: now - 120_000,
      },
      evidenceRefs: timelineBoundary
        ? [makeReference('evidence', 'evidence:preview-compaction', '伙伴对话整理依据')]
        : [makeReference('event', 'event:10003', '输入框最终文本采集记录')],
    };
  }
  if (referenceKind === 'book') {
    return {
      ...base,
      source: { kind: 'memory_book', id: referenceId },
      ref: makeReference('book', referenceId),
      item: {
        id: referenceId,
        title: referenceId === previewGovernanceTopic.id ? previewGovernanceTopic.title
          : referenceId.includes('agent-runtime') ? '伙伴运行' : '输入法记忆与上下文',
        summary: '聚合当前 Atom 和来源证据，作为主题检索入口。',
        status: 'active',
        type: 'topic',
        ownerKind: 'user',
        ownerId: 'default',
        updatedAtMs: now - 90_000,
      },
      evidenceRefs: [
        makeReference('atom', 'atom:input-boundary', '输入段封口规则'),
        makeReference('atom', 'atom:timeline-governance', '时间线联想边界'),
      ],
    };
  }
  if (referenceKind === 'timeline') {
    const matchedDate = referenceId.match(/(20\d{2}-\d{2}-\d{2})/u)?.[1]
      ?? new Date().toISOString().slice(0, 10);
    const timeline = previewActivityTimeline(matchedDate, 'approved');
    return {
      ...base,
      source: { kind: 'activity_timeline', id: referenceId },
      ref: makeReference('timeline', referenceId),
      item: {
        ...timeline,
        id: referenceId,
        title: `${matchedDate} 语义任务时间线`,
        status: 'approved',
      },
      evidenceRefs: [makeReference('event', 'event:10002', '时间线来源输入')],
    };
  }
  return {
    ...base,
    source: { kind: 'role_book_revision', id: referenceId },
    ref: makeReference('role_book_revision', referenceId),
    item: {
      id: referenceId,
      title: 'Agent 设置',
      detail: '维护角色使命、能力画像、协作习惯与已验证教训。',
      status: 'active',
      ownerKind: 'agent',
      ownerId: 'companion-present-v1',
      updatedAtMs: now - 60_000,
    },
    evidenceRefs: [makeReference('evidence', 'evidence:preview-compaction', '最近角色整理证据')],
  };
}

export function previewMemorySummary(timelineStatuses = new Map<string, string>()): Record<string, unknown> {
  const today = new Date().toISOString().slice(0, 10);
  const currentTimelineStatus = timelineStatuses.get(today) || 'draft';
  const activityTimelineCounts: Record<string, number> = { approved: 11 };
  activityTimelineCounts[currentTimelineStatus] = (activityTimelineCounts[currentTimelineStatus] ?? 0) + 1;
  return {
    ok: true,
    runtimeRevision: 7,
    appCount: 4,
    completeInputCount: 3_602,
    blockedFragmentCount: 4_887,
    memoryBookCount: 15,
    memoryAtomCount: 123,
    memoryAtomArchivedCount: 21,
    memoryAtomSourceArchiveCount: 44,
    memoryTagCount: 93,
    pendingCompileEvents: 0,
    evidenceSourceCount: 2,
    memoryEvidenceCount: 3,
    inputMethodEvidenceCount: 1,
    voiceEvidenceCount: 1,
    agentCapturedSourceCount: 0,
    agentCapturedEvidenceCount: 1,
    forgottenSourceCount: 0,
    needsReviewSourceCount: 0,
    currentAtomCount: 123,
    historicalAtomCount: 21,
    agentEvidenceCount: 1,
    agentEvidenceTombstonedCount: 4,
    roleBookRevisionCounts: { active: 3, draft: 1, superseded: 8 },
    activityTimelineCounts,
    governanceProposalCounts: { preview: 2, applied: 14, rolled_back: 1 },
    latestActivityTimeline: {
      date: today,
      status: currentTimelineStatus,
      updatedAtMs: Date.now() - 90_000,
    },
    projection: {
      fresh: true,
      backlog: 0,
      dead: 0,
      retrievalDocuments: 151,
      checkpointCaughtUp: true,
      vectorCoverage: 1,
    },
    owners: [
      { ownerKind: 'user', ownerId: 'default', itemCount: 124 },
      { ownerKind: 'agent', ownerId: 'companion-present-v1', itemCount: 16 },
    ],
  };
}

export function previewActivityTimeline(date: string, status: string): Record<string, unknown> {
  const dayStart = localPreviewDayStart(date);
  const hash = '8d4a2d9c1fc84d408f8fe9a314f34c767b28e32e5b6461d73cc8e22d2209dc11';
  const segments = [
    previewActivitySegment({
      id: 'codex-account-switch',
      position: 0,
      title: 'CAS 切换 Codex 账号',
      apps: ['com.mitchellh.ghostty', 'com.openai.codex'],
      startMs: dayStart + 8.4 * 3_600_000,
      endMs: dayStart + 8.58 * 3_600_000,
      eventCount: 8,
      summary: '在 Ghostty 中使用 CAS 切换账号，随后回到 Codex 验证新账号会话；跨 App 事件属于同一个任务。',
      sourceKinds: ['squirrel_input_segment', 'pi_agent'],
      contextGroupIds: ['group:codex-account'],
    }),
    previewActivitySegment({
      id: 'memory-redesign',
      position: 1,
      title: '重构个人上下文记忆系统',
      apps: ['com.openai.codex', 'com.google.Chrome'],
      startMs: dayStart + 9.2 * 3_600_000,
      endMs: dayStart + 12.1 * 3_600_000,
      eventCount: 27,
      summary: '围绕来源记录、已整理记忆、主题、伙伴记忆和时间线的边界完成方案核对与资料查证。',
      sourceKinds: ['pi_agent', 'browser_extension', 'squirrel_input_segment'],
      contextGroupIds: ['group:personal-context', 'group:memory-evaluation'],
      redactedEventCount: 2,
    }),
    previewActivitySegment({
      id: 'timeline-implementation',
      position: 2,
      title: '实现语义时间线与来源下钻',
      apps: ['com.microsoft.VSCode', 'com.mitchellh.ghostty', 'com.openai.codex'],
      startMs: dayStart + 13.2 * 3_600_000,
      endMs: dayStart + 17.1 * 3_600_000,
      eventCount: 36,
      summary: '实现跨 App 任务聚合、记忆联想和逐层来源查看，并运行 Web、后端与输入法集成验证。',
      sourceKinds: ['squirrel_input_segment', 'pi_agent'],
      contextGroupIds: ['group:input-method', 'group:personal-context', 'group:verification'],
    }),
  ];
  return {
    schemaVersion: 'rag-ime.daily-activity-timeline.v1',
    timelineId: `timeline:${date}`,
    project: 'wisdom-weasel-rag-ime',
    date,
    timezone: 'Asia/Shanghai',
    status,
    segmentationMode: 'semantic_task_v5',
    sourceEventIds: segments.flatMap((segment) => segment.sourceEventIds as number[]),
    sourceEventHash: hash,
    segments,
    summary: '当天完成 Codex 账号切换、个人上下文架构重构，以及语义时间线和来源下钻的实现验证。',
    eventCount: segments.reduce((sum, segment) => sum + Number(segment.eventCount), 0),
    segmentCount: segments.length,
    observedStartMs: Math.min(...segments.map((segment) => Number(segment.startMs))),
    observedEndMs: Math.max(...segments.map((segment) => Number(segment.endMs))),
    spanSemantics: 'first_to_last_source_event',
    ordinaryActivityCount: segments.filter((segment) => segment.activityKind === 'ordinary_activity').length,
    consolidatedActivityCount: segments.filter((segment) => segment.activityKind === 'consolidated_activity').length,
    approvedBookId: status === 'approved' ? `book:daily:${date}` : '',
    approvedBy: status === 'approved' ? 'control-center-user' : '',
    approvedAtMs: status === 'approved' ? Date.now() - 30_000 : 0,
    createdAtMs: dayStart + 18 * 3_600_000,
    updatedAtMs: Date.now() - 30_000,
    source: { type: 'daily_activity_timeline', id: `timeline:${date}` },
    ref: { type: 'timeline', id: `timeline:${date}` },
    policy: {
      derivedFromInputEvents: true,
      longTermFact: false,
      automaticPromotion: true,
      explicitApprovalRequired: false,
      minimumConsolidatedSpanMs: 30 * 60_000,
    },
  };
}

export function previewActivityTimelineCalendar(
  month: string,
  statuses: ReadonlyMap<string, string>,
): Record<string, unknown> {
  const monthStart = new Date(`${month}-01T00:00:00`);
  const daysInMonth = Number.isNaN(monthStart.getTime())
    ? 30
    : new Date(monthStart.getFullYear(), monthStart.getMonth() + 1, 0).getDate();
  const today = new Date().toISOString().slice(0, 10);
  const latestSeedDay = today.startsWith(`${month}-`)
    ? Number(today.slice(-2))
    : daysInMonth;
  const seeded = [
    { day: Math.max(1, latestSeedDay - 8), status: 'approved', sourceEventCount: 42, segmentCount: 3, needsRefresh: false },
    { day: Math.max(1, latestSeedDay - 5), status: 'approved', sourceEventCount: 31, segmentCount: 2, needsRefresh: true },
    { day: Math.max(1, latestSeedDay - 3), status: 'draft', sourceEventCount: 27, segmentCount: 3, needsRefresh: false },
    { day: Math.max(1, latestSeedDay - 1), status: 'none', sourceEventCount: 18, segmentCount: 0, needsRefresh: false },
  ];
  const byDate = new Map(seeded.map((item) => [
    `${month}-${String(item.day).padStart(2, '0')}`,
    item,
  ]));
  for (const [date, status] of statuses) {
    if (!date.startsWith(`${month}-`)) continue;
    const current = byDate.get(date);
    byDate.set(date, {
      day: Number(date.slice(-2)),
      status,
      sourceEventCount: current?.sourceEventCount ?? (date === today ? 71 : 24),
      segmentCount: current?.segmentCount ?? (date === today ? 3 : 2),
      needsRefresh: false,
    });
  }
  const days = [...byDate.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([date, item]) => {
    const organized = (item.status === 'approved' || item.status === 'draft') && !item.needsRefresh;
    return {
      date,
      status: item.status,
      organized,
      modelOrganized: organized,
      needsRefresh: item.needsRefresh,
      sourceEventCount: item.sourceEventCount,
      timelineId: item.status === 'none' ? '' : `timeline:${date}`,
      timelineEventCount: item.needsRefresh ? Math.max(0, item.sourceEventCount - 6) : item.sourceEventCount,
      segmentCount: item.segmentCount,
      updatedAtMs: localPreviewDayStart(date) + 18 * 3_600_000,
    };
  });
  return {
    schemaVersion: 'rag-ime.activity-timeline-calendar.v1',
    ok: true,
    project: 'wisdom-weasel-rag-ime',
    timezone: 'Asia/Shanghai',
    month,
    summary: {
      sourceEventCount: days.reduce((sum, item) => sum + item.sourceEventCount, 0),
      activityDayCount: days.length,
      organizedDayCount: days.filter((item) => item.organized).length,
      approvedDayCount: days.filter((item) => item.status === 'approved' && item.organized).length,
      draftDayCount: days.filter((item) => item.status === 'draft' && item.organized).length,
      waitingDayCount: days.filter((item) => !item.organized).length,
      outdatedDayCount: days.filter((item) => item.needsRefresh).length,
    },
    days,
  };
}

function previewActivitySegment(input: {
  id: string;
  position: number;
  title: string;
  apps: string[];
  startMs: number;
  endMs: number;
  eventCount: number;
  summary: string;
  sourceKinds: string[];
  contextGroupIds: string[];
  redactedEventCount?: number;
}): Record<string, unknown> {
  const firstEventId = 10_001 + input.position * 100;
  const sourceEventIds = Array.from(
    { length: input.eventCount },
    (_, index) => firstEventId + index,
  );
  return {
    segmentId: `segment:${input.id}`,
    position: input.position,
    title: input.title,
    app: input.apps[0],
    apps: input.apps,
    sourceKinds: input.sourceKinds,
    contextGroupIds: input.contextGroupIds,
    startMs: Math.round(input.startMs),
    endMs: Math.round(input.endMs),
    activityKind: input.endMs - input.startMs >= 30 * 60_000 ? 'consolidated_activity' : 'ordinary_activity',
    spanSemantics: 'first_to_last_source_event',
    eventCount: input.eventCount,
    sourceEventIds,
    sourceEventHash: '24d18e6ea9f1d8dd36b957d3948dc6948c00c86173691104be5b3e24358da8bb',
    summary: input.summary,
    redactedEventCount: input.redactedEventCount ?? 0,
    evidenceRefs: sourceEventIds.map((eventId, index) => ({
      sourceType: 'input_event',
      sourceId: `event:${eventId}`,
      eventId,
      app: input.apps[index % input.apps.length],
      sourceKind: input.sourceKinds[index % input.sourceKinds.length],
      occurredAtMs: Math.round(
        input.startMs
        + ((input.endMs - input.startMs) * index) / Math.max(1, input.eventCount - 1),
      ),
      redacted: index < (input.redactedEventCount ?? 0),
    })),
    source: { type: 'daily_activity_timeline', id: `timeline:${new Date(input.startMs).toISOString().slice(0, 10)}` },
    ref: { type: 'timeline', id: `timeline:${new Date(input.startMs).toISOString().slice(0, 10)}` },
  };
}

export function previewTimelineDate(timelineId: string): string {
  const value = timelineId.startsWith('timeline:') ? timelineId.slice('timeline:'.length) : '';
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : new Date().toISOString().slice(0, 10);
}

function localPreviewDayStart(date: string): number {
  const [year, month, day] = date.split('-').map(Number);
  return new Date(year, month - 1, day).getTime();
}

function previewMemoryCatalogPage(kind: string): Record<string, unknown> {
  const common = { ok: true, nextCursor: '', limit: 50 };
  if (kind === 'apps') {
    return {
      ...common,
      rawTextVisible: false,
      items: [
        previewAppMemory('com.openai.codex', 'Codex', 3_501, 89, 8, Date.now() - 42_000),
        previewAppMemory('com.mitchellh.ghostty', 'Ghostty', 67, 10, 1, Date.now() - 320_000),
        previewAppMemory('com.microsoft.VSCode', 'VS Code', 26, 5, 1, Date.now() - 840_000),
        previewAppMemory('com.microsoft.edgemac', 'Edge', 8, 7, 0, Date.now() - 1_500_000),
      ],
    };
  }
  if (kind === 'tags') {
    return {
      ...common,
      items: [
        { id: 'agent-runtime', tag: '伙伴运行', description: '会话、工具与执行边界', item_count: 18, edge_count: 2, color_token: 'teal', status: 'active', source: 'agent' },
        { id: 'memory-quality', tag: '记忆质量', description: '去噪、合并与来源约束', item_count: 14, edge_count: 2, color_token: 'green', status: 'active', source: 'agent' },
        { id: 'input-boundary', tag: '输入封口', description: 'Backspace 编辑，Enter 后持久化', item_count: 9, edge_count: 2, color_token: 'orange', status: 'active', source: 'agent' },
      ],
    };
  }
  if (kind === 'groups') {
    return {
      ...common,
      items: [
        { id: 'group:input-method', title: '输入法', note: '输入质量、候选与上下文注入', tags: ['输入封口', '记忆质量'], event_count: 34, color_token: 'teal', status: 'active', source: 'agent' },
        { id: 'group:agent', title: '伙伴运行资料', note: '会话、工具和长期记忆', tags: ['伙伴运行', '记忆质量'], event_count: 27, color_token: 'blue', status: 'active', source: 'agent' },
      ],
    };
  }
  if (kind === 'atoms') {
    return {
      ...common,
      items: [
        {
          id: 'atom:input-boundary',
          title: '输入段封口规则',
          text: 'Backspace 修改当前缓冲区，Enter 或切换 App 后才形成完整输入段。',
          status: 'active',
          source: { type: 'memory_atom', id: 'atom:input-boundary' },
          ref: { type: 'atom', id: 'atom:input-boundary' },
          evidenceRefs: [{ kind: 'event', referenceId: 'event:10003', title: '输入框最终文本采集记录' }],
          tags: ['输入封口', '记忆质量'],
          updatedAtMs: Date.now() - 120_000,
        },
        {
          id: 'atom:timeline-governance',
          title: '时间线联想边界',
          text: '活动时间线是经审批的活动衍生物，可用于对话启动时的上下文和工具检索，但不能单独证明长期事实。',
          status: 'active',
          source: { type: 'memory_atom', id: 'atom:timeline-governance' },
          ref: { type: 'atom', id: 'atom:timeline-governance' },
          evidenceRefs: [{ kind: 'evidence', referenceId: 'evidence:preview-compaction', title: '伙伴对话整理依据' }],
          tags: ['时间线', '记忆治理'],
          updatedAtMs: Date.now() - 240_000,
        },
      ],
    };
  }
  if (kind === 'phrases') {
    return { ...common, items: [{ id: 'phrase:memory-curator', text: '记忆整理 Skill', status: 'approved', source: 'agent', updatedAtMs: Date.now() - 360_000 }] };
  }
  if (kind === 'negative') {
    return { ...common, items: [{ id: 'negative:rime-fragment', reason: '未封口的 Rime 单词碎片不得注入上下文', active: true, status: 'active', source: 'input_quality', updatedAtMs: Date.now() - 480_000 }] };
  }
  return {
    ...common,
    items: [
      {
        id: 'book:input-memory',
        type: 'topic',
        title: '输入法记忆与上下文',
        summary: '完整输入段、App 来源、当前事实与闪电联想边界。',
        status: 'active',
        source: { type: 'memory_book', id: 'book:input-memory' },
        ref: { type: 'book', id: 'book:input-memory' },
        evidenceRefs: [{ kind: 'atom', referenceId: 'atom:input-boundary', title: '输入段封口规则' }],
        tags: ['输入封口', '记忆质量'],
        updatedAtMs: Date.now() - 90_000,
      },
      {
        id: 'book:agent-runtime',
        type: 'topic',
        title: '伙伴运行',
        summary: '伙伴记忆、对话启动参考、记忆工具与受控写入。',
        status: 'active',
        source: { type: 'memory_book', id: 'book:agent-runtime' },
        ref: { type: 'book', id: 'book:agent-runtime' },
        evidenceRefs: [{ kind: 'evidence', referenceId: 'evidence:preview-compaction', title: '伙伴对话整理依据' }],
        tags: ['伙伴运行'],
        updatedAtMs: Date.now() - 210_000,
      },
    ],
  };
}

function previewAppMemory(
  id: string,
  title: string,
  eventCount: number,
  atomCount: number,
  bookCount: number,
  latestAtMs: number,
): Record<string, unknown> {
  return {
    id,
    title,
    detail: `${eventCount} 段完整输入 · ${atomCount} 个记忆原子 · ${bookCount} 本主题书`,
    source: 'input_app',
    status: 'active',
    type: 'app',
    bundleId: id,
    eventCount,
    finalizedSegmentCount: Math.min(eventCount, Math.max(0, Math.round(eventCount * 0.72))),
    contextGroupCount: Math.max(1, Math.round(eventCount / 18)),
    atomCount,
    bookCount,
    latestAtMs,
  };
}

export function previewMemoryGraph(plane: 'groups' | 'tags', query = ''): Record<string, unknown> {
  const runtime = previewMemoryGraphNode('tag:agent-runtime', 'tag', '伙伴运行', '会话、工具与执行边界', 18, 'teal');
  const quality = previewMemoryGraphNode('tag:memory-quality', 'tag', '记忆质量', '去噪、合并与来源约束', 14, 'green');
  const boundary = previewMemoryGraphNode('tag:input-boundary', 'tag', '输入封口', 'Backspace 编辑，Enter 后持久化', 9, 'orange');
  const tags = [runtime, quality, boundary];
  if (plane === 'tags') {
    return previewMemoryGraphEnvelope(plane, tags, [
      previewMemoryGraphEdge('tag-edge:runtime-quality', 'tagRelation', 'tag:agent-runtime', 'tag:memory-quality', 'related_to', 0.92, 8),
      previewMemoryGraphEdge('tag-edge:quality-boundary', 'tagRelation', 'tag:memory-quality', 'tag:input-boundary', 'depends_on', 0.88, 6),
      previewMemoryGraphEdge('tag-edge:boundary-runtime', 'tagRelation', 'tag:input-boundary', 'tag:agent-runtime', 'feeds', 0.74, 4),
    ], query);
  }
  const inputGroup = previewMemoryGraphNode('group:input-method', 'group', '输入法', '输入质量、候选与上下文注入', 34, 'teal');
  const agentGroup = previewMemoryGraphNode('group:agent', 'group', '伙伴运行资料', '会话、工具和长期记忆', 27, 'blue');
  const book = previewMemoryGraphNode('book:input-memory', 'book', '输入法记忆与上下文', '完整输入段、App 来源和闪电联想边界', 12, 'green');
  return previewMemoryGraphEnvelope(plane, [inputGroup, agentGroup, book, ...tags], [
    previewMemoryGraphEdge('member:input-boundary', 'groupMember', 'group:input-method', 'tag:input-boundary', 'contains', 1, 1),
    previewMemoryGraphEdge('member:input-quality', 'groupMember', 'group:input-method', 'tag:memory-quality', 'contains', 1, 1),
    previewMemoryGraphEdge('member:input-book', 'groupMember', 'group:input-method', 'book:input-memory', 'contains', 0.9, 1),
    previewMemoryGraphEdge('member:agent-runtime', 'groupMember', 'group:agent', 'tag:agent-runtime', 'contains', 1, 1),
    previewMemoryGraphEdge('member:agent-quality', 'groupMember', 'group:agent', 'tag:memory-quality', 'contains', 0.85, 1),
  ], query);
}

function previewMemoryGraphEnvelope(
  plane: 'groups' | 'tags',
  nodes: Record<string, unknown>[],
  edges: Record<string, unknown>[],
  query: string,
): Record<string, unknown> {
  const needle = query.trim().toLocaleLowerCase();
  if (needle) {
    const matches = new Set(nodes.filter(node => `${node.label} ${node.description}`.toLocaleLowerCase().includes(needle)).map(node => node.id));
    const visible = new Set(matches);
    for (const edge of edges) {
      if (matches.has(edge.sourceId) || matches.has(edge.targetId)) {
        visible.add(edge.sourceId);
        visible.add(edge.targetId);
      }
    }
    nodes = nodes.filter(node => visible.has(node.id));
    edges = edges.filter(edge => visible.has(edge.sourceId) && visible.has(edge.targetId));
  }
  return {
    schemaVersion: 'rag-ime.memory-graph.v1',
    ok: true,
    settingsRevision: 'settings:preview',
    runtimeRevision: 7,
    graphRevision: `sha256:${'a'.repeat(64)}`,
    plane,
    project: 'wisdom-weasel-rag-ime',
    filters: { status: 'active', query: query.trim(), focusId: '', minWeight: 0 },
    nodes,
    edges,
    truncated: { nodes: false, edges: false },
    limits: { nodeLimit: 48, edgeLimit: 160, depth: 1 },
  };
}

function previewMemoryGraphNode(
  id: string,
  kind: 'tag' | 'group' | 'book',
  label: string,
  description: string,
  memberCount: number,
  color: string,
): Record<string, unknown> {
  return {
    id,
    entityId: id.slice(id.indexOf(':') + 1),
    kind,
    label,
    description,
    color,
    status: 'active',
    source: 'preview',
    project: 'wisdom-weasel-rag-ime',
    qualityScore: 1,
    memberCount,
    edgeCount: 2,
    updatedAtMs: Date.now() - 60_000,
  };
}

function previewMemoryGraphEdge(
  id: string,
  kind: 'tagRelation' | 'groupMember',
  sourceId: string,
  targetId: string,
  relation: string,
  weight: number,
  evidenceCount: number,
): Record<string, unknown> {
  return {
    id,
    kind,
    sourceId,
    targetId,
    sourceKind: sourceId.split(':', 1)[0],
    targetKind: targetId.split(':', 1)[0],
    relation,
    weight,
    directionBias: 0,
    evidenceCount,
    source: 'preview',
    updatedAtMs: Date.now() - 60_000,
  };
}

export function previewMemoryEntity(kindValue: string, entityId: string): Record<string, unknown> {
  const kind = kindValue === 'group' || kindValue === 'book' ? kindValue : 'tag';
  const catalog = {
    'agent-runtime': previewMemoryGraphNode('tag:agent-runtime', 'tag', '伙伴运行', '会话、工具与执行边界', 18, 'teal'),
    'memory-quality': previewMemoryGraphNode('tag:memory-quality', 'tag', '记忆质量', '去噪、合并与来源约束', 14, 'green'),
    'input-boundary': previewMemoryGraphNode('tag:input-boundary', 'tag', '输入封口', 'Backspace 编辑，Enter 后持久化', 9, 'orange'),
    'input-method': previewMemoryGraphNode('group:input-method', 'group', '输入法', '输入质量、候选与上下文注入', 34, 'teal'),
    agent: previewMemoryGraphNode('group:agent', 'group', '伙伴运行资料', '会话、工具和长期记忆', 27, 'blue'),
    'input-memory': previewMemoryGraphNode('book:input-memory', 'book', '输入法记忆与上下文', '完整输入段、App 来源和闪电联想边界', 12, 'green'),
  } as const;
  const fallback = previewMemoryGraphNode(
    entityId === previewGovernanceTopic.id ? entityId : `${kind}:${entityId || 'preview'}`,
    kind,
    entityId === previewGovernanceTopic.id ? previewGovernanceTopic.title : entityId || '预览记忆',
    entityId === previewGovernanceTopic.id ? previewGovernanceTopic.summary : '本地记忆关系',
    0,
    'gray',
  );
  const entity = catalog[entityId as keyof typeof catalog] ?? fallback;
  const related = kind === 'tag'
    ? catalog['memory-quality']
    : kind === 'book'
      ? catalog['input-method']
      : catalog['agent-runtime'];
  const connectionEdge = kind === 'tag'
    ? previewMemoryGraphEdge(`entity-edge:${entityId}`, 'tagRelation', String(entity.id), String(related.id), 'related_to', 0.88, 6)
    : kind === 'book'
      ? previewMemoryGraphEdge(`entity-edge:${entityId}`, 'groupMember', String(related.id), String(entity.id), 'contains', 0.9, 1)
      : previewMemoryGraphEdge(`entity-edge:${entityId}`, 'groupMember', String(entity.id), String(related.id), 'contains', 0.9, 1);
  const members = kind === 'group'
    ? [catalog['input-boundary'], catalog['memory-quality']].map((node) => ({
        node,
        edge: previewMemoryGraphEdge(`entity-member:${String(node.id)}`, 'groupMember', String(entity.id), String(node.id), 'contains', 1, 1),
      }))
    : [];
  return {
    schemaVersion: 'rag-ime.memory-entity.v1',
    ok: true,
    settingsRevision: 'settings:preview',
    runtimeRevision: 7,
    kind,
    entityId: entityId || 'preview',
    entityRevision: `sha256:${'b'.repeat(64)}`,
    project: 'wisdom-weasel-rag-ime',
    entity,
    attributes: {
      type: kind === 'group' ? 'semantic' : kind === 'book' ? 'topic' : 'concept',
      aliases: kind === 'tag' && entityId === 'memory-quality' ? ['记忆治理'] : [],
      tags: kind === 'book' ? ['输入封口', '记忆质量'] : [],
    },
    connections: { items: [{ node: related, edge: connectionEdge }], nextCursor: '', limit: 40, hasMore: false },
    members: { items: members, nextCursor: '', limit: 40, hasMore: false },
    limits: { connectionsLimit: 40, membersLimit: 40 },
  };
}

export function previewMemoryCurationStatus(status: string): Record<string, unknown> {
  const today = new Date().toISOString().slice(0, 10);
  const dateBefore = (days: number) => new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
  const backlogDays = [
    { date: dateBefore(6), pendingSourceCount: 38, needsReviewSourceCount: 0, applications: [{ name: 'Codex', count: 21 }, { name: 'Chrome', count: 11 }, { name: '系统输入框', count: 6 }], channels: [] },
    { date: dateBefore(5), pendingSourceCount: 44, needsReviewSourceCount: 0, applications: [{ name: 'Codex', count: 27 }, { name: 'Chrome', count: 17 }], channels: [] },
    { date: dateBefore(4), pendingSourceCount: 51, needsReviewSourceCount: 0, applications: [{ name: 'Codex', count: 31 }, { name: 'Terminal', count: 12 }, { name: 'Chrome', count: 8 }], channels: [] },
    { date: dateBefore(3), pendingSourceCount: 63, needsReviewSourceCount: 0, applications: [{ name: 'Codex', count: 42 }, { name: 'Chrome', count: 21 }], channels: [] },
    { date: dateBefore(2), pendingSourceCount: 48, needsReviewSourceCount: 0, applications: [{ name: 'Codex', count: 26 }, { name: 'Chrome', count: 15 }, { name: 'Terminal', count: 7 }], channels: [] },
    { date: dateBefore(1), pendingSourceCount: 42, needsReviewSourceCount: 0, applications: [{ name: 'Codex', count: 25 }, { name: 'Chrome', count: 17 }], channels: [] },
    { date: today, pendingSourceCount: 24, needsReviewSourceCount: 0, applications: [{ name: 'Codex', count: 15 }, { name: 'Chrome', count: 9 }], channels: [] },
  ];
  return {
    ok: true,
    policy: 'auto_governed',
    autoApply: false,
    scheduledDraftOnly: true,
    due: true,
    compileState: { undraftedEventCount: 310 },
    ownerCuration: {
      pendingSourceCount: 310,
      needsReviewSourceCount: 0,
      backlog: {
        schemaVersion: 'rag-ime.owner-memory-curation-backlog.v1',
        pendingSourceCount: 310,
        pendingDayCount: backlogDays.length,
        oldestPendingAtMs: Date.now() - 6 * 86_400_000,
        newestPendingAtMs: Date.now(),
        coveredThroughAtMs: Date.now() - 7 * 86_400_000,
        coveredThroughDate: dateBefore(7),
        targetDate: today,
        caughtUpThroughToday: false,
        applications: [
          { name: 'Codex', count: 187, lastSourceAtMs: Date.now() },
          { name: 'Chrome', count: 98, lastSourceAtMs: Date.now() - 30_000 },
          { name: 'Terminal', count: 19, lastSourceAtMs: Date.now() - 120_000 },
          { name: '系统输入框', count: 6, lastSourceAtMs: Date.now() - 180_000 },
        ],
        channels: [],
        days: backlogDays,
        truncated: false,
      },
      scopes: [{
        status: 'backoff',
        pendingSourceCount: 310,
        totalSourceCount: 339,
        consecutiveFailures: 2,
        lastSourceCursor: { createdAtMs: Date.now() - 7 * 86_400_000 },
        lastError: 'managed memory model request failed: request failed',
      }],
    },
    modelCuration: {
      stateCounts: { running: 0, resumable: 2, completed: 9, cancelled: 1 },
      runs: [{ state: 'resumable', lastError: 'request failed' }],
    },
    bookProjection: { inSync: true, unbookedAtomCount: 0 },
    projection: { freshness: { fresh: true, retrievalDocuments: 76, vectorCoverage: .49, providerFingerprint: 'sha256:preview' } },
    pendingDraftCount: status === 'draft' ? 1 : 0,
    runs: [{ runId: 'memory_book_preview', status, diffCount: 3, createdAtMs: Date.now() - 180_000 }],
  };
}

export function previewMemoryCurationRun(
  selections: Map<number, boolean>,
  status: string,
): Record<string, unknown> {
  const changes = [
    ['upsert_memory_atom', '更新记忆原子', '输入封口边界', 'Backspace 修改缓冲区，Enter 或切换 App 后才写入完整段落。', 18],
    ['merge_semantic_tag', '合并标签', '合并输入法同义标签', '保留清晰名称和别名，移除传输层标签。', 11],
    ['supersede_memory', '归档噪声', '隔离旧 Rime 碎片', '未封口单词和短片段不再用于伙伴上下文或长期记忆。', 4_887],
  ].map(([operation, operationLabel, title, detail, sourceCount], index) => {
    const diffId = index + 1;
    const selected = selections.get(diffId) === true;
    return {
      diffId,
      operation,
      operationLabel,
      status: status === 'draft' ? (selected ? 'approved' : 'rejected') : status === 'rolled_back' ? 'rolled_back' : 'applied',
      selected,
      title,
      detail,
      sourceCount,
    };
  });
  return {
    ok: true,
    stale: false,
    canApply: status === 'draft' && changes.some((change) => change.selected),
    canRollback: status === 'applied',
    run: {
      runId: 'memory_book_preview',
      status,
      createdAtMs: Date.now() - 180_000,
      diffCount: changes.length,
      pendingDiffCount: status === 'draft' ? changes.filter((change) => change.selected).length : 0,
      changes,
    },
  };
}

export function previewMemoryApplyPreview(): Record<string, unknown> {
  return {
    schemaVersion: 'rag-ime.management-work-preview.v1',
    ok: true,
    previewToken: 'preview-memory-curation',
    pathId: 'knowledge.database.apply',
    payloadSha256: `sha256:${'d'.repeat(64)}`,
    expectedRevision: { runtimeRevision: 7, subjectRevision: 'memory_book_preview' },
    expiresAtMs: Date.now() + 60_000,
    requiredConfirm: 'apply',
    summary: {
      title: '应用所选记忆更新',
      items: ['只写入已勾选的整理建议', '重建本地记忆检索索引'],
      risk: 'R2',
    },
  };
}

export function previewMemoryWorkReceipt(pathId: string, rollbackAvailable: boolean): Record<string, unknown> {
  return {
    schemaVersion: 'rag-ime.management-work-receipt.v1',
    ok: true,
    receiptId: pathId.endsWith('rollback') ? 'receipt-memory-curation-rollback' : 'receipt-memory-curation',
    pathId,
    payloadSha256: `sha256:${'d'.repeat(64)}`,
    appliedAtMs: Date.now(),
    auditId: 17,
    rollbackAvailable,
    rollbackToken: rollbackAvailable ? 'rollback-memory-curation' : '',
    rollbackAuthority: { runId: 'memory_book_preview' },
    restartComponents: [],
    result: { status: pathId.endsWith('rollback') ? 'rolled_back' : 'applied' },
  };
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}
