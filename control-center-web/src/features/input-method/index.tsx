import * as RadioGroup from '@radix-ui/react-radio-group';
import {
  BookMarked,
  Bug,
  Check,
  ChevronDown,
  Keyboard,
  Network,
  RefreshCw,
  Shield,
  Sparkles,
  TextCursorInput,
  type LucideIcon,
} from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { Button, EmptyState, Field, Input, Select, Switch } from '@/components/primitives';
import {
  inputSettingsMutationPathIds,
  useInputMethodQueries,
  type LexiconOrganizationStatus,
} from './api';
import {
  applyModeLabel,
  completionLaneFacts,
  componentStatus,
  contextLaneFacts,
  formatSetting,
  inferInputMode,
  inputFieldFallback,
  inputModeChanges,
  inputOptionLabel,
  inputSourceDetail,
  inputSourceMessage,
  modeFactChips,
  modeSettingLabel,
  modelConfigValue,
  numericDraftValue,
  presetInputModes,
  predictionLiveTraceSummaries,
  type PredictionTraceFrameSummary,
  profileLabel,
  publicInputText,
  readinessLabel,
  recallLaneFacts,
  sectionLabel,
  suggestionPanel,
  validInputSettingValue,
  type DraftValue,
  type InputMode,
  type StatusTone,
  type SuggestionPanel,
} from './input-method-presentation';
import { LexiconWorkflow } from './lexicon-workflow';
import { DiagnosticsRuntimeWorkflow } from '@/features/diagnostics/runtime-actions';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import {
  ManagementMutationWorkflow,
  parseManagementWorkPreview,
  parseManagementWorkReceipt,
} from '@/features/overview/management-mutation';
import {
  DataTable,
  InlineNotice,
  ManagementPage,
  ManagementSection,
  QueryState,
  StatusBadge,
  arrayRecords,
  asRecord,
  booleanValue,
  publicErrorText,
  stringValue,
  valueAt,
} from '@/features/overview/management-ui';
import './input-method.css';


/* 卡片描述只补充一句话；每张卡片的事实签名由 modeFactChips 从真实写入
 * 逐键派生，宣传语没有独立生存空间。 */
const inputModes: readonly {
  value: InputMode;
  label: string;
  icon: LucideIcon;
  description: string;
}[] = [
  { value: '安全模式', label: '安全', icon: Shield, description: '全部关闭，输入交回系统输入法。' },
  { value: '标准模式', label: '标准', icon: Sparkles, description: '本机联想，记忆按紧凑方式召回。' },
  { value: '记忆增强', label: '记忆增强', icon: BookMarked, description: '召回更详尽、可展开时间线，其余与标准一致。' },
  { value: '调试模式', label: '调试', icon: Bug, description: '露出实时诊断与候选解释，用于排查。' },
];

const commonInputSettingKeys = new Set([
  'interaction.composition.showPrediction',
  'interaction.composition.showOnlyRime',
  'interaction.postCommit.showPendingStatus',
  'interaction.postCommit.enabled',
  'interaction.postCommit.idleTriggerMs',
  'interaction.postCommit.minDeltaChars',
  'interaction.postCommit.maxCallsPer10s',
  'interaction.postCommit.cooldownMs',
  'interaction.postCommit.panelTtlMs',
  'interaction.postCommit.modelBudgetMs',
  'interaction.postCommit.tabAction',
  'interaction.postCommit.optionNumber',
  'display.maxPostCommitCandidates',
  'display.panelStyle',
  'activeRag.defaultPlacement',
  'activeRag.latencyBudgetMs',
  'pinyin.fuzzyProfile',
  'lexiconOrganization.enabled',
  'lexiconOrganization.runsPerDay',
  'models.modelId',
  'models.hot',
  'models.path',
  'models.promptMode',
  'models.maxTokens',
  'models.temperature',
  'models.topP',
]);

