import {
  Activity,
  Bot,
  BookOpen,
  CircleAlert,
  Eye,
  Fingerprint,
  FlaskConical,
  FolderOpen,
  Gauge,
  History,
  Keyboard,
  LibraryBig,
  LoaderCircle,
  Mic2,
  Network,
  PackageCheck,
  PackageOpen,
  Palette,
  RefreshCw,
  Search,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { Fragment, lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { useControlTransport } from '@/app/control-transport';
import { Button, EmptyState, Input, SegmentedControl, Switch } from '@/components/primitives';
import {
  useAgentPreferencesAuthority,
  type AgentExecutionMode,
} from '@/features/agent/composer/agent-preferences-store';
import { ModelChoiceList } from '@/features/agent/composer/ModelChoiceList';
import {
  countModelChoices,
  modelChoiceGroupsFromPiOptions,
} from '@/features/agent/composer/model-choice';
import {
  parsePiModelCatalogOptions,
  supportedPiThinkingLevels,
} from '@/features/agent/model-catalog-options';
import { diagnosticsQueryKeys } from '@/features/diagnostics/api';
import {
  InlineNotice,
  ManagementPage,
  ManagementSection,
  QueryState,
  StatusBadge,
  arrayRecords,
  asRecord,
  publicErrorText,
  stringValue,
} from '@/features/overview/management-ui';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { pluginQueryKeys, usePluginCatalog } from '@/features/plugins/api';
import {
  agentModelRouting,
  roleModelCatalog,
  type AgentModelRoute,
  type AgentModelRouting,
  type ModelRouteId,
} from '@/features/roles/role-model';
import type { PawAppId } from '../runtime/app-registry';
import { pawApp } from '../runtime/app-registry';
import { AppSidebarToggle, useAppSidebar } from './app-sidebar';

const ApprovalsFeature = lazy(async () => ({ default: (await import('@/features/approvals')).ApprovalsFeature }));
const ConfigurationFeature = lazy(async () => ({ default: (await import('@/features/configuration')).ConfigurationFeature }));
const PluginScenes = lazy(async () => ({ default: (await import('@/features/plugins/PluginScenes')).PluginScenes }));
const PawOsAppearanceSettings = lazy(async () => ({ default: (await import('@/features/configuration/PawOsAppearanceSettings')).PawOsAppearanceSettings }));
const ContextDebugFeature = lazy(async () => ({ default: (await import('@/features/context-debug')).ContextDebugFeature }));
const DiagnosticsFeature = lazy(async () => ({ default: (await import('@/features/diagnostics')).DiagnosticsFeature }));
const GovernanceFeature = lazy(async () => ({ default: (await import('@/features/governance')).GovernanceFeature }));
const HistoryFeature = lazy(async () => ({ default: (await import('@/features/history')).HistoryFeature }));
const PluginsFeature = lazy(async () => ({ default: (await import('@/features/plugins')).PluginsFeature }));
const ModelRoutingPanel = lazy(async () => ({ default: (await import('@/features/roles')).ModelRoutingPanel }));
const ObservabilityFeature = lazy(async () => ({ default: (await import('@/features/observability')).ObservabilityFeature }));
const VoiceFeature = lazy(async () => ({ default: (await import('@/features/voice')).VoiceFeature }));
const InputLexiconFeature = lazy(async () => ({ default: (await import('@/features/input-method')).InputLexiconFeature }));
const InputMethodFeature = lazy(async () => ({ default: (await import('@/features/input-method')).InputMethodFeature }));

export const pawSystemAppIds = [
  'input-studio',
  'app-center',
  'system-monitor',
  'system-settings',
] as const;

export type PawSystemAppId = (typeof pawSystemAppIds)[number];

type SystemPage = {
  id: string;
  label: string;
  icon: LucideIcon;
  route: string;
  group?: string;
  /** One line of purpose, carried by the stage chrome instead of a hero header. */
  purpose: string;
  external?: boolean;
};

const systemPages: Record<PawSystemAppId, readonly SystemPage[]> = {
  'input-studio': [
    { id: 'input', label: '输入法', icon: Keyboard, route: '/input', purpose: '本机输入法的连接、模式与候选行为' },
    { id: 'lexicon', label: '词库', icon: BookOpen, route: '/input?view=lexicon', purpose: '自定义词条与来源，只影响本机候选' },
    { id: 'voice', label: '语音', icon: Mic2, route: '/voice', purpose: '语音服务、识别结果与润色的处理方式' },
    { id: 'history', label: '输入记录', icon: History, route: '/history', purpose: '本机保留的输入记录，随时可以清理' },
  ],
  'app-center': [
    { id: 'installed', label: '已安装', icon: PackageOpen, route: '/plugins', purpose: '已安装 Package 的启用、更新与移除' },
    { id: 'capabilities', label: '功能开关', icon: Settings2, route: '/plugins?view=capabilities', purpose: '查看每项功能是否启用，调整对话、项目和所有对话默认' },
    { id: 'skills', label: 'Skills', icon: BookOpen, route: '/plugins?view=skills', purpose: '查看 Bundled、项目与 Package Skill 的正文和来源' },
    { id: 'scenes', label: '场景策略', icon: Settings2, route: '/plugins?view=scenes', purpose: '按 App 和场景管理系统提示词、Tool 披露与 Skill 路由' },
    { id: 'studio', label: '制作', icon: Sparkles, route: '/plugins?view=studio', purpose: '自己编写插件，或让 Agent 制作 App 与插件' },
    { id: 'catalog', label: '目录', icon: LibraryBig, route: '/plugins?view=catalog', purpose: '安装之前先看清来源、权限与版本' },
    { id: 'proposals', label: '建议', icon: Sparkles, route: '/plugins?view=proposals', purpose: 'Agent 提出的安装建议，逐项等你确认' },
  ],
  'system-monitor': [
    { id: 'activity', label: '活动', icon: Activity, route: '/observability', group: '实时', purpose: '持续接收运行事件，回看发生时的状态' },
    { id: 'evolution-report', label: '优化报告', icon: FlaskConical, route: '/evolution-report', group: '实验', purpose: '在独立网页读懂冻结实验、指标与 Keep / Reject 边界', external: true },
    { id: 'context', label: '上下文', icon: Network, route: '/context-debug', group: '排查', purpose: '逐轮查看模型实际收到的上下文' },
    { id: 'diagnostics', label: '诊断', icon: Gauge, route: '/diagnostics', group: '排查', purpose: '各组件自报的状态与可执行的检查' },
  ],
  'system-settings': [
    { id: 'configuration', label: '配置', icon: Settings2, route: '/configuration', group: '通用', purpose: '本机服务、路径与运行参数' },
    { id: 'appearance', label: '外观', icon: Palette, route: '/appearance', group: '通用', purpose: '桌面主题、界面动效与 App 身份色' },
    { id: 'agent', label: 'Agent', icon: Bot, route: '/configuration?view=agent', group: 'Agent', purpose: '新对话默认用的模型、深度与执行权限' },
    { id: 'governance', label: '治理', icon: ShieldCheck, route: '/governance', group: '安全与信任', purpose: '风险等级、审计留痕与保护规则' },
    { id: 'approvals', label: '审批', icon: Fingerprint, route: '/approvals', group: '安全与信任', purpose: '等你决定的高风险操作队列' },
  ],
};

export function isPawSystemAppId(appId: PawAppId): appId is PawSystemAppId {
  return (pawSystemAppIds as readonly string[]).includes(appId);
}

export type PawSystemAppsMigratedProps = {
  appId: PawSystemAppId;
  initialRoute?: string;
};

/**
 * Isolated PAWOS container for the four system-facing Apps.
 *
 * The container owns navigation and presentation only. Every page below keeps
 * its existing transport, guarded mutation, native bridge, and persisted state.
 *
 * Shape is a window, not a document: `.paw-system-app` is the size container
 * every band answers to, `__frame` carries the rail beside the stage, and the
 * stage is one thin chrome band above a single scrolling workspace. Nothing
 * below the chrome is pinned to a viewport dimension, so a drag on any window
 * edge is absorbed by the workspace instead of reflowing the whole App.
 */
export function PawSystemAppsMigrated({
  appId,
  initialRoute = '',
}: PawSystemAppsMigratedProps) {
  const pages = systemPages[appId];
  const sidebar = useAppSidebar(appId);
  const app = pawApp(appId);
  const desktop = usePawOsDesktop();
  const route = initialRoute || app.route || pages[0].route;
  const page = systemPageForRoute(pages, route);
  // Each system rail reports one honest number from its own Runtime evidence:
  // Settings queues human approvals, App Center queues install proposals, and
  // Monitor relays components that report a problem.
  const railSignal = useSystemRailSignal(appId);

  return (
    <div
      className="paw-system-app"
      data-page-id={page.id}
      data-stage={systemStageKind[appId]}
      data-system-app={appId}
    >
      <div className="paw-system-app__frame" data-sidebar-collapsed={sidebar.collapsed}>
        <aside className="paw-system-app__nav">
          <AppSidebarToggle collapsed={sidebar.collapsed} controlsId={sidebar.controlsId} label={`${app.label}导航`} onToggle={() => sidebar.setCollapsed(!sidebar.collapsed)} toggleRef={sidebar.toggleRef} />
          <nav aria-label={`${app.label}页面`} {...sidebar.contentProps}>
            {pages.map((candidate, index) => {
              const Icon = candidate.icon;
              const current = !candidate.external && candidate.id === page.id;
              const badge = railSignal && candidate.id === railSignal.pageId ? railSignal.count : 0;
              const name = candidate.external
                ? `${candidate.label}（网页）`
                : badge && railSignal
                ? `${candidate.label}（${railSignal.describe(badge)}）`
                : candidate.label;
              return (
                <Fragment key={candidate.id}>
                  {candidate.group && candidate.group !== pages[index - 1]?.group ? (
                    <span aria-hidden="true" className="paw-system-app__nav-group">{candidate.group}</span>
                  ) : null}
                  {/* The collapsed rail drops the visible label only; the
                      accessible name and the tooltip stay whole. */}
                  <button
                    aria-current={current ? 'page' : undefined}
                    aria-label={name}
                    onClick={() => candidate.external
                      ? openStandalonePage(candidate.route)
                      : openPawOsRoute(desktop, candidate.route)}
                    title={name}
                    type="button"
                  >
                    <Icon aria-hidden="true" size={16} />
                    <span className="paw-system-app__nav-label">{candidate.label}</span>
                    {badge && railSignal ? (
                      <span
                        aria-hidden="true"
                        className="paw-system-app__nav-badge"
                        data-tone={railSignal.tone}
                        key={badge}
                      >
                        {badge > 99 ? '99+' : badge}
                      </span>
                    ) : null}
                  </button>
                </Fragment>
              );
            })}
          </nav>
        </aside>

        <section className="paw-system-app__stage">
          <header className="paw-system-app__chrome" key={page.id}>
            <span aria-hidden="true" className="paw-system-app__page-title">
              {page.group ? `${page.group} · ${page.label}` : page.label}
            </span>
            <span aria-hidden="true" className="paw-system-app__page-purpose">{page.purpose}</span>
          </header>
          <div className="paw-system-app__workspace">
            <MemoryRouter initialEntries={[route]} key={route}>
              <PawSystemRouteReporter expectedRoute={route} />
              <div className="paw-system-app__page" key={`${appId}:${page.id}`}>
                <Suspense fallback={<div className="paw-app-loading" role="status">正在打开 {page.label}…</div>}>
                  <PawSystemSurface appId={appId} pageId={page.id} />
                </Suspense>
              </div>
            </MemoryRouter>
          </div>
        </section>
      </div>
    </div>
  );
}

/**
 * Purpose, not palette. Every App keeps the one cobalt accent and the one
 * control scale; what changes is how its workspace is paced — a settings sheet
 * reads at a preference measure, a catalogue at gallery density, an instrument
 * at monitoring density, a studio at form density.
 */
const systemStageKind: Record<PawSystemAppId, string> = {
  'input-studio': 'studio',
  'app-center': 'gallery',
  'system-monitor': 'instrument',
  'system-settings': 'sheet',
};

function PawSystemRouteReporter({ expectedRoute }: { expectedRoute: string }) {
  const desktop = usePawOsDesktop();
  const location = useLocation();
  const route = `${location.pathname}${location.search}${location.hash}`;
  useEffect(() => {
    if (route !== expectedRoute) openPawOsRoute(desktop, route);
  }, [desktop, expectedRoute, route]);
  return null;
}

function PawSystemSurface({ appId, pageId }: { appId: PawSystemAppId; pageId: string }) {
  if (appId === 'input-studio') {
    if (pageId === 'lexicon') return <InputLexiconFeature />;
    if (pageId === 'voice') return <VoiceFeature />;
    if (pageId === 'history') return <HistoryFeature />;
    return <InputMethodFeature />;
  }
  if (appId === 'app-center') {
    if (pageId === 'scenes') return <PluginScenes />;
    if (pageId === 'catalog') return <PawPackageCatalog />;
    return <PluginsFeature />;
  }
  if (appId === 'system-monitor') {
    if (pageId === 'context') return <ContextDebugFeature />;
    if (pageId === 'diagnostics') return <DiagnosticsFeature />;
    return <ObservabilityFeature />;
  }
  if (pageId === 'agent') return <PawAgentSettings />;
  if (pageId === 'appearance') return <PawAppearanceSettings />;
  if (pageId === 'governance') return <GovernanceFeature />;
  if (pageId === 'approvals') return <ApprovalsFeature />;
  return <ConfigurationFeature />;
}

function PawAppearanceSettings() {
  return (
    <ManagementPage
      description="桌面主题、界面动效与 App 身份色。"
      routeId="appearance"
      title="外观"
    >
      <PawOsAppearanceSettings />
    </ManagementPage>
  );
}

/** Every persisted Session execution mode remains a first-class default. */
const agentExecutionModes: readonly {
  value: AgentExecutionMode;
  title: string;
  detail: string;
  icon: LucideIcon;
  recommended?: boolean;
}[] = [
  {
    value: 'read_only',
    title: '只读（沙箱）',
    detail: 'macOS 沙箱内源文件只读、网络关闭；允许前台验证命令，阻止写入、后台任务与应用动作。',
    icon: Eye,
  },
  {
    value: 'per_action',
    title: '全权限',
    detail: '整个系统与所有 Tool 可用；有影响的操作逐项请求确认。',
    icon: ShieldCheck,
    recommended: true,
  },
  {
    value: 'workspace_managed',
    title: '工作区托管（沙箱）',
    detail: 'macOS 沙箱内新对话需选择并确认项目范围；范围内动作自动批准。',
    icon: FolderOpen,
  },
  {
    value: 'full_trust',
    title: '全自动',
    detail: '整个系统与所有 Tool 可用；每个动作自动批准，仍受操作系统边界约束。',
    icon: Zap,
  },
];

function PawAgentSettings() {
  const desktop = usePawOsDesktop();
  const resource = useAgentModelResource();
  const modelRouting = useAgentModelRoutingAuthority();
  const authority = useAgentPreferencesAuthority();
  const preferences = authority.preferences;
  const catalog = useMemo(
    () => parsePiModelCatalogOptions(resource.data),
    [resource.data],
  );
  const modelGroups = useMemo(
    () => modelChoiceGroupsFromPiOptions(catalog.models),
    [catalog.models],
  );
  const modelCount = countModelChoices(modelGroups);
  const routingCatalog = useMemo(() => roleModelCatalog(resource.data), [resource.data]);
  const selectedModel = catalog.models.find((model) => model.reference === preferences.modelReference);
  const thinkingLevels = supportedPiThinkingLevels(selectedModel, { includeOff: true });
  const controlsDisabled = authority.saving || Boolean(authority.readError);

  function selectModel(reference: string): void {
    const model = catalog.models.find((candidate) => candidate.reference === reference);
    const levels = supportedPiThinkingLevels(model, { includeOff: true });
    void authority.save({
      modelReference: reference,
      thinking: levels.includes(preferences.thinking) ? preferences.thinking : levels[0] ?? 'off',
    });
  }

  return (
    <ManagementPage
      actions={<Button leadingIcon={<RefreshCw size={15} />} loading={resource.loading || authority.isPending || modelRouting.isPending} onClick={() => { resource.reload(); authority.reload(); modelRouting.reload(); }} size="small">刷新</Button>}
      description="决定新对话、Trace 诊断、Room 行星伙伴和私有卫星默认用哪个模型、想多深，以及动手之前要不要先问你。改动只影响之后开始的运行。"
      eyebrow="新对话的起点"
      routeId="agent-settings"
      title="Agent"
    >
      {resource.error ? (
        <div className="paw-system-resource-state" data-state="error" role="alert">
          <CircleAlert aria-hidden="true" size={17} />
          <span>{resource.error}</span>
          <button onClick={resource.reload} type="button">重试</button>
        </div>
      ) : null}
      {authority.readError ? (
        <div className="paw-system-resource-state" data-state="error" role="alert">
          <CircleAlert aria-hidden="true" size={17} />
          <span>{authority.readError}</span>
          <button onClick={authority.reload} type="button">重新读取</button>
        </div>
      ) : null}
      {resource.loading || authority.isPending ? (
        <div className="paw-system-resource-state" data-state="loading" role="status"><LoaderCircle aria-hidden="true" size={17} />正在读取 Agent 默认设置</div>
      ) : (
        <>
          <ManagementSection
            description="清单来自 Runtime 的真实报告，不会显示不可用的选项。"
            title="用哪个模型，想多深"
          >
            <div className="paw-agent-model">
              {/* Same gesture as the Session composer: every model on the
                  surface at once under its provider, chosen with one press.
                  A default is a choice too, so 自动选择 is a row, not a blank
                  field the reader has to interpret. */}
              <div className="paw-agent-model__field">
                <span>模型</span>
                <ModelChoiceList
                  ariaLabel="Agent 模型"
                  className="paw-agent-model__choices agent-model-picker__list"
                  disabled={controlsDisabled}
                  emptyLabel="Runtime 没有报告可用模型"
                  groups={modelGroups}
                  leadingOptions={[{
                    key: '',
                    providerId: '',
                    providerName: '',
                    modelId: '',
                    name: '自动选择',
                    detail: '由 Runtime 为每个新对话挑选',
                  }]}
                  onChoose={(option) => selectModel(option.key)}
                  selectedKey={preferences.modelReference}
                />
                <small>{modelCount ? `${modelCount} 个可用模型` : 'Runtime 没有报告可用模型'}</small>
              </div>
              <div className="paw-agent-model__thinking">
                <span>推理强度</span>
                {selectedModel && thinkingLevels.length ? (
                  <>
                    <SegmentedControl
                      aria-label="Agent 推理强度"
                      disabled={controlsDisabled}
                      items={thinkingLevels.map((level) => ({ value: level, label: thinkingLabel(level) }))}
                      onValueChange={(level) => { void authority.save({ thinking: level }); }}
                      value={thinkingLevels.includes(preferences.thinking) ? preferences.thinking : thinkingLevels[0]}
                    />
                    <small>只显示当前模型明确支持的档位</small>
                  </>
                ) : (
                  <small>选择具体模型后，这里会列出它支持的档位。</small>
                )}
              </div>
            </div>
          </ManagementSection>

          <ModelRoutingPanel
            catalog={routingCatalog}
            onOpenSettings={() => openPawOsRoute(desktop, '/configuration')}
            onSave={modelRouting.save}
            routing={modelRouting.routing}
            saving={modelRouting.saving}
          />
          {modelRouting.readError ? <InlineNotice title="模型分工没有读取" tone="danger">{modelRouting.readError}</InlineNotice> : null}
          {modelRouting.saveError ? <InlineNotice title="模型分工没有保存" tone="danger">{modelRouting.saveError}</InlineNotice> : null}

          <ManagementSection
            description="新对话可以默认使用只读沙箱、全权限、工作区托管沙箱或全自动；每种选择都会原样保存。"
            title="Agent 执行权限"
          >
            <div aria-label="Agent 执行权限" className="paw-agent-modes" role="radiogroup">
              {agentExecutionModes.map((mode) => {
                const Icon = mode.icon;
                return (
                  <label className="paw-agent-mode" key={mode.value}>
                    <input
                      aria-label={mode.title}
                      checked={mode.value === preferences.executionMode}
                      disabled={controlsDisabled}
                      name="paw-agent-execution-mode"
                      onChange={() => { void authority.save({ executionMode: mode.value }); }}
                      type="radio"
                      value={mode.value}
                    />
                    <span aria-hidden="true" className="paw-agent-mode__icon"><Icon size={16} /></span>
                    <span className="paw-agent-mode__copy">
                      <strong>{mode.title}{mode.recommended ? <em>推荐</em> : null}</strong>
                      <small>{mode.detail}</small>
                    </span>
                  </label>
                );
              })}
            </div>
          </ManagementSection>
        </>
      )}
      {!authority.isPending && !authority.writesSupported ? (
        <InlineNotice title="当前版本只能读取 Agent 默认设置" tone="warning">本机服务尚未开放安全保存接口；页面不会保留仅存在于前端的修改。</InlineNotice>
      ) : null}
      {authority.saving ? <InlineNotice title="正在保存" tone="info">写入本机设置后会立即重新读取确认。</InlineNotice> : null}
      {authority.saveError ? <InlineNotice title="Agent 默认设置没有保存" tone="danger">{authority.saveError}</InlineNotice> : null}
    </ManagementPage>
  );
}

/**
 * Decision tracks over the Package catalogue. Every track is a real count on
 * the same Runtime list — switching narrows the view without hiding items.
 */
const catalogTracks = [
  { value: 'all', label: '全部' },
  { value: 'updates', label: '有更新' },
  { value: 'installable', label: '可安装' },
  { value: 'installed', label: '已安装' },
] as const;

type CatalogTrack = (typeof catalogTracks)[number]['value'];

function matchesCatalogTrack(item: Record<string, unknown>, track: CatalogTrack): boolean {
  if (track === 'updates') return item.updateAvailable === true;
  if (track === 'installed') return item.installed === true;
  if (track === 'installable') return item.installed !== true && item.actionable === true;
  return true;
}

function PawPackageCatalog() {
  const {
    apply,
    installed,
    preview,
    validate,
    versions,
  } = usePluginCatalog();
  const [query, setQuery] = useState('');
  const [track, setTrack] = useState<CatalogTrack>('all');
  const [enableAfterInstall, setEnableAfterInstall] = useState(true);
  const [pendingChange, setPendingChange] = useState<Record<string, unknown>>({});
  const [validation, setValidation] = useState<Record<string, unknown>>({});
  const [completedChange, setCompletedChange] = useState<{ summary: string; receiptId: string }>();
  const [error, setError] = useState('');
  const versionItems = arrayRecords(asRecord(versions.data).items);
  const installedEnvelope = asRecord(installed.data);
  const runtimeAvailable = installedEnvelope.runtimeAvailable !== false;
  const filteredItems = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('zh-CN');
    if (!needle) return versionItems;
    return versionItems.filter((item) => [
      stringValue(item.displayName),
      stringValue(item.id),
      stringValue(item.description),
      stringValue(asRecord(item.source).label),
    ].join(' ').toLocaleLowerCase('zh-CN').includes(needle));
  }, [query, versionItems]);
  const trackCounts = useMemo(() => Object.fromEntries(catalogTracks.map(({ value }) => [
    value,
    filteredItems.filter((item) => matchesCatalogTrack(item, value)).length,
  ])) as Record<CatalogTrack, number>, [filteredItems]);
  const visibleItems = track === 'all'
    ? filteredItems
    : filteredItems.filter((item) => matchesCatalogTrack(item, track));
  const pendingSummary = asRecord(pendingChange.summary);
  const busy = validate.isPending || preview.isPending || apply.isPending;
  const queryError = asError(versions.error ?? installed.error);
  const hasCatalogSnapshot = versions.data !== undefined && installed.data !== undefined;
  const refreshCatalog = () => Promise.all([versions.refetch(), installed.refetch()]);

  async function previewCatalogAction(item: Record<string, unknown>): Promise<void> {
    if (busy || queryError) return;
    setError('');
    setCompletedChange(undefined);
    setValidation({});
    setPendingChange({});
    try {
      const checked = asRecord(await validate.mutateAsync({
        catalogId: stringValue(item.id),
        catalogVersion: stringValue(item.latestVersion),
      }));
      const next = asRecord(await preview.mutateAsync({
        action: item.updateAvailable === true ? 'update' : 'install',
        validationToken: stringValue(checked.validationToken),
        enable: item.installed === true ? item.enabled === true : enableAfterInstall,
      }));
      setValidation(checked);
      setPendingChange(next);
    } catch (catalogError) {
      setError(publicErrorText(catalogError, 'Package 检查没有完成。'));
    }
  }

  async function applyPendingChange(): Promise<void> {
    if (busy || queryError) return;
    setError('');
    const summary = `${packageActionLabel(stringValue(pendingSummary.action))}：${stringValue(pendingSummary.displayName, stringValue(pendingSummary.pluginId))}`;
    try {
      const response = asRecord(await apply.mutateAsync({
        previewToken: stringValue(pendingChange.previewToken),
        payloadSha256: stringValue(pendingChange.payloadSha256),
        confirmText: 'apply',
      }));
      setCompletedChange({
        summary,
        receiptId: stringValue(asRecord(response.receipt).receiptId, stringValue(response.receiptId)),
      });
      setPendingChange({});
      setValidation({});
      await refreshCatalog();
    } catch (catalogError) {
      setError(publicErrorText(catalogError, 'Package 更改没有完成。'));
    }
  }

  return (
    <ManagementPage
      actions={<Button leadingIcon={<RefreshCw size={15} />} loading={versions.isFetching || installed.isFetching} onClick={() => void refreshCatalog()} size="small">刷新</Button>}
      description="查看 Runtime 报告的 Package 来源、权限和版本；安装或更新前必须先预览。"
      routeId="plugins-catalog"
      title="Package 目录"
    >
      <QueryState error={hasCatalogSnapshot ? null : queryError} isPending={!hasCatalogSnapshot && (versions.isPending || installed.isPending)} onRetry={() => void refreshCatalog()}>
        {completedChange ? (
          <InlineNotice title="Package 更改已完成" tone="success">
            <p>{completedChange.summary}</p>
            {completedChange.receiptId ? <p>回执 {completedChange.receiptId}</p> : null}
          </InlineNotice>
        ) : null}
        {hasCatalogSnapshot && queryError ? (
          <InlineNotice title="目录未能刷新，已保留上次结果" tone="warning">
            <p>重新读取目录后，可以继续检查安装状态与版本。已完成的更改不会重复执行。</p>
            <Button loading={versions.isFetching || installed.isFetching} onClick={() => void refreshCatalog()} size="small">重试读取目录</Button>
          </InlineNotice>
        ) : null}
        <ManagementSection
          description="目录只展示真实注册项；没有可用条目时保持空状态。"
          title="目录"
          trailing={<StatusBadge label={`${visibleItems.length} 项`} tone="neutral" />}
        >
          {!runtimeAvailable ? <InlineNotice title="Pi Runtime 暂时未连接" tone="warning">目录仍可阅读，但安装和更新要等 Runtime 恢复后再继续。</InlineNotice> : null}
          <div className="paw-system-catalog-tools">
            <label><Search aria-hidden="true" size={15} /><Input aria-label="搜索 Package 目录" onChange={(event) => setQuery(event.target.value)} placeholder="名称、用途或来源" value={query} /></label>
            <Switch checked={enableAfterInstall} label="安装后立即启用" onCheckedChange={setEnableAfterInstall} />
          </div>
          <div aria-label="目录范围" className="paw-system-catalog-tracks" role="group">
            {catalogTracks.map((candidate) => (
              <button
                aria-pressed={track === candidate.value}
                data-active={track === candidate.value || undefined}
                key={candidate.value}
                onClick={() => setTrack(candidate.value)}
                type="button"
              >
                {candidate.label}
                <span aria-hidden="true">{trackCounts[candidate.value]}</span>
              </button>
            ))}
          </div>

          {/* The one pending decision sits above the grid, never below the
              fold of a long catalogue. */}
          {validation.validationToken && pendingChange.previewToken ? (
            <InlineNotice title="等待你的确认" tone="warning">
              <div className="paw-system-package-approval">
                <span><strong>{packageActionLabel(stringValue(pendingSummary.action))}：{stringValue(pendingSummary.displayName, stringValue(pendingSummary.pluginId))}</strong><small>{stringArray(pendingSummary.permissions).length ? `需要的权限：${stringArray(pendingSummary.permissions).join('、')}` : '无额外权限'}</small></span>
                <div><Button disabled={busy} onClick={() => { setPendingChange({}); setValidation({}); }} size="small" variant="quiet">取消</Button><Button disabled={busy || Boolean(queryError)} loading={apply.isPending} onClick={() => void applyPendingChange()} size="small" variant="primary">确认更改</Button></div>
              </div>
            </InlineNotice>
          ) : null}
          {error ? <InlineNotice title="Package 操作未完成" tone="danger">{error}</InlineNotice> : null}

          {visibleItems.length ? (
            <div className="paw-system-package-grid" aria-label="受管 Package 目录">
              {visibleItems.map((item) => {
                const id = stringValue(item.id);
                const source = asRecord(item.source);
                const security = asRecord(item.security);
                const permissions = stringArray(item.permissions);
                const actionable = item.actionable === true;
                const upToDate = item.installed === true && item.updateAvailable !== true;
                return (
                  <article className="paw-system-package" key={id}>
                    <header>
                      <span><PackageOpen aria-hidden="true" size={18} /></span>
                      <div><strong>{stringValue(item.displayName, id)}</strong><small>v{stringValue(item.latestVersion, '未声明')} · {stringValue(source.label, stringValue(item.publisher, '来源未声明'))}</small></div>
                      <StatusBadge {...packageCatalogBadge(item)} />
                    </header>
                    <p>{stringValue(item.description, '这个 Package 没有提供说明。')}</p>
                    <dl>
                      <div><dt><ShieldCheck aria-hidden="true" size={13} />权限</dt><dd>{permissions.length ? permissions.join('、') : '无额外权限'}</dd></div>
                      <div><dt><ShieldAlert aria-hidden="true" size={13} />检查</dt><dd>{stringValue(security.notes, '尚无安全说明')}</dd></div>
                    </dl>
                    <footer>
                      <span>{arrayRecords(item.versions).length} 个版本</span>
                      <Button
                        disabled={!runtimeAvailable || !actionable || upToDate || busy || Boolean(queryError)}
                        leadingIcon={<PackageCheck size={14} />}
                        loading={(validate.isPending || preview.isPending) && stringValue(validate.variables?.catalogId) === id}
                        onClick={() => void previewCatalogAction(item)}
                        size="small"
                      >
                        {item.updateAvailable === true ? '查看更新内容' : upToDate ? '已是最新' : actionable ? '查看安装内容' : '不可安装'}
                      </Button>
                    </footer>
                  </article>
                );
              })}
            </div>
          ) : (
            <EmptyState
              action={versionItems.length && (query.trim() || track !== 'all')
                ? <Button onClick={() => { setQuery(''); setTrack('all'); }} size="small">查看全部</Button>
                : undefined}
              description={versionItems.length ? '换一个关键词或范围试试。' : 'Runtime 没有返回可安装或可更新的 Package。'}
              icon={PackageOpen}
              title={versionItems.length ? '没有匹配的 Package' : '目录为空'}
            />
          )}
        </ManagementSection>
      </QueryState>
    </ManagementPage>
  );
}