export function InputMethodFeature() {
  const desktop = usePawOsDesktop();
  const openDiagnostics = () => openPawOsRoute(desktop, '/diagnostics');
  const queries = useInputMethodQueries('input');
  const source = asRecord(queries.source.data);
  const overview = asRecord(queries.overview.data);
  const components = asRecord(overview.components);
  const effectiveRuntimeConfig = asRecord(overview.runtimeConfig);
  const effectiveComposition = asRecord(effectiveRuntimeConfig.composition);
  const effectivePostCommit = asRecord(effectiveRuntimeConfig.postCommit);
  const settingsPayload = asRecord(queries.settings.data);
  const settings = asRecord(settingsPayload.settings);
  const runtimeConfig = asRecord(settingsPayload.runtimeConfig);
  const modelsStatus = asRecord(queries.models.data);
  const predictorStatus = asRecord(modelsStatus.predictor);
  const modelCapabilityProbe = asRecord(
    predictorStatus.capabilityProbe ?? modelsStatus.capabilityProbe,
  );
  const activeModelConfig = asRecord(modelsStatus.activeConfig);
  const availableModels = arrayRecords(modelsStatus.availableModels);
  const availableModelIds = availableModels
    .map((item) => modelConfigValue(item.id))
    .filter((item) => item !== '由本机注册表决定');
  const modelHealthAgreement = asRecord(modelsStatus.healthAgreement);
  const modelConfigurationPending = booleanValue(modelsStatus.configurationPending);
  const modelHealthReady = booleanValue(modelHealthAgreement.ok);
  const rawRuntimeRevision = settingsPayload.runtimeRevision ?? runtimeConfig.runtimeRevision;
  const runtimeRevision = typeof rawRuntimeRevision === 'number'
    && Number.isInteger(rawRuntimeRevision)
    && rawRuntimeRevision >= 0
    ? rawRuntimeRevision
    : null;
  const sections = arrayRecords(asRecord(queries.schema.data).sections).filter((section) =>
    ['interaction', 'display', 'activeRag', 'pinyin', 'models', 'lexiconOrganization'].includes(stringValue(section.id)),
  );
  const settingsGroups = sections
    .map((section) => ({
      id: stringValue(section.id),
      fields: arrayRecords(section.fields).filter((field) => commonInputSettingKeys.has(stringValue(field.key))),
    }))
    .filter((section) => section.fields.length > 0);
  const fieldCount = settingsGroups.reduce((count, section) => count + section.fields.length, 0);
  const [changes, setChanges] = useState<Record<string, DraftValue>>({});
  const updateSettingChange = (key: string, value: DraftValue) => {
    setChanges((current) => {
      const next = { ...current, [key]: value };
      if (key !== 'models.modelId' || typeof value !== 'string') return next;
      const selected = value
        ? availableModels.find((item) => stringValue(item.id) === value)
        : availableModels.find((item) => booleanValue(item.active));
      if (!selected) return next;
      return {
        ...next,
        'models.hot': stringValue(selected.profileId),
        'models.path': stringValue(selected.path),
        'models.promptMode': stringValue(selected.promptMode),
        'models.maxTokens': numericDraftValue(selected.maxTokens, 8),
        'models.temperature': numericDraftValue(selected.temperature, 0.15),
        'models.topP': numericDraftValue(selected.topP, 0.85),
      };
    });
  };
  const fields = settingsGroups.flatMap((section) => section.fields);
  const pendingChanges = useMemo(() => Object.fromEntries(
    Object.entries(changes).filter(([key, next]) => (
      commonInputSettingKeys.has(key) && !Object.is(valueAt(settings, key), next)
    )),
  ) as Record<string, DraftValue>, [changes, settings]);
  const diffRows = useMemo(() => Object.entries(pendingChanges).map(([key, next]) => {
    const field = fields.find((item) => stringValue(item.key) === key) ?? {};
    const applyMode = stringValue(field.applyMode, 'live');
    return {
      id: key,
      key: inputFieldFallback(key),
      before: formatSetting(valueAt(settings, key), key),
      after: formatSetting(next, key),
      applyMode: applyModeLabel(applyMode),
      requiresReload: applyMode !== 'live',
    };
  }), [fields, pendingChanges, settings]);
  const hasInvalidChanges = Object.entries(pendingChanges).some(([key, value]) => {
    const field = fields.find((item) => stringValue(item.key) === key) ?? {};
    return !validInputSettingValue(field, value);
  });
  const settingsWriteAvailability = queries.settingsMutationAvailability();
  const inferredMode = inferInputMode(settings);
  const [modeDraft, setModeDraft] = useState<InputMode | ''>('');
  /* 视口台账开关：差异清单可折叠，生成管线默认收起。关键动作（选择
   * 模式、保存）始终留在首屏，细节按需展开而不是把页面推成长卷。 */
  const [modeLedgerOpen, setModeLedgerOpen] = useState(true);
  const [pipelineOpen, setPipelineOpen] = useState(false);
  const [settingsLedgerOpen, setSettingsLedgerOpen] = useState(true);
  const displayedMode = modeDraft || inferredMode;
  const pendingModeChanges = useMemo(() => modeDraft
    ? Object.fromEntries(Object.entries(inputModeChanges[modeDraft]).filter(([key, value]) => (
      !Object.is(valueAt(settings, key), value)
    ))) as Record<string, DraftValue>
    : {}, [modeDraft, settings]);
  const modeDiffItems = Object.entries(pendingModeChanges).map(([key, value]) => (
    `${modeSettingLabel(key)}：${formatSetting(valueAt(settings, key), key)} → ${formatSetting(value, key)}`
  ));
  const reportedProfile = stringValue(overview.profile);
  /* 运行端报告的模式与设置推断的模式是两个真相来源；都落在已知预设且
   * 不一致时如实提示，不把“已保存”偷换成“已生效”。 */
  const runtimeProfileMode = (presetInputModes as readonly string[]).includes(reportedProfile)
    ? reportedProfile as InputMode
    : '';
  const runtimeModeMismatch = Boolean(
    inferredMode && runtimeProfileMode && runtimeProfileMode !== inferredMode,
  );
  const nativeExternalActions = Boolean(
    queries.capabilities.data?.native.approvedExternalActions
      && queries.transport.runApprovedExternalAction,
  );
  const sourceError = queries.source.error as Error | null;
  const overviewError = queries.overview.error as Error | null;
  const settingsError = [queries.settings.error, queries.schema.error].find(Boolean) as Error | null;
  const settingsPending = queries.settings.isPending || queries.schema.isPending;
  const hasRetainedSettings = Boolean(queries.settings.data && queries.schema.data);
  const isRefreshing = [
    queries.source,
    queries.overview,
    queries.models,
    queries.settings,
    queries.schema,
    queries.capabilities,
  ].some((query) => query.isFetching);

  const refresh = () => {
    void Promise.all([
      queries.source.refetch(),
      queries.overview.refetch(),
      queries.models.refetch(),
      queries.settings.refetch(),
      queries.schema.refetch(),
      queries.capabilities.refetch(),
    ]);
  };

  const panel = suggestionPanel(settings);
  const savedCompositionPrediction = booleanValue(valueAt(settings, 'interaction.composition.showPrediction'));
  const savedPostCommit = booleanValue(valueAt(settings, 'interaction.postCommit.enabled'));
  const effectiveCompositionPrediction = optionalBoolean(effectiveComposition.aiEnabled);
  const effectiveShowOnlyRime = optionalBoolean(effectiveComposition.showOnlyRime);
  const effectivePostCommitEnabled = optionalBoolean(effectivePostCommit.enabled);
  const runtimeSafetyClamps = stringArray(effectiveRuntimeConfig.safetyClamps);
  const activeModelId = modelConfigValue(activeModelConfig.modelId);
  const predictorProviderName = stringValue(
    predictorStatus.providerName,
    stringValue(modelsStatus.providerName, '未报告服务'),
  );
  const modelLoaded = optionalBoolean(modelCapabilityProbe.modelLoaded);
  const mlxMatchesRegistry = optionalBoolean(modelHealthAgreement.mlxMatchesRegistry);
  const completionLane = completionLaneFacts(
    settings,
    activeModelId === '由本机注册表决定' ? '' : activeModelId,
  );
  const recallLane = recallLaneFacts(settings);
  const contextLane = contextLaneFacts(settings);
  const contextLive = componentStatus(
    asRecord(components.foregroundContext),
    '上下文获取',
    TextCursorInput,
    queries.overview.isPending,
    overviewError,
  );
  const predictorLive = componentStatus(
    asRecord(components.predictor),
    '本机联想',
    Sparkles,
    queries.overview.isPending,
    overviewError,
  );
  const modelStatusUnavailable = booleanValue(modelsStatus.statusUnavailable);
  const modelHealthBadge: { label: string; tone: StatusTone } | null =
    !completionLane.enabled || queries.models.isPending || modelStatusUnavailable
      ? null
      : modelHealthReady && !modelConfigurationPending
        ? { label: '配置已生效', tone: 'success' }
        : { label: '等待应用', tone: 'warning' };
  const modelActionNeeded = completionLane.enabled
    && !queries.models.isPending
    && !modelStatusUnavailable
    && (modelConfigurationPending || !modelHealthReady);

  return (
    <ManagementPage
      actions={(
        <Button
          leadingIcon={<RefreshCw size={15} />}
          loading={isRefreshing}
          onClick={refresh}
          size="small"
        >
          刷新
        </Button>
      )}
      description="上屏后的智能候选在这里配置。拼音解码与选字仍由系统输入法负责。"
      eyebrow="输入体验"
      routeId="input"
      title="输入法"
    >
      <ManagementSection
        description="每张卡片列出它真实写入的设置；核对差异清单后保存，其余键不动。"
        title="使用方式"
        trailing={(
          <span>
            运行报告：{' '}
            <StatusBadge
              label={queries.overview.isPending ? '正在读取模式' : overviewError ? '读取失败' : profileLabel(reportedProfile)}
              tone={overviewError ? 'danger' : 'neutral'}
            />
          </span>
        )}
      >
        {runtimeModeMismatch ? (
          <InlineNotice title="运行端与已保存设置不一致" tone="warning">
            运行端仍报告「{runtimeProfileMode}」，已保存设置对应「{inferredMode}」。刷新核对，必要时重新载入输入法设置。
          </InlineNotice>
        ) : null}
        <div className="input-mode-console">
          <RadioGroup.Root
            aria-label="使用方式"
            className="input-mode-deck"
            disabled={settingsWriteAvailability.state !== 'available'}
            onValueChange={(next) => setModeDraft(next as InputMode)}
            value={displayedMode as InputMode}
          >
            {inputModes.map((mode) => (
              <RadioGroup.Item
                aria-label={mode.label}
                className="input-mode-card"
                data-current={mode.value === inferredMode || undefined}
                key={mode.value}
                value={mode.value}
              >
                <span className="input-mode-card__head">
                  <span aria-hidden="true" className="input-mode-card__glyph"><mode.icon size={15} /></span>
                  <strong>{mode.label}</strong>
                  {mode.value === inferredMode ? <span className="input-mode-card__current">当前</span> : null}
                  <RadioGroup.Indicator className="input-mode-card__check">
                    <Check aria-hidden="true" size={13} />
                  </RadioGroup.Indicator>
                </span>
                <small className="input-mode-card__note">{mode.description}</small>
                <span className="input-mode-card__facts">
                  {modeFactChips(mode.value).map((chip) => (
                    <span
                      data-highlight={chip.highlight || undefined}
                      data-off={chip.off || undefined}
                      key={chip.key}
                    >
                      {chip.label}
                    </span>
                  ))}
                </span>
              </RadioGroup.Item>
            ))}
          </RadioGroup.Root>
          <div className="input-mode-decision">
            <div aria-live="polite" className="input-mode-delta">
              {settingsPending ? (
                <span>正在读取当前设置，暂不判断使用方式。</span>
              ) : settingsError ? (
                <span>无法读取当前设置，请刷新后重试。</span>
              ) : modeDraft && modeDiffItems.length ? (
                <>
                  <div className="input-mode-delta__head">
                    <strong>改用{modeDraft}将改动 {modeDiffItems.length} 项</strong>
                    <button
                      aria-controls="input-mode-ledger-panel"
                      aria-expanded={modeLedgerOpen}
                      className="input-ledger-toggle"
                      id="input-mode-ledger-trigger"
                      onClick={() => setModeLedgerOpen((current) => !current)}
                      type="button"
                    >
                      差异清单
                      <ChevronDown aria-hidden="true" size={14} />
                    </button>
                  </div>
                  <InputSettingsDisclosure
                    id="input-mode-ledger-panel"
                    labelledBy="input-mode-ledger-trigger"
                    open={modeLedgerOpen}
                  >
                    <ul>
                      {modeDiffItems.map((item) => <li key={item}>{item}</li>)}
                    </ul>
                  </InputSettingsDisclosure>
                </>
              ) : modeDraft ? (
                <span>所选方式与当前设置一致，无需保存。</span>
              ) : inferredMode ? (
                <span>当前设置与「{inferredMode}」一致。</span>
              ) : (
                <span>当前设置不属于任何预设。</span>
              )}
            </div>
            <ManagementMutationWorkflow
              availability={queries.settingsMutationAvailability(
                settingsError || settingsPending || runtimeRevision === null
                  ? '当前设置状态尚未同步，刷新后才能继续。'
                    : !modeDraft
                    ? inferredMode
                      ? `正在使用${inferredMode}；选择其他方式后可保存。`
                      : '选择一种使用方式后可保存。'
                    : modeDiffItems.length === 0
                      ? '当前设置已符合所选模式。'
                      : '',
              )}
              description="只写入差异清单里的设置，其余不动。"
              draftKey={JSON.stringify({ mode: modeDraft, changes: pendingModeChanges, runtimeRevision })}
              mutationKey={['input-method', 'mutation', 'mode']}
              onApply={async (preview) => parseManagementWorkReceipt(
                await queries.requestSettingsMutation({
                  pathId: inputSettingsMutationPathIds.apply,
                  body: {
                    changes: preview.context.changes,
                    expectedRuntimeRevision: preview.expectedRuntimeRevision,
                    previewToken: preview.previewToken,
                    payloadSha256: preview.payloadSha256,
                    confirmText: preview.requiredConfirm,
                  },
                }),
                inputSettingsMutationPathIds.apply,
                preview.payloadSha256,
              )}
              onApplied={() => {
                // 保存成功后清空草稿：勾选状态回到“真实生效的模式”，
                // 回执与撤销入口仍由工作流自身保留。
                setModeDraft('');
                void Promise.all([queries.settings.refetch(), queries.overview.refetch()]);
              }}
              onPreview={async () => {
                if (settingsError || settingsPending || !modeDraft || runtimeRevision === null || modeDiffItems.length === 0) {
                  throw new Error('运行模式或当前设置版本已失效，请刷新后重试。');
                }
                const context = { mode: modeDraft, changes: { ...pendingModeChanges } };
                const parsed = parseManagementWorkPreview(
                  await queries.requestSettingsMutation({
                    pathId: inputSettingsMutationPathIds.preview,
                    body: { changes: context.changes, expectedRuntimeRevision: runtimeRevision },
                  }),
                  inputSettingsMutationPathIds.apply,
                  context,
                );
                return {
                  ...parsed,
                  summary: {
                    ...parsed.summary,
                    title: `使用${modeDraft}？`,
                    items: modeDiffItems,
                  },
                };
              }}
              onRollback={async (receipt, preview) => parseManagementWorkReceipt(
                await queries.requestSettingsMutation({
                  pathId: inputSettingsMutationPathIds.rollback,
                  body: {
                    receiptId: receipt.receiptId,
                    rollbackToken: receipt.rollbackToken,
                    payloadSha256: receipt.payloadSha256,
                    confirmText: 'rollback',
                  },
                }),
                inputSettingsMutationPathIds.rollback,
                preview.payloadSha256,
              )}
              onRolledBack={() => {
                setModeDraft('');
                void Promise.all([queries.settings.refetch(), queries.overview.refetch()]);
              }}
              risk={modeDraft === '调试模式' || modeDraft === '安全模式' ? 'R2' : 'R1'}
              title="保存使用方式"
            />
          </div>
        </div>
      </ManagementSection>

      <ManagementSection
        description="上下文、记忆、本机联想三步生成，与原生候选并排出现。"
        title="智能候选"
        trailing={(
          <StatusBadge
            label={settingsPending ? '正在读取' : settingsError ? '读取失败' : panel.enabled ? '已开启' : '已关闭'}
            tone={settingsError ? 'danger' : !settingsPending && panel.enabled ? 'info' : 'neutral'}
          />
        )}
      >
        <QueryState
          error={hasRetainedSettings ? null : settingsError}
          headingLevel={3}
          isPending={settingsPending}
          onRetry={() => void Promise.all([queries.settings.refetch(), queries.schema.refetch()])}
        >
          <div aria-label="输入预测运行事实" className="input-prediction-facts" role="list">
            <article aria-label="输入拼音时预测" className="input-prediction-fact" role="listitem">
              <div className="input-prediction-fact__head">
                <strong>输入拼音时预测</strong>
                <StatusBadge
                  label={effectiveCompositionPrediction === null
                    ? '运行未报告'
                    : effectiveCompositionPrediction && effectiveShowOnlyRime !== true
                      ? '运行已生效'
                      : '运行未生效'}
                  tone={effectiveCompositionPrediction === null
                    ? 'neutral'
                    : effectiveCompositionPrediction && effectiveShowOnlyRime !== true
                      ? 'success'
                      : 'warning'}
                />
              </div>
              <div className="input-prediction-fact__copy">
                <span>{savedCompositionPrediction ? '已保存开启' : '已保存关闭'}</span>
                <small>{compositionRuntimeDetail(runtimeSafetyClamps, effectiveCompositionPrediction, effectiveShowOnlyRime)}</small>
              </div>
            </article>
            <article aria-label="上屏后联想" className="input-prediction-fact" role="listitem">
              <div className="input-prediction-fact__head">
                <strong>上屏后联想</strong>
                <StatusBadge
                  label={effectivePostCommitEnabled === null
                    ? '运行未报告'
                    : effectivePostCommitEnabled
                      ? '运行已生效'
                      : '运行未生效'}
                  tone={effectivePostCommitEnabled === null
                    ? 'neutral'
                    : effectivePostCommitEnabled
                      ? 'success'
                      : 'warning'}
                />
              </div>
              <div className="input-prediction-fact__copy">
                <span>{savedPostCommit ? '已保存开启' : '已保存关闭'}</span>
                <small>上屏后的 AI 联想与 Rime 原生候选分开展示。</small>
              </div>
            </article>
            <article aria-label="MLX 本机模型" className="input-prediction-fact" role="listitem">
              <div className="input-prediction-fact__head">
                <strong>MLX 本机模型</strong>
                <StatusBadge
                  label={modelLoaded === true && mlxMatchesRegistry === true ? '就绪' : '需检查'}
                  tone={modelLoaded === true && mlxMatchesRegistry === true ? 'success' : 'warning'}
                />
              </div>
              <div className="input-prediction-fact__copy">
                <span>{predictorProviderName} · {inputModelDisplayName(activeModelId)}</span>
                <small>{modelRuntimeDetail(modelLoaded, mlxMatchesRegistry)}</small>
              </div>
            </article>
            <p className="input-prediction-facts__boundary">
              Rime 负责拼音解码与原生候选；AI 候选保持独立标识。保存值与运行值不一致时，重新载入输入法设置后复查。
            </p>
          </div>
          {/* 一行事实概览常驻视口：三步各自的真实状态。示意图与车道细节
              折叠在后面，按需展开，不把首屏推成长卷。 */}
          <div className="input-pipeline" data-open={pipelineOpen ? 'true' : 'false'}>
            <button
              aria-controls="input-pipeline-panel"
              aria-expanded={pipelineOpen}
              aria-label="生成步骤详情"
              className="input-pipeline__summary"
              id="input-pipeline-trigger"
              onClick={() => setPipelineOpen((current) => !current)}
              type="button"
            >
              <span className="input-pipeline__steps">
                <span className="input-pipeline__step" data-tone={contextLive.tone}>
                  上下文获取 · {contextLive.value}
                </span>
                <span
                  className="input-pipeline__step"
                  data-off={!recallLane.enabled || undefined}
                  data-tone={recallLane.enabled ? 'success' : 'neutral'}
                >
                  记忆召回 · {recallLane.enabled ? '已启用' : '已关闭'}
                </span>
                <span
                  className="input-pipeline__step"
                  data-off={!completionLane.enabled || undefined}
                  data-tone={completionLane.enabled ? predictorLive.tone : 'neutral'}
                >
                  本机联想 · {completionLane.enabled ? predictorLive.value : '已关闭'}
                </span>
              </span>
              <ChevronDown aria-hidden="true" size={16} />
            </button>
            <InputSettingsDisclosure
              id="input-pipeline-panel"
              labelledBy="input-pipeline-trigger"
              open={pipelineOpen}
            >
              <div className="input-pipeline__detail">
                <SuggestionPanelPreview panel={panel} />
                <ol aria-label="智能候选的生成步骤" className="input-gen-lanes">
                  <GenerationLane
                    badges={[{ label: contextLive.value, tone: contextLive.tone }]}
                    facts={contextLane.facts}
                    icon={TextCursorInput}
                    liveDetail={contextLive.detail}
                    summary={contextLane.summary}
                    title="上下文获取"
                  />
                  <GenerationLane
                    badges={[recallLane.enabled
                      ? { label: '已启用', tone: 'success' }
                      : { label: '已关闭', tone: 'neutral' }]}
                    dimmed={!recallLane.enabled}
                    facts={recallLane.facts}
                    icon={BookMarked}
                    summary={recallLane.summary}
                    title="记忆召回"
                  />
                  <GenerationLane
                    badges={completionLane.enabled
                      ? [{ label: predictorLive.value, tone: predictorLive.tone }, ...(modelHealthBadge ? [modelHealthBadge] : [])]
                      : [{ label: '已关闭', tone: 'neutral' }]}
                    dimmed={!completionLane.enabled}
                    facts={completionLane.facts}
                    icon={Sparkles}
                    liveDetail={completionLane.enabled ? predictorLive.detail : undefined}
                    summary={completionLane.summary}
                    title="本机联想"
                  />
                </ol>
              </div>
            </InputSettingsDisclosure>
          </div>

          {modelStatusUnavailable ? (
            <div className="input-inline-action">
              <InlineNotice title="模型状态暂不可用" tone="warning">
                已保存的选择保持不变；检查成功前不视为已生效。
              </InlineNotice>
              <Button loading={queries.models.isFetching} onClick={() => void queries.models.refetch()} size="small">重试模型检查</Button>
            </div>
          ) : null}
          {modelActionNeeded ? (
            runtimeRevision === null ? (
              <InlineNotice title="正在等待运行版本" tone="warning">运行版本返回后才能安全应用联想模型。</InlineNotice>
            ) : !nativeExternalActions ? (
              <InlineNotice title="请在已安装的应用中操作" tone="warning">网页端只能查看与保存；应用到本机联想服务需在已安装的应用中进行。</InlineNotice>
            ) : (
              <DiagnosticsRuntimeWorkflow
                action="restart_predictor"
                description="应用已保存的联想模型并重启本机服务，完成后重新检查模型状态。"
                nativeExternalActions={nativeExternalActions}
                onApplied={refresh}
                risk="R2"
                runtimeRevision={runtimeRevision}
                title="应用联想模型"
                transport={queries.transport}
              />
            )
          ) : null}
        </QueryState>
      </ManagementSection>

      <ManagementSection
        description="系统输入源与本机补全服务的当前状态。"
        title="输入链路"
      >
        <QueryState
          error={sourceError}
          headingLevel={3}
          isPending={queries.source.isPending}
          onRetry={() => void queries.source.refetch()}
        >
          <div aria-label="输入链路状态" className="input-status-grid" role="list">
            <InputStatusItem
              detail={inputSourceDetail(source)}
              icon={Keyboard}
              label="系统输入源"
              tone={booleanValue(source.typingReady) ? 'success' : 'warning'}
              value={readinessLabel(source)}
            />
            <InputStatusItem
              {...componentStatus(
                asRecord(components.sidecar),
                '补全服务',
                Network,
                queries.overview.isPending,
                overviewError,
              )}
            />
          </div>

          {overviewError ? (
            <div className="input-inline-action">
              <InlineNotice title="部分运行状态读取失败" tone="danger">
                {publicErrorText(overviewError, '输入源状态可用，但暂时无法读取其他运行状态。')}
              </InlineNotice>
              <Button onClick={() => void queries.overview.refetch()} size="small">重试运行状态</Button>
            </div>
          ) : null}

          {booleanValue(source.typingReady) ? (
            <InlineNotice title="系统检查通过" tone="success">{inputSourceMessage(source)}</InlineNotice>
          ) : (
            <div className="input-inline-action">
              <InlineNotice title="输入源需要处理" tone="warning">{inputSourceMessage(source)}</InlineNotice>
              <Button leadingIcon={<TextCursorInput size={14} />} onClick={openDiagnostics} size="small">打开问题排查</Button>
            </div>
          )}
        </QueryState>
      </ManagementSection>

      <ManagementSection
        description="仅显示最近少量输入请求的运行摘要；原文、候选与诊断原因不会在这里展开。"
        title="输入 Trace"
        trailing={<StatusBadge label={queries.predictionLiveTrace.isFetching ? '正在读取' : '本机摘要'} tone="neutral" />}
      >
        <InputTraceDiscoverability query={queries.predictionLiveTrace} />
      </ManagementSection>

      <ManagementSection
        description="候选数量、触发时机与本机联想参数，只保存本次改动。"
        title="输入体验设置"
        trailing={(
          <StatusBadge
            label={settingsPending ? '正在读取' : settingsError ? '读取失败' : String(fieldCount) + ' 项'}
            tone={settingsError ? 'danger' : 'neutral'}
          />
        )}
      >
        {settingsError && hasRetainedSettings ? <InlineNotice title="输入设置刷新失败" tone="warning">
          已读取的设置和未保存草稿仍在；重新读取成功前不能保存。请使用页面上的刷新重试。
        </InlineNotice> : null}
        <QueryState
          error={hasRetainedSettings ? null : settingsError}
          headingLevel={3}
          isPending={settingsPending}
          onRetry={() => void Promise.all([queries.settings.refetch(), queries.schema.refetch()])}
        >
          {settingsGroups.length ? (
            <div className="input-settings-layout">
              <div
                className="input-settings-ledger"
                data-attention={hasInvalidChanges || diffRows.length > 0 || undefined}
              >
                <div className="input-settings-ledger__row">
                  <h3 className="input-settings-diff-title">待保存更改</h3>
                  <StatusBadge
                    label={hasInvalidChanges ? '需要修正' : diffRows.length ? `${diffRows.length} 项待保存` : '尚未修改'}
                    tone={hasInvalidChanges ? 'danger' : diffRows.length ? 'info' : 'neutral'}
                  />
                  {diffRows.length ? (
                    <button
                      aria-controls="input-settings-ledger-panel"
                      aria-expanded={settingsLedgerOpen}
                      className="input-ledger-toggle"
                      id="input-settings-ledger-trigger"
                      onClick={() => setSettingsLedgerOpen((current) => !current)}
                      type="button"
                    >
                      差异清单
                      <ChevronDown aria-hidden="true" size={14} />
                    </button>
                  ) : (
                    <span className="input-settings-ledger__hint">下方分组里的修改会先在这里列出。</span>
                  )}
                </div>
                {diffRows.length ? (
                  <InputSettingsDisclosure
                    id="input-settings-ledger-panel"
                    labelledBy="input-settings-ledger-trigger"
                    open={settingsLedgerOpen}
                  >
                    <DataTable
                      caption="输入设置待保存更改"
                      columns={[
                        { key: 'key', label: '设置项', width: '34%' },
                        { key: 'before', label: '当前' },
                        { key: 'after', label: '目标' },
                        { key: 'applyMode', label: '生效方式', width: '20%' },
                      ]}
                      rows={diffRows}
                    />
                  </InputSettingsDisclosure>
                ) : null}
                <ManagementMutationWorkflow
                  availability={queries.settingsMutationAvailability(
                    settingsError || settingsPending || runtimeRevision === null
                      ? '当前设置状态尚未同步，刷新后才能继续。'
                      : hasInvalidChanges
                        ? '至少一项设置超出可用范围，请先修正。'
                      : diffRows.length === 0
                        ? '修改至少一个输入设置后才能保存。'
                        : '',
                  )}
                  description="只提交差异清单里的改动，按各项的生效方式处理。"
                  draftKey={JSON.stringify({ changes: pendingChanges, runtimeRevision })}
                  mutationKey={['input-method', 'mutation', 'settings']}
                  onApply={async (preview) => parseManagementWorkReceipt(
                    await queries.requestSettingsMutation({
                      pathId: inputSettingsMutationPathIds.apply,
                      body: {
                        changes: preview.context.changes,
                        expectedRuntimeRevision: preview.expectedRuntimeRevision,
                        previewToken: preview.previewToken,
                        payloadSha256: preview.payloadSha256,
                        confirmText: preview.requiredConfirm,
                      },
                    }),
                    inputSettingsMutationPathIds.apply,
                    preview.payloadSha256,
                  )}
                  onApplied={() => {
                    setChanges({});
                    void Promise.all([queries.settings.refetch(), queries.models.refetch(), queries.overview.refetch()]);
                  }}
                  onPreview={async () => {
                    if (settingsError || settingsPending || runtimeRevision === null || hasInvalidChanges || diffRows.length === 0) {
                      throw new Error('输入设置差异或当前版本已失效，请刷新后重试。');
                    }
                    const context = { changes: { ...pendingChanges } };
                    const parsed = parseManagementWorkPreview(
                      await queries.requestSettingsMutation({
                        pathId: inputSettingsMutationPathIds.preview,
                        body: { ...context, expectedRuntimeRevision: runtimeRevision },
                      }),
                      inputSettingsMutationPathIds.apply,
                      context,
                    );
                    return {
                      ...parsed,
                      summary: {
                        ...parsed.summary,
                        title: '保存这些输入设置？',
                        items: diffRows.map((row) => `${row.key}：${row.before} → ${row.after}（${row.applyMode}）`),
                      },
                    };
                  }}
                  onRollback={async (receipt, preview) => parseManagementWorkReceipt(
                    await queries.requestSettingsMutation({
                      pathId: inputSettingsMutationPathIds.rollback,
                      body: {
                        receiptId: receipt.receiptId,
                        rollbackToken: receipt.rollbackToken,
                        payloadSha256: receipt.payloadSha256,
                        confirmText: 'rollback',
                      },
                    }),
                    inputSettingsMutationPathIds.rollback,
                    preview.payloadSha256,
                  )}
                  onRolledBack={() => void Promise.all([queries.settings.refetch(), queries.models.refetch()])}
                  risk={diffRows.some((row) => row.requiresReload) ? 'R2' : 'R1'}
                  title="保存输入体验设置"
                />
              </div>
              <div className="input-settings-grid">
                {settingsGroups.map((section) => (
                  <InputSettingsGroup
                    changes={changes}
                    disabled={settingsWriteAvailability.state !== 'available'}
                    fields={section.fields}
                    id={section.id}
                    key={section.id}
                    modelIds={availableModelIds}
                    onChange={updateSettingChange}
                    settings={settings}
                  />
                ))}
              </div>
            </div>
          ) : (
            <EmptyState
              action={<Button leadingIcon={<TextCursorInput size={14} />} onClick={openDiagnostics} size="small">打开问题排查</Button>}
              description="当前没有可显示的输入设置。刷新后仍为空时，可到问题排查页检查状态。"
              headingLevel={3}
              icon={TextCursorInput}
              title="暂无输入设置"
            />
          )}
        </QueryState>
      </ManagementSection>

      <ManagementSection
        description="把已保存的候选与触发设置应用到输入法；词库与个人配置不受影响。"
        title="重新载入输入法设置"
      >
        {!nativeExternalActions ? (
          <InlineNotice title="请在已安装的应用中操作" tone="warning">
            重新载入功能会在已安装应用中出现。
          </InlineNotice>
        ) : runtimeRevision === null ? (
          <InlineNotice title="正在等待运行版本" tone="warning">运行状态返回后才能安全地重新载入输入法设置。</InlineNotice>
        ) : (
          <DiagnosticsRuntimeWorkflow
            action="redeploy_rime"
            description="重载当前输入法并应用已保存设置；完成后用实际输入与选词确认。"
            nativeExternalActions={nativeExternalActions}
            onApplied={refresh}
            risk="R3"
            runtimeRevision={runtimeRevision}
            title="重新载入输入法设置"
            transport={queries.transport}
          />
        )}
      </ManagementSection>

    </ManagementPage>
  );
}

export function InputLexiconFeature() {
  const desktop = usePawOsDesktop();
  const openDiagnostics = () => openPawOsRoute(desktop, '/diagnostics');
  const queries = useInputMethodQueries('lexicon');
  const lexiconState = lexiconSectionStatus({
    capabilityError: Boolean(queries.capabilities.error),
    capabilityPending: queries.capabilities.isPending,
    entryCount: queries.lexiconReview.data?.entryCount,
    reviewError: Boolean(queries.lexiconReview.error),
    reviewPending: queries.lexiconReview.isPending,
    supported: queries.lexiconAvailable,
  });
  const isRefreshing = queries.capabilities.isFetching || queries.lexiconReview.isFetching;
  const refresh = () => {
    const jobs: Promise<unknown>[] = [queries.capabilities.refetch()];
    if (queries.lexiconAvailable) jobs.push(queries.lexiconReview.refetch());
    void Promise.all(jobs);
  };

  return (
    <ManagementPage
      actions={(
        <Button
          leadingIcon={<RefreshCw size={15} />}
          loading={isRefreshing}
          onClick={refresh}
          size="small"
        >
          刷新审阅
        </Button>
      )}
      description="审阅本机整理出的常用词建议；只写入你勾选的词条，写入后可撤销。"
      eyebrow="输入体验"
      routeId="input-lexicon"
      title="个人词库"
    >
      <ManagementSection
        description="逐条核对来源与采用记录；解码与排序仍归 Rime。"
        title="常用词建议"
        trailing={<StatusBadge label={lexiconState.label} tone={lexiconState.tone} />}
      >
        {queries.capabilities.isPending ? (
          <InlineNotice title="正在确认可用能力" tone="info">正在确认审阅与撤销能力。</InlineNotice>
        ) : queries.capabilities.error ? (
          <div className="input-inline-action">
            <InlineNotice title="无法确认词库能力" tone="danger">
              {publicErrorText(queries.capabilities.error, '暂时无法确认词库能力，请稍后重试。')}
            </InlineNotice>
            <Button onClick={() => void queries.capabilities.refetch()} size="small">重试能力检查</Button>
          </div>
        ) : !queries.lexiconAvailable ? (
          <div className="input-inline-action">
            <InlineNotice title="词库管理不可用" tone="warning">当前版本缺少完整的审阅、写入与撤销能力，本页不会执行任何更改。</InlineNotice>
            <Button leadingIcon={<TextCursorInput size={14} />} onClick={openDiagnostics} size="small">打开问题排查</Button>
          </div>
        ) : queries.lexiconReview.isPending ? (
          <InlineNotice title="正在读取词库建议" tone="info">正在准备本次可审阅的词条。</InlineNotice>
        ) : queries.lexiconReview.error ? (
          <div className="input-inline-action">
            <InlineNotice title="词库审阅失败" tone="danger">{publicErrorText(queries.lexiconReview.error, '暂时无法读取词库建议，请稍后重试。')}</InlineNotice>
            <Button onClick={() => void queries.lexiconReview.refetch()} size="small">重试审阅</Button>
          </div>
        ) : !queries.lexiconReview.data ? (
          <InlineNotice title="词库审阅不可用" tone="warning">当前没有可验证的词库审阅记录，请刷新后重试。</InlineNotice>
        ) : null}
        {queries.lexiconReview.data ? (
          <>
            <LexiconOrganizationState organization={queries.lexiconReview.data.organization} />
            <LexiconWorkflow
              applyDisabled={queries.lexiconReview.isFetching || Boolean(queries.lexiconReview.error) || Boolean(queries.capabilities.error) || !queries.lexiconAvailable}
              onRefresh={() => void queries.lexiconReview.refetch()}
              review={queries.lexiconReview.data}
              rollbackDisabled={Boolean(queries.capabilities.error) || !queries.lexiconAvailable}
              transport={queries.transport}
            />
          </>
        ) : null}
      </ManagementSection>
    </ManagementPage>
  );
}