function useAgentModelResource() {
  const transport = useControlTransport();
  const [data, setData] = useState<Record<string, unknown>>({});
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision((current) => current + 1), []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void transport.request({ pathId: 'agent.role.models' }).then((response) => {
      if (!active) return;
      setData(asRecord(response));
      setError('');
    }).catch((resourceError) => {
      if (active) setError(publicErrorText(resourceError, '模型目录读取失败。'));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [revision, transport]);

  return { data, error, loading, reload };
}

function useAgentModelRoutingAuthority() {
  const transport = useControlTransport();
  const [routing, setRouting] = useState<AgentModelRouting | null>(null);
  const [readError, setReadError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState<ModelRouteId | ''>('');
  const [isPending, setIsPending] = useState(true);
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision((current) => current + 1), []);

  useEffect(() => {
    let active = true;
    setIsPending(true);
    void transport.request({ pathId: 'agent.configuration.get' }).then((response) => {
      if (!active) return;
      const next = agentModelRouting(response);
      if (!next) throw new Error('Runtime 没有返回可确认的模型分工配置。');
      setRouting(next);
      setReadError('');
    }).catch((error) => {
      if (active) setReadError(publicErrorText(error, '模型分工读取失败。'));
    }).finally(() => {
      if (active) setIsPending(false);
    });
    return () => { active = false; };
  }, [revision, transport]);

  const save = useCallback(async (routeId: ModelRouteId, route: AgentModelRoute) => {
    if (!routing || saving) return;
    setSaving(routeId);
    setSaveError('');
    try {
      const response = await transport.request({
        pathId: 'agent.configuration.update',
        body: {
          expectedRevision: routing.revision,
          changes: { [`modelRouting.${routeId}`]: route },
          updatedBy: 'system-agent-settings-ui',
        },
      });
      const updated = agentModelRouting(response);
      if (!updated) throw new Error('保存结果没有返回可确认的模型分工配置。');
      setRouting(updated);
    } catch (error) {
      setSaveError(publicErrorText(error, '模型分工暂时无法保存。'));
    } finally {
      setSaving('');
    }
  }, [routing, saving, transport]);

  return { isPending, readError, reload, routing, save, saveError, saving };
}

type SystemRailTransport = ReturnType<typeof useControlTransport>;

type SystemRailContract = {
  pageId: string;
  /** decision: a queue waiting for the human; attention: self-reported health. */
  tone: 'decision' | 'attention';
  queryKey: readonly unknown[];
  read: (transport: SystemRailTransport, signal: AbortSignal | undefined) => Promise<unknown>;
  count: (value: unknown) => number;
  describe: (count: number) => string;
};

/**
 * One honest number per system rail. Each query key matches the owning
 * feature exactly (approvals desk, plugin proposals, diagnostics runtime), so
 * acting inside the page refreshes the rail without a second request cycle.
 * A rail with no data — route missing, Runtime down — stays quiet instead of
 * inventing a zero-risk story.
 */
const systemRailContracts: Partial<Record<PawSystemAppId, SystemRailContract>> = {
  'system-settings': {
    pageId: 'approvals',
    tone: 'decision',
    // Matches the approvals feature key ['approvals', 'all'].
    queryKey: ['approvals', 'all'],
    read: (transport, signal) => transport.request({
      pathId: 'agent.approvals.list',
      query: { limit: 500 },
      signal,
    }),
    count: pendingApprovalCount,
    describe: (count) => `${count} 项待处理`,
  },
  'app-center': {
    pageId: 'proposals',
    tone: 'decision',
    queryKey: pluginQueryKeys.proposals(),
    read: (transport, signal) => transport.request({ pathId: 'agent.extensions.proposals', signal }),
    count: (value) => arrayRecords(asRecord(value).items).length,
    describe: (count) => `${count} 项待确认`,
  },
  'system-monitor': {
    pageId: 'diagnostics',
    tone: 'attention',
    queryKey: diagnosticsQueryKeys.runtime(),
    read: (transport, signal) => transport.request({ pathId: 'diagnostics.runtime', signal }),
    count: componentAttentionCount,
    describe: (count) => `${count} 项需要检查`,
  },
};

function useSystemRailSignal(appId: PawSystemAppId): {
  count: number;
  describe: (count: number) => string;
  pageId: string;
  tone: 'decision' | 'attention';
} | null {
  const transport = useControlTransport();
  const contract = systemRailContracts[appId];
  const signalQuery = useQuery({
    queryKey: contract?.queryKey ?? ['paw-system-rail', appId],
    queryFn: ({ signal }) => contract
      ? contract.read(transport, signal)
      : Promise.resolve(null),
    enabled: Boolean(contract),
    refetchInterval: 60_000,
    retry: false,
  });
  if (!contract) return null;
  return {
    count: contract.count(signalQuery.data),
    describe: contract.describe,
    pageId: contract.pageId,
    tone: contract.tone,
  };
}

function pendingApprovalCount(value: unknown): number {
  const items = asRecord(value).items;
  if (!Array.isArray(items)) return 0;
  return items.filter((item) => {
    const approval = asRecord(item);
    return approval.schemaVersion === 'rag-ime.agent-approval.v1' && approval.state === 'pending';
  }).length;
}

/**
 * Mirrors the diagnostics service list: any component that does not report
 * ok=true is worth a check. The count never distinguishes "broken" from
 * "awaiting foreground verification" — that judgement belongs to the page.
 */
function componentAttentionCount(value: unknown): number {
  return Object.values(asRecord(asRecord(value).components))
    .filter((component) => asRecord(component).ok !== true)
    .length;
}

function systemPageForRoute(pages: readonly SystemPage[], route: string): SystemPage {
  const internalPages = pages.filter((page) => !page.external);
  const exact = internalPages.find((page) => page.route === route);
  if (exact) return exact;
  const view = new URLSearchParams(route.split('?', 2)[1] ?? '').get('view');
  if (view) {
    const byView = internalPages.find((page) => page.id === view);
    if (byView) return byView;
  }
  const path = route.split('?', 1)[0];
  return internalPages.find((page) => page.route.split('?', 1)[0] === path) ?? internalPages[0];
}

function openStandalonePage(path: string): void {
  const target = new URL(path, window.location.origin).toString();
  window.open(target, '_blank', 'noopener,noreferrer');
}

function thinkingLabel(value: string): string {
  return ({ off: '关闭', minimal: '极低', low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高' } as Record<string, string>)[value] ?? value;
}

function packageCatalogBadge(item: Record<string, unknown>): { label: string; tone: 'success' | 'warning' | 'neutral' } {
  if (item.updateAvailable === true) return { label: '有更新', tone: 'warning' };
  if (item.installed === true) return { label: '已安装', tone: 'success' };
  return { label: item.actionable === true ? '可安装' : '仅供审阅', tone: 'neutral' };
}

function packageActionLabel(action: string): string {
  if (action === 'install') return '安装 Package';
  if (action === 'update') return '更新 Package';
  return '更改 Package';
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function asError(error: unknown): Error | null {
  return error instanceof Error ? error : error ? new Error('暂时无法读取这部分内容。') : null;
}