function InputTraceDiscoverability({
  query,
}: {
  query: ReturnType<typeof useInputMethodQueries>['predictionLiveTrace'];
}) {
  if (query.isPending) {
    return <InlineNotice title="正在读取输入 Trace" tone="info">正在等待本机最近请求的摘要。</InlineNotice>;
  }
  if (query.error) {
    return (
      <div className="input-inline-action">
        <InlineNotice title="输入 Trace 暂不可用" tone="warning">
          {publicErrorText(query.error, '本机摘要暂时不可用；不会影响正常输入。')}
        </InlineNotice>
        <Button onClick={() => void query.refetch()} size="small">重试 Trace</Button>
      </div>
    );
  }
  const summaries = predictionLiveTraceSummaries(query.data);
  const latest = summaries.length ? summaries[summaries.length - 1] : null;
  if (!latest) {
    return <InlineNotice title="尚未有可定位的输入请求" tone="info">输入发生后，这里会显示最近请求的安全摘要。</InlineNotice>;
  }
  return (
    <div aria-label="输入 Trace 最近请求" className="input-trace-discovery">
      <div className="input-trace-discovery__head">
        <div>
          <strong>最近请求</strong>
          <span>会话 {latest.sessionId} · 请求 #{latest.requestSeq}</span>
        </div>
        <a href={`#/observability?traceId=${encodeURIComponent(latest.traceId)}`}>查看标准 Trace</a>
      </div>
      <div className="input-trace-discovery__lanes">
        <InputTraceLane label="知识召回" lane={latest.ragLane} />
        <InputTraceLane label="本机联想" lane={latest.modelLane} />
      </div>
      {summaries.length > 1 ? <small>已保留最近 {summaries.length} 条可定位请求</small> : null}
    </div>
  );
}

function InputTraceLane({
  label,
  lane,
}: {
  label: string;
  lane: PredictionTraceFrameSummary['ragLane'];
}) {
  const status = ({
    completed: '已完成',
    timed_out: '超时',
    dropped: '已丢弃',
    waiting: '等待中',
    skipped: '已跳过',
    not_reported: '未报告',
  } as const)[lane.status];
  const count = lane.count === null ? '数量未知' : `${lane.count} 条`;
  const elapsed = lane.elapsedMs === null ? '耗时未知' : `${lane.elapsedMs} ms`;
  return (
    <div className="input-trace-discovery__lane">
      <span>{label}</span>
      <StatusBadge label={status} tone={lane.status === 'completed' ? 'success' : lane.status === 'not_reported' ? 'neutral' : 'warning'} />
      <small>{count} · {elapsed}</small>
    </div>
  );
}

function InputSettingsGroup({
  changes,
  disabled,
  fields,
  id,
  modelIds,
  onChange,
  settings,
}: {
  changes: Record<string, DraftValue>;
  disabled: boolean;
  fields: Record<string, unknown>[];
  id: string;
  modelIds: readonly string[];
  onChange: (key: string, value: DraftValue) => void;
  settings: Record<string, unknown>;
}) {
  // 视口台账：所有分组默认收起，首屏只读分组索引与待保存状态。
  const [userOpen, setUserOpen] = useState(false);
  const [advancedUserOpen, setAdvancedUserOpen] = useState(false);
  const dailyFields = fields.filter((field) => stringValue(field.key) !== 'models.path');
  const advancedFields = fields.filter((field) => stringValue(field.key) === 'models.path');
  const changedKeys = fields
    .map((field) => stringValue(field.key))
    .filter((key) => key in changes && !Object.is(valueAt(settings, key), changes[key]));
  const invalidKeys = changedKeys.filter((key) => {
    const field = fields.find((item) => stringValue(item.key) === key) ?? {};
    return !validInputSettingValue(field, changes[key]);
  });
  const needsAttention = changedKeys.length > 0 || invalidKeys.length > 0;
  const open = userOpen;
  const advancedNeedsAttention = advancedFields.some((field) => changedKeys.includes(stringValue(field.key)));
  const advancedOpen = advancedUserOpen;
  const status = invalidKeys.length
    ? `${invalidKeys.length} 项需修正`
    : changedKeys.length
      ? `${changedKeys.length} 项待保存`
      : `共 ${fields.length} 项`;
  const triggerId = `input-settings-trigger-${id}`;
  const panelId = `input-settings-panel-${id}`;
  const advancedTriggerId = `input-settings-advanced-trigger-${id}`;
  const advancedPanelId = `input-settings-advanced-panel-${id}`;

  return (
    <section
      aria-labelledby={`input-settings-${id}`}
      className="input-settings-group"
      data-attention={needsAttention || undefined}
      data-open={open ? 'true' : 'false'}
      role="group"
    >
      <button
        aria-controls={panelId}
        aria-expanded={open}
        className="input-settings-group__summary"
        id={triggerId}
        onClick={() => setUserOpen((current) => !current)}
        type="button"
      >
        <span className="input-settings-group__copy">
          <span aria-level={3} id={`input-settings-${id}`} role="heading">{sectionLabel(id)}</span>
          <small>{inputSettingsGroupDescription(id)}</small>
        </span>
        <span className="input-settings-group__meta">
          <span>{status}</span>
          <ChevronDown aria-hidden="true" size={16} />
        </span>
      </button>
      <InputSettingsDisclosure id={panelId} labelledBy={triggerId} open={open}>
        <div className="input-setting-list">
          {dailyFields.map((field) => {
            const key = stringValue(field.key);
            return (
              <InputSettingField
                disabled={disabled}
                edited={key in changes && !Object.is(valueAt(settings, key), changes[key])}
                field={field}
                key={key}
                modelIds={key === 'models.modelId' ? modelIds : undefined}
                onChange={(value) => onChange(key, value)}
                value={changes[key] ?? valueAt(settings, key)}
              />
            );
          })}
          {advancedFields.length ? (
            <section
              className="input-settings-advanced"
              data-attention={advancedNeedsAttention || undefined}
              data-open={advancedOpen ? 'true' : 'false'}
            >
              <button
                aria-controls={advancedPanelId}
                aria-expanded={advancedOpen}
                id={advancedTriggerId}
                onClick={() => setAdvancedUserOpen((current) => !current)}
                type="button"
              >
                <span>高级：模型文件位置</span>
                <ChevronDown aria-hidden="true" size={14} />
              </button>
              <InputSettingsDisclosure
                id={advancedPanelId}
                labelledBy={advancedTriggerId}
                open={advancedOpen}
              >
                <div className="input-settings-advanced__content">
                  {advancedFields.map((field) => {
                    const key = stringValue(field.key);
                    return (
                      <InputSettingField
                        disabled={disabled}
                        edited={key in changes && !Object.is(valueAt(settings, key), changes[key])}
                        field={field}
                        key={key}
                        onChange={(value) => onChange(key, value)}
                        value={changes[key] ?? valueAt(settings, key)}
                      />
                    );
                  })}
                </div>
              </InputSettingsDisclosure>
            </section>
          ) : null}
        </div>
      </InputSettingsDisclosure>
    </section>
  );
}

function InputSettingsDisclosure({
  children,
  id,
  labelledBy,
  open,
}: {
  children: ReactNode;
  id: string;
  labelledBy: string;
  open: boolean;
}) {
  return (
    <div
      aria-hidden={!open}
      aria-labelledby={labelledBy}
      className="input-settings-disclosure"
      data-open={open ? 'true' : 'false'}
      id={id}
      inert={open ? undefined : true}
    >
      <div>
        <div className="input-settings-disclosure__content">{children}</div>
      </div>
    </div>
  );
}

function inputSettingsGroupDescription(id: string): string {
  return ({
    interaction: '联想的触发时机与按键接纳。',
    display: '候选数量与面板样式。',
    activeRag: '知识结果的插入位置与等待。',
    pinyin: '模糊音方案。',
    models: '本机模型与生成参数。',
    lexiconOrganization: '常用词整理频率。',
  } as Record<string, string>)[id] ?? '这一组输入设置。';
}

function optionalBoolean(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function compositionRuntimeDetail(
  clamps: readonly string[],
  enabled: boolean | null,
  showOnlyRime: boolean | null,
): string {
  const reasons: string[] = [];
  if (clamps.includes('composition_ai_disabled_by_profile')) {
    reasons.push('当前运行模式关闭了 AI 拼音候选');
  }
  if (clamps.includes('composition_rime_only') || showOnlyRime === true) {
    reasons.push('Rime 独占拼音候选');
  }
  if (reasons.length) return reasons.join('；');
  if (enabled === true) return 'AI 候选已进入运行配置，并与 Rime 原生候选分开展示。';
  if (enabled === false) return '运行配置当前未启用 AI 拼音候选。';
  return '运行端尚未报告拼音候选是否生效。';
}

function inputModelDisplayName(modelId: string): string {
  if (!modelId || modelId === '由本机注册表决定') return '由本机注册表决定';
  return modelId
    .split(/[-_]+/)
    .filter(Boolean)
    .map((part) => {
      const lower = part.toLowerCase();
      if (lower === 'minimind') return 'MiniMind';
      if (lower === 'ime') return 'IME';
      if (/^v\d+$/i.test(part)) return part.toLowerCase();
      if (/^\d+m$/i.test(part)) return part.toUpperCase();
      return part.charAt(0).toUpperCase() + part.slice(1);
    })
    .join(' ');
}

function modelRuntimeDetail(modelLoaded: boolean | null, registryMatches: boolean | null): string {
  const load = modelLoaded === true ? '模型已加载' : modelLoaded === false ? '模型未加载' : '加载状态未报告';
  const registry = registryMatches === true
    ? '注册配置一致'
    : registryMatches === false
      ? '注册配置不一致'
      : '注册配置状态未报告';
  return `${load} · ${registry}`;
}

function LexiconOrganizationState({
  organization,
}: {
  organization: LexiconOrganizationStatus;
}) {
  const lastRun = organization.lastRun;
  const failure = lastRun.status === 'failed';
  return (
    <div className="mgmt-stack">
      <dl className="mgmt-kv">
        <dt>定期整理建议</dt>
        <dd>{organization.enabled ? `已启用 · 每天约 ${organization.runsPerDay} 次` : '已停用'}</dd>
        <dt>上次运行</dt>
        <dd>{formatLexiconRunTime(organization.lastRunAtMs, '尚未运行')}</dd>
        <dt>下次运行</dt>
        <dd>{organization.enabled && organization.nextRunAtMs !== null ? formatLexiconRunTime(organization.nextRunAtMs, '等待下一次本机整理') : '已停用'}</dd>
        <dt>本次结果</dt>
        <dd>{lastRun.status === 'succeeded' ? `已整理 ${lastRun.candidateCount} 条待审阅建议` : lastRun.status === 'running' ? '正在本机整理' : failure ? '运行失败' : '等待首次运行'}</dd>
      </dl>
      {failure ? (
        <InlineNotice title="上次定期整理失败" tone="danger">
          {publicErrorText(lastRun.error, '本机任务没有返回成功结果。')}
        </InlineNotice>
      ) : (
        <InlineNotice title="本机整理，先审阅再写入" tone="info">
          建议只来自本机选词反馈；你保存前，词库不变。
        </InlineNotice>
      )}
    </div>
  );
}

function formatLexiconRunTime(value: number, fallback: string): string {
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));
}

function InputSettingField({
  disabled,
  edited,
  field,
  modelIds,
  onChange,
  value,
}: {
  disabled: boolean;
  edited: boolean;
  field: Record<string, unknown>;
  modelIds?: readonly string[];
  onChange: (value: DraftValue) => void;
  value: unknown;
}) {
  const key = stringValue(field.key);
  const label = inputFieldFallback(key);
  const description = [
    publicInputText(stringValue(field.description), '当前输入设置'),
    inputSettingHint(key, field),
    applyModeLabel(stringValue(field.applyMode, 'live')),
  ].filter(Boolean).join(' · ');
  const type = stringValue(field.type, 'string');
  const id = `input-setting-${key.replace(/[^A-Za-z0-9_-]/g, '-')}`;
  const invalid = edited && !validInputSettingValue(field, value as DraftValue);
  const error = invalid ? inputSettingValidationMessage(field) : undefined;

  if (type === 'boolean') {
    return (
      <div className="input-setting-editor-row">
        <Switch
          checked={value === true}
          description={description}
          disabled={disabled}
          label={label}
          onCheckedChange={onChange}
        />
      </div>
    );
  }
  if (key === 'models.modelId' && modelIds?.length) {
    const current = stringValue(value);
    const options = [...new Set([...modelIds, ...(current ? [current] : [])])];
    return (
      <div className="input-setting-editor-row">
        <Field description={description} error={error} htmlFor={id} label={label}>
          <Select
            aria-invalid={invalid || undefined}
            disabled={disabled}
            id={id}
            onValueChange={(next) => onChange(next === '__active_model__' ? '' : next)}
            options={[
              { value: '__active_model__', label: '沿用当前本机模型' },
              ...options.map((modelId) => ({ value: modelId, label: modelId })),
            ]}
            value={current || '__active_model__'}
          />
        </Field>
      </div>
    );
  }
  if (Array.isArray(field.options)) {
    return (
      <div className="input-setting-editor-row">
        <Field description={description} error={error} htmlFor={id} label={label}>
          <Select
            aria-invalid={invalid || undefined}
            disabled={disabled}
            id={id}
            onValueChange={onChange}
            options={field.options.map((option) => ({
              value: String(option),
              label: inputOptionLabel(String(option)),
            }))}
            value={stringValue(value)}
          />
        </Field>
      </div>
    );
  }
  if (type === 'integer' || type === 'number') {
    return (
      <div className="input-setting-editor-row">
        <Field description={description} error={error} htmlFor={id} label={label}>
          <Input
            aria-invalid={invalid || undefined}
            disabled={disabled}
            id={id}
            max={typeof field.max === 'number' ? field.max : undefined}
            min={typeof field.min === 'number' ? field.min : undefined}
            onChange={(event) => onChange(Number(event.target.value))}
            step={typeof field.step === 'number' ? field.step : 1}
            type="number"
            value={typeof value === 'number' ? String(value) : ''}
          />
        </Field>
      </div>
    );
  }
  if (type === 'string') {
    return (
      <div className="input-setting-editor-row">
        <Field description={description} error={error} htmlFor={id} label={label}>
          <Input
            aria-invalid={invalid || undefined}
            disabled={disabled}
            id={id}
            maxLength={typeof field.maxLength === 'number' ? field.maxLength : undefined}
            onChange={(event) => onChange(event.target.value)}
            type="text"
            value={stringValue(value)}
          />
        </Field>
      </div>
    );
  }
  return (
    <div className="input-setting-editor-row input-setting-editor-row--readonly">
      <span><strong>{label}</strong><small>{description}</small></span>
      <StatusBadge label={formatSetting(value, key)} tone="neutral" />
    </div>
  );
}

function inputSettingValidationMessage(field: Record<string, unknown>): string {
  const minimum = typeof field.min === 'number' ? field.min : null;
  const maximum = typeof field.max === 'number' ? field.max : null;
  if (minimum !== null && maximum !== null) return `请输入 ${minimum} 到 ${maximum} 之间的数值。`;
  if (minimum !== null) return `请输入不小于 ${minimum} 的数值。`;
  if (maximum !== null) return `请输入不大于 ${maximum} 的数值。`;
  if (typeof field.maxLength === 'number') return `最多输入 ${field.maxLength} 个字符。`;
  if (stringValue(field.key) === 'models.path') return '请输入以 / 或 ~/ 开头的本机路径。';
  return '当前值不可用，请重新选择。';
}

function inputSettingHint(key: string, field: Record<string, unknown>): string {
  if (['interaction.postCommit.idleTriggerMs', 'interaction.postCommit.cooldownMs', 'interaction.postCommit.panelTtlMs', 'interaction.postCommit.modelBudgetMs', 'activeRag.latencyBudgetMs'].includes(key)) {
    return '单位：毫秒';
  }
  if (key === 'interaction.postCommit.minDeltaChars') return '单位：字';
  if (key === 'models.temperature' && field.min === 0 && field.max === 1) return '0–1，越高变化越明显';
  if (key === 'models.topP' && field.min === 0 && field.max === 1) return '0–1，越高范围更宽';
  return '';
}

/* 候选面板示意只按当前真实设置绘制：槽位数量、紧凑/展开、采纳方式与
 * 停留时长全部来自已保存配置。槽位内容是抽象占位，绝不编造候选正文，
 * 也不冒充前台运行证据。 */
function SuggestionPanelPreview({ panel }: { panel: SuggestionPanel }) {
  const slotCount = panel.candidateCount || 3;
  return (
    <figure
      aria-label="上屏后智能候选面板示意"
      className="input-suggest-preview"
      data-enabled={panel.enabled ? 'true' : 'false'}
      data-panel={panel.expanded ? 'expanded' : 'compact'}
    >
      <div aria-hidden="true" className="input-suggest-preview__stage">
        <span className="input-suggest-preview__committed">
          <i />
          <em>刚上屏</em>
        </span>
        {panel.enabled ? (
          <span className="input-suggest-preview__panel">
            <span className="input-suggest-preview__tag">智能候选</span>
            <span className="input-suggest-preview__slots">
              {Array.from({ length: slotCount }, (_, index) => (
                <span className="input-suggest-preview__slot" key={index}>
                  <b>{index + 1}</b>
                  <i />
                </span>
              ))}
            </span>
          </span>
        ) : (
          <span className="input-suggest-preview__panel input-suggest-preview__panel--off">
            不出现智能候选
          </span>
        )}
      </div>
      <figcaption>
        <span className="input-suggest-preview__note">
          {panel.enabled
            ? '与原生候选并排出现、样式可见区分；解码与选字仍归输入法。'
            : '已关闭：上屏后不出现智能候选，输入完全交回系统输入法。'}
        </span>
        {panel.hints.length ? (
          <span aria-label="采纳方式" className="input-suggest-preview__hints" role="list">
            {panel.hints.map((hint) => <span key={hint} role="listitem">{hint}</span>)}
          </span>
        ) : null}
      </figcaption>
    </figure>
  );
}

function GenerationLane({
  badges,
  dimmed,
  facts,
  icon: Icon,
  liveDetail,
  summary,
  title,
}: {
  badges: readonly { label: string; tone: StatusTone }[];
  dimmed?: boolean;
  facts: readonly string[];
  icon: LucideIcon;
  liveDetail?: string;
  summary: string;
  title: string;
}) {
  return (
    <li className="input-gen-lane" data-dimmed={dimmed || undefined}>
      <div className="input-gen-lane__head">
        <span aria-hidden="true" className="input-gen-lane__icon"><Icon size={15} /></span>
        <strong>{title}</strong>
        <span className="input-gen-lane__badges">
          {badges.map((badge) => (
            <StatusBadge key={badge.label} label={badge.label} tone={badge.tone} />
          ))}
        </span>
      </div>
      <p>{summary}</p>
      {liveDetail ? <small>{liveDetail}</small> : null}
      {facts.length ? (
        <ul className="input-gen-lane__facts">
          {facts.map((fact) => <li key={fact}>{fact}</li>)}
        </ul>
      ) : null}
    </li>
  );
}

function InputStatusItem({
  detail,
  icon: Icon,
  label,
  tone,
  value,
}: {
  detail: string;
  icon: LucideIcon;
  label: string;
  tone: StatusTone;
  value: string;
}) {
  return (
    <div className="input-status-item" data-tone={tone} role="listitem">
      <span aria-hidden="true" className="input-status-item__icon"><Icon size={16} /></span>
      <span className="input-status-item__copy">
        <small>{label}</small>
        <strong>{value}</strong>
        <span title={detail}>{detail}</span>
      </span>
    </div>
  );
}


function lexiconSectionStatus({
  capabilityError,
  capabilityPending,
  entryCount,
  reviewError,
  reviewPending,
  supported,
}: {
  capabilityError: boolean;
  capabilityPending: boolean;
  entryCount: number | undefined;
  reviewError: boolean;
  reviewPending: boolean;
  supported: boolean;
}): { label: string; tone: StatusTone } {
  if (capabilityPending) return { label: '能力检查中', tone: 'neutral' };
  if (capabilityError) return { label: '能力读取失败', tone: 'danger' };
  if (!supported) return { label: '当前不可用', tone: 'warning' };
  if (reviewPending) return { label: '正在读取', tone: 'neutral' };
  if (reviewError) return { label: '读取失败', tone: 'danger' };
  if (entryCount === undefined) return { label: '等待审阅快照', tone: 'warning' };
  return {
    label: entryCount > 0 ? String(entryCount) + ' 条待审' : '暂无待审',
    tone: entryCount > 0 ? 'info' : 'success',
  };
}
