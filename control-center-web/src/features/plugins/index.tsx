import {
  Boxes,
  CheckCircle2,
  ChevronRight,
  CircleArrowUp,
  Clock3,
  ExternalLink,
  History,
  MessageCircle,
  PackageCheck,
  PackageX,
  PanelRightClose,
  Power,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  ShieldCheck,
  ShieldAlert,
  ShieldQuestion,
  Sparkles,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { presentSkill } from './skill-presentation';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Button,
  Disclosure,
  EmptyState,
  Field,
  IconButton,
  Input,
  SegmentedControl,
  Select,
  Switch,
} from '@/components/primitives';
import {
  InlineNotice,
  ManagementPage,
  ManagementSection,
  MetricStrip,
  QueryState,
  StatusBadge,
  arrayRecords,
  asRecord,
  publicErrorText,
  stringValue,
} from '@/features/overview/management-ui';
import {
  capabilityEffectiveLabel,
  capabilityKindLabel,
  capabilityPreferenceOptions,
  capabilityRiskLabel,
  capabilityScopeLabel,
  capabilityStatusLabel,
  preferenceLabel,
  type CapabilityCatalogItem,
  type CapabilityDefaultsSnapshot,
  type CapabilityKind,
  type CapabilityMutationOutcome,
  type CapabilityPreference,
} from './capability-policy';
import { usePluginCatalog } from './api';
import { PluginStudio } from './PluginStudio';
import { useProductIdentity } from '@/features/identity/product-identity';
import { openPawOsRoute, usePawOsAppActive, usePawOsAppIdentity, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { extensionAppInstallationMatches } from '@/paw-os/extensions/installation';
import { extensionAppForPackage } from '@/paw-os/extensions/registry';
import type { PawExtensionAppManifest } from '@/paw-os/extensions/types';
import { PawAppIcon } from '@/paw-os/shell/PawAppIcon';
import { usePageVisibility } from '@/platform/use-page-visibility';
import './plugins.css';

type ToolRecord = CapabilityCatalogItem;
type KindFilter = 'all' | CapabilityKind;
type DefaultMutationOutcome = CapabilityMutationOutcome & { scope: 'global' | 'project' };
type AvailabilityFilter = 'all' | 'online' | 'attention' | 'enabled' | 'disabled';
type LifecycleReceipt = { summary: string; evidence: string };

const kindFilters: readonly { label: string; value: KindFilter }[] = [
  { label: '全部', value: 'all' },
  { label: '工具', value: 'tool' },
  { label: '技能', value: 'skill' },
  { label: '扩展', value: 'extension' },
];


const operationLabels: Record<string, string> = {
  abort: '停止任务', apply_settings: '应用输入设置', artifact: '查看任务产物', audit: '查看审计记录',
  cache_stats: '查看缓存状态', candidate_explain: '解释候选词', capabilities: '查看可用能力', catalog: '浏览目录',
  create_package: '创建 Pi Package', validate: '检查安装来源', propose_install: '提交安装提议',
  propose_enable: '提交启用提议', propose_disable: '提交停用提议', propose_update: '提交更新提议',
  propose_rollback: '提交回滚提议', propose_uninstall: '提交卸载提议',
  components: '检查运行组件', dashboard: '查看规划面板', deep_recall: '深度检索', delegate: '委派任务',
  diagnose: '运行诊断', export: '导出备份', export_preview: '预览备份', get_settings: '查看输入设置',
  health: '检查服务状态', history: '查看历史记录', lexicon_apply: '应用词库更新', lexicon_review: '审阅词库建议',
  lexicon_rollback: '撤销词库更新', list: '浏览内容', maintenance_apply: '应用记忆整理',
  curation_prepare: '生成记忆草案', maintenance_preview: '预览记忆整理', maintenance_review: '审阅记忆整理', maintenance_rollback: '撤销记忆整理',
  maintenance_status: '查看整理状态', pause_ai: '暂停智能功能', privacy_policy: '查看隐私保护', probe: '检查模型连接',
  profile: '查看输入方案', profiles: '查看模型方案', profile_apply: '应用模型方案', profile_preview: '预览模型方案',
  profile_rollback: '撤销模型方案', provider_apply: '切换语音服务', provider_preview: '预览语音切换',
  provider_rollback: '撤销语音切换', provider_status: '查看语音服务', read: '读取内容', recall: '检索知识',
  recent: '查看最近内容', recent_activity: '查看最近活动', redeploy_rime: '重新部署输入法', restart_predictor: '重启预测服务',
  restart_sidecar: '重新连接本机补全服务', restore_apply: '恢复备份', restore_preview: '预览恢复内容', resume_ai: '恢复智能功能',
  rollback_settings: '撤销输入设置', route_status: '检查检索连接', run: '运行受控命令', search: '搜索内容',
  status: '查看当前状态', task_action: '更新任务', trace: '查看来源链路', undo_task_event: '撤销任务更新',
};

// An operation token is meaningful within its Tool contract. Browser `wait`
// and Room `wait` do different jobs, even though the token is the same.
const toolOperationLabels: Record<string, Record<string, string>> = {
  'tool:room_partner': {
    list: '查看伙伴与分工', add_participant: '添加协作伙伴', remove_participant: '移除协作伙伴',
    delegate: '分派工作', delegate_batch: '批量分派工作', retry: '重试已分派工作',
    accept: '验收工作成果', return: '退回工作成果', collect: '收集伙伴结果', wait: '等待伙伴结果',
    post: '发布协作进展与结果', peer_list: '查看可联系的伙伴', peer_send: '向伙伴发送消息',
    peer_ask: '向伙伴提问', peer_reply: '回复伙伴',
  },
  'tool:browser': {
    status: '查看浏览器状态', tabs: '查看浏览器标签页', snapshot: '读取页面快照', screenshot: '获取页面截图',
    trace: '查看浏览器操作记录', run: '执行网页操作脚本', navigate: '打开网页', back: '后退一页', forward: '前进一页',
    click: '点击页面元素', type: '向页面输入', scroll: '滚动页面', wait: '等待页面内容', stop: '停止浏览器操作',
  },
};

export function PluginsFeature() {
  const navigate = useNavigate();
  const identity = useProductIdentity();
  const appSurface = usePawOsAppIdentity();
  const surfaceActive = usePawOsAppActive();
  const pageVisible = usePageVisibility();
  const desktop = usePawOsDesktop();
  const [searchParams] = useSearchParams();
  const sessionContextId = searchParams.get('sessionId')?.trim() ?? '';
  const packageContextId = searchParams.get('packageId')?.trim() ?? '';
  const skillsView = searchParams.get('view') === 'skills';
  const [skillQuery, setSkillQuery] = useState('');
  const [skillSource, setSkillSource] = useState<'all' | 'package' | 'bundled' | 'project'>('all');
  const [skillStatus, setSkillStatus] = useState<'all' | 'enabled' | 'disabled' | 'inspect_only'>('all');
  const [selectedSkillId, setSelectedSkillId] = useState('');
  const {
    catalog,
    defaults,
    installed,
    skills,
    skill,
    versions,
    proposals,
    lifecycle,
    validate,
    preview,
    apply,
    updateDefaults,
    updateProjectDefaults,
    updateLifecycle,
    refreshAll,
  } = usePluginCatalog(
    sessionContextId,
    (surfaceActive ?? true) && pageVisible,
    skillsView,
    selectedSkillId,
  );
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<KindFilter>('all');
  const [availability, setAvailability] = useState<AvailabilityFilter>('all');
  const [selectedId, setSelectedId] = useState(searchParams.get('capability') ?? '');
  useEffect(() => {
    const requested = searchParams.get('capability');
    if (requested) { setSelectedId(requested); setQuery(''); setAvailability('all'); setKind('all'); }
  }, [searchParams]);
  const selectedTriggerRef = useRef<HTMLButtonElement | null>(null);
  const selectedSkillTriggerRef = useRef<HTMLButtonElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const skillSearchRef = useRef<HTMLInputElement | null>(null);
  const [enableAfterInstall, setEnableAfterInstall] = useState(true);
  const [packageSource, setPackageSource] = useState('');
  const [validation, setValidation] = useState<Record<string, unknown>>({});
  const [pendingChange, setPendingChange] = useState<Record<string, unknown>>({});
  const [studioPending, setStudioPending] = useState(false);
  const [lifecycleReceipt, setLifecycleReceipt] = useState<LifecycleReceipt>();
  const [lifecycleError, setLifecycleError] = useState('');
  const [defaultMutation, setDefaultMutation] = useState<DefaultMutationOutcome>();
  const [hookError, setHookError] = useState('');
  const [showMaintenance, setShowMaintenance] = useState(Boolean(packageContextId));
  const nativeAppCenter = appSurface?.appId === 'app-center';
  const nativePage = searchParams.get('view') === 'capabilities' ? 'capabilities' : skillsView ? 'skills' : searchParams.get('view') === 'studio' ? 'studio' : searchParams.get('view') === 'proposals' ? 'proposals' : 'installed';
  const items = catalog.data?.items ?? [];
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('zh-CN');
    return items.filter((item) => {
      const state = item.status.toLowerCase();
      const haystack = [
        publicCapabilityDisplayName(item),
        item.displayName,
        item.canonicalId,
        publicCapabilityDescription(item),
        item.description,
        publicCapabilitySourceLabel(item.source.label),
        item.source.label,
        capabilityKindLabel(item.kind),
        ...item.requiredPermissions.map(publicCapabilityPermissionLabel),
        ...item.requiredPermissions,
      ].join(' ').toLocaleLowerCase('zh-CN');
      const matchesAvailability = availability === 'all'
        || (availability === 'enabled' || availability === 'disabled'
          ? item.disclosure.effective === availability && !capabilityNeedsRoomContext(item)
          : availability === 'online' ? ['online', 'ready', 'installed'].includes(state)
            : !['online', 'ready', 'installed'].includes(state));
      return (!needle || haystack.includes(needle))
        && (kind === 'all' || item.kind === kind)
        && matchesAvailability;
    }).sort((left, right) => capabilityNameRank(left, needle) - capabilityNameRank(right, needle));
  }, [availability, items, kind, query]);
  const selected = filtered.find((item) => itemKey(item) === selectedId);
  useEffect(() => {
    if (surfaceActive === false || !(skillsView ? selectedSkillId : selectedId)) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      event.preventDefault();
      if (skillsView) {
        setSelectedSkillId('');
        selectedSkillTriggerRef.current?.focus();
      } else {
        setSelectedId('');
        selectedTriggerRef.current?.focus();
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [selectedId, selectedSkillId, skillsView, surfaceActive]);
  const installedSnapshot = asRecord(installed.data);
  const installedItems = arrayRecords(installedSnapshot.items);
  const pluginRuntimeAvailable = installedSnapshot.runtimeAvailable !== false;
  const skillSnapshot = asRecord(skills.data);
  const skillRuntimeAvailable = skillSnapshot.runtimeAvailable !== false;
  const pluginActionBlockedReason = installed.error
    ? '暂时无法确认插件状态，请刷新后再试。已填写的内容会保留。'
    : installed.data === undefined
      ? '正在读取插件状态，请稍后再试。'
      : !pluginRuntimeAvailable || (skillsView && !skillRuntimeAvailable)
        ? '插件服务暂未连接。恢复连接后刷新即可继续，已填写的内容会保留。'
        : '';
  const pluginOperationsAvailable = !pluginActionBlockedReason;
  // Validation can span a refresh. Read current availability before dispatch,
  // rather than letting an older render submit a preview after disconnection.
  const pluginActionBlockedReasonRef = useRef(pluginActionBlockedReason);
  pluginActionBlockedReasonRef.current = pluginActionBlockedReason;
  const proposalItems = arrayRecords(asRecord(proposals.data).items);
  const versionItems = arrayRecords(asRecord(versions.data).items);
  const lifecyclePolicies = arrayRecords(asRecord(lifecycle.data).policies);
  const lifecycleEvents = arrayRecords(asRecord(lifecycle.data).recentEvents);
  const availableCount = items.filter((item) => ['online', 'ready', 'installed'].includes(item.status.toLowerCase())).length;
  const disclosedCount = items.filter((item) => item.disclosure.state === 'disclosed').length;
  const hiddenCount = items.filter((item) => item.disclosure.state === 'hidden' && !capabilityNeedsRoomContext(item)).length;
  const roomContextCount = items.filter(capabilityNeedsRoomContext).length;
  const pendingSummary = asRecord(pendingChange.summary);
  const pendingResources = asRecord(pendingSummary.resources);
  const pendingResourceCount = packageResourceCount(pendingResources);
  const pendingSource = asRecord(pendingSummary.source);
  const validatedExtension = asRecord(validation.extension);
  const validatedResourceCount = packageResourceCount(validatedExtension.resources);
  const pendingPluginId = stringValue(pendingSummary.pluginId);
  const pendingSummaryDisplayName = stringValue(pendingSummary.displayName);
  const pendingInstalledPlugin = installedItems.find(
    (plugin) => stringValue(plugin.id) === pendingPluginId,
  );
  const pendingDisplayName = pendingSummaryDisplayName
    || stringValue(pendingInstalledPlugin?.displayName, pendingPluginId);
  const pendingCanonicalEvidence = !pendingSummaryDisplayName
    && pendingDisplayName !== pendingPluginId
    ? `标识：${pendingPluginId} · `
    : '';
  const lifecyclePending = studioPending || validate.isPending || preview.isPending || apply.isPending;
  // The native App Center scopes each console to its own resources; the web
  // maintenance section keeps folding proposals into the same read state.
  const packagesPending = nativeAppCenter
    ? installed.isPending || versions.isPending
    : installed.isPending || versions.isPending || proposals.isPending;
  const packagesError = nativeAppCenter
    ? firstError(installed.error, versions.error)
    : firstError(installed.error, versions.error, proposals.error);
  const retryPackages = () => void Promise.all([
    installed.refetch(),
    versions.refetch(),
    ...(nativeAppCenter ? [] : [proposals.refetch()]),
  ]);
  const refreshing = catalog.isFetching || installed.isFetching || versions.isFetching
    || proposals.isFetching || lifecycle.isFetching;

  const defaultSettingsPending = updateDefaults.isPending || updateProjectDefaults.isPending;

  const updateDefaultPreference = async (
    item: CapabilityCatalogItem,
    preference: CapabilityPreference,
    snapshot: CapabilityDefaultsSnapshot | undefined = defaults.data,
  ) => {
    if (!snapshot) return;
    setDefaultMutation({
      canonicalId: item.canonicalId,
      preference,
      scope: 'global',
      status: 'pending',
      message: `正在保存 ${publicCapabilityDisplayName(item)} 的所有对话默认。`,
    });
    try {
      await updateDefaults.mutateAsync({
        expectedRevision: snapshot.revision,
        preferences: {
          ...snapshot.preferences,
          [item.canonicalId]: preference,
        },
      });
      setDefaultMutation({
        canonicalId: item.canonicalId,
        preference,
        scope: 'global',
        status: 'succeeded',
        message: `所有对话默认已保存：${preferenceLabel(preference)}。`,
      });
    } catch (error) {
      setDefaultMutation({
        canonicalId: item.canonicalId,
        preference,
        scope: 'global',
        status: 'failed',
        message: publicErrorText(error, '默认设置没有保存，请重新读取后重试。'),
      });
    }
  };
  const updateProjectPreference = async (
    item: CapabilityCatalogItem,
    preference: CapabilityPreference,
    snapshot: CapabilityDefaultsSnapshot | undefined = defaults.data,
  ) => {
    const projectId = catalog.data?.projectScope.projectId;
    if (!snapshot || !projectId) return;
    setDefaultMutation({
      canonicalId: item.canonicalId,
      preference,
      scope: 'project',
      status: 'pending',
      message: `正在保存 ${publicCapabilityDisplayName(item)} 的当前项目默认。`,
    });
    try {
      await updateProjectDefaults.mutateAsync({
        expectedRevision: snapshot.revision,
        projectPreferences: {
          ...snapshot.projectPreferences,
          [projectId]: {
            ...snapshot.projectPreferences[projectId],
            [item.canonicalId]: preference,
          },
        },
      });
      setDefaultMutation({
        canonicalId: item.canonicalId,
        preference,
        scope: 'project',
        status: 'succeeded',
        message: `当前项目默认已保存：${preferenceLabel(preference)}。`,
      });
    } catch (error) {
      setDefaultMutation({
        canonicalId: item.canonicalId,
        preference,
        scope: 'project',
        status: 'failed',
        message: publicErrorText(error, '项目默认没有保存，请重新读取后重试。'),
      });
    }
  };
  const retryDefaultMutation = async () => {
    if (!defaultMutation) return;
    const refreshed = await defaults.refetch();
    if (!refreshed.data) {
      setDefaultMutation({
        ...defaultMutation,
        status: 'failed',
        message: '无法重新读取最新配置；未重发这次更改。',
      });
      return;
    }
    const item = items.find((candidate) => candidate.canonicalId === defaultMutation.canonicalId);
    if (!item) {
      setDefaultMutation({
        ...defaultMutation,
        status: 'failed',
        message: '这项能力已不在可用目录中；旧设置没有再次发送。',
      });
      return;
    }
    if (defaultMutation.scope === 'global') {
      await updateDefaultPreference(item, defaultMutation.preference, refreshed.data);
      return;
    }
    await updateProjectPreference(item, defaultMutation.preference, refreshed.data);
  };
  // All entry points share the same approval lifecycle. Source preparation
  // supplies only the reviewed request; this owner publishes its preview.
  const prepareLifecyclePreview = async (
    prepare: () => Promise<Parameters<typeof preview.mutateAsync>[0]>,
  ) => {
    if (pluginActionBlockedReasonRef.current) {
      setLifecycleError(pluginActionBlockedReasonRef.current);
      return;
    }
    setLifecycleError('');
    setLifecycleReceipt(undefined);
    setPendingChange({});
    setValidation({});
    try {
      const request = await prepare();
      if (pluginActionBlockedReasonRef.current) throw new Error(pluginActionBlockedReasonRef.current);
      setPendingChange(asRecord(await preview.mutateAsync(request)));
    } catch (error) {
      setLifecycleError(errorMessage(error));
    }
  };

  const validatePreviewSource = async (source: Parameters<typeof validate.mutateAsync>[0]) => {
    const result = asRecord(await validate.mutateAsync(source));
    setValidation(result);
    return result;
  };

  const previewInstalledAction = (action: 'enable' | 'disable' | 'uninstall' | 'rollback', pluginId: string) => (
    prepareLifecyclePreview(async () => ({ action, pluginId }))
  );

  const previewCatalogAction = (item: Record<string, unknown>) => (
    prepareLifecyclePreview(async () => {
      const validationResult = await validatePreviewSource({
        catalogId: stringValue(item.id), catalogVersion: stringValue(item.latestVersion),
      });
      return {
        action: item.updateAvailable === true ? 'update' : 'install',
        validationToken: stringValue(validationResult.validationToken),
        enable: item.installed === true ? item.enabled === true : enableAfterInstall,
      };
    })
  );

  const previewInstalledUpdate = (
    plugin: Record<string, unknown>,
    catalogItem: Record<string, unknown>,
  ) => prepareLifecyclePreview(async () => {
    const validationResult = await validatePreviewSource({
      catalogId: stringValue(catalogItem.id), catalogVersion: stringValue(catalogItem.latestVersion),
    });
    return {
      action: 'update',
      validationToken: stringValue(validationResult.validationToken),
      enable: plugin.enabled === true,
    };
  });

  const previewPackageSource = () => prepareLifecyclePreview(async () => {
    const source = packageSource.trim();
    if (!source) {
      throw new Error('请输入 npm 包、Git 地址或本地 Pi Package 目录。');
    }
    const validationResult = await validatePreviewSource({ packageSource: source });
    const extension = asRecord(validationResult.extension);
    const pluginId = stringValue(extension.id);
    const installedPackage = installedItems.find((item) => stringValue(item.id) === pluginId);
    return {
      action: installedPackage ? 'update' : 'install',
      validationToken: stringValue(validationResult.validationToken),
      enable: installedPackage ? installedPackage.enabled === true : enableAfterInstall,
    };
  });

  const applyPendingChange = async () => {
    if (pluginActionBlockedReasonRef.current) {
      setLifecycleError(pluginActionBlockedReasonRef.current);
      return;
    }
    setLifecycleError('');
    const confirmedSummary = `${pluginActionLabel(stringValue(pendingSummary.action))}：${publicPluginDisplayName(pendingDisplayName)}`;
    try {
      const response = asRecord(await apply.mutateAsync({
        previewToken: stringValue(pendingChange.previewToken),
        payloadSha256: stringValue(pendingChange.payloadSha256),
        confirmText: 'apply',
      }));
      const receiptId = stringValue(asRecord(response.receipt).receiptId);
      setLifecycleReceipt({
        summary: confirmedSummary,
        evidence: `回执 ${receiptId}`,
      });
      setPendingChange({});
      setValidation({});
    } catch (error) {
      // The backend consumes the token before dispatch. Never offer to replay
      // an operation whose outcome may be unknown after a connection failure.
      setPendingChange({});
      setValidation({});
      setLifecycleError(`${errorMessage(error)} 请先刷新安装状态；如仍需更改，重新预览后再确认。`);
    }
  };

  const updateHook = async (eventType: string, enabled: boolean) => {
    setHookError('');
    try {
      await updateLifecycle.mutateAsync({ eventType, enabled });
    } catch (error) {
      setHookError(errorMessage(error));
    }
  };

  const availableUpdateFor = (pluginId: string) => versionItems.find(
    (item) => stringValue(item.id) === pluginId && item.updateAvailable === true,
  );
  const skillItems = arrayRecords(skillSnapshot.items);
  const filteredSkills = useMemo(() => {
    const needle = skillQuery.trim().toLocaleLowerCase('zh-CN');
    return skillItems.filter((item) => {
      const presentation = presentSkill(item);
      const haystack = [
        presentation.name,
        presentation.description,
        skillSourceLabel(stringValue(item.sourceKind)),
        stringValue(item.skillId),
        stringValue(item.name),
        stringValue(item.description),
        stringValue(item.sourceKind),
        stringValue(item.packageId),
        stringValue(item.packageVersion),
        stringValue(item.resourcePath),
        stringValue(item.installState),
        stringValue(item.contentRevision),
      ].join(' ').toLocaleLowerCase('zh-CN');
      const matchesStatus = skillStatus === 'all'
        || (skillStatus === 'inspect_only'
          ? stringValue(item.management) === 'inspect_only'
          : skillStatus === 'enabled'
            ? item.enabled === true
            : item.enabled === false);
      return (!needle || haystack.includes(needle))
        && (skillSource === 'all' || stringValue(item.sourceKind) === skillSource)
        && matchesStatus;
    });
  }, [skillItems, skillQuery, skillSource, skillStatus]);
  const selectedSkill = filteredSkills.find((item) => stringValue(item.skillId) === selectedSkillId);
  const selectedSkillDetail = asRecord(asRecord(skill.data).item);
  const selectedSkillPackage = selectedSkill && stringValue(selectedSkill.packageId)
    ? installedItems.find((item) => stringValue(item.id) === stringValue(selectedSkill.packageId))
    : undefined;
  const selectedSkillUpdate = selectedSkillPackage
    ? availableUpdateFor(stringValue(selectedSkillPackage.id))
    : undefined;
  const skillCountLabel = skills.error ? '暂时无法读取' : skills.data === undefined ? '读取中' : !skillRuntimeAvailable ? '状态未知' : `${filteredSkills.length} 项`;

  /* -- Shared building blocks. Web keeps the management-sheet sections; the
     native App Center composes the same blocks into purpose cards without
     repeating the window title or the left navigation labels. ------------- */

  const capabilityOverviewBlock = (
    <>
      <p className="plugins-capability-counts">{items.length} 项功能 · {disclosedCount} 项已启用 · {hiddenCount} 项已关闭 · {availableCount} 项连接正常{roomContextCount ? ` · ${roomContextCount} 项需进入 Room` : ''}</p>
      <div className="plugins-settings-scope">
        <div><strong>{sessionContextId ? '正在查看当前对话的功能设置' : '正在设置所有对话的默认功能'}</strong>
          <p>对话单独设置优先，其次是项目设置，最后是所有对话默认。更改从下一轮生效。</p>
        </div>
        {sessionContextId ? <Button onClick={() => openPawOsRoute(desktop, `/agent?session=${encodeURIComponent(sessionContextId)}&tools=open&toolsRequest=${Date.now()}`)} size="small" variant="quiet">调整本对话开关</Button> : null}
      </div>
      <div className="capability-policy-feedback">
        {defaults.error ? (
          <InlineNotice title="默认设置暂时无法读取" tone="danger">
            当前目录仍可查看，但不会猜测默认值，也不会发送修改。
            <Button onClick={() => void defaults.refetch()} size="small" variant="quiet">重试默认设置</Button>
          </InlineNotice>
        ) : null}
        {defaultMutation?.status === 'pending' ? (
          <InlineNotice title="正在保存默认设置" tone="info">{defaultMutation.message}</InlineNotice>
        ) : null}
        {defaultMutation?.status === 'succeeded' ? (
          <InlineNotice title="默认设置已保存" tone="success">{defaultMutation.message}</InlineNotice>
        ) : null}
        {defaultMutation?.status === 'failed' ? (
          <InlineNotice title="默认设置没有保存" tone="danger">
            {defaultMutation.message}
            <Button disabled={defaultSettingsPending} onClick={retryDefaultMutation} size="small" variant="quiet">重试这次更改</Button>
          </InlineNotice>
        ) : null}
      </div>
    </>
  );

  const capabilityBrowseBlock = (
    <>
      <div className="plugins-filters">
        <Field className="plugins-search" htmlFor="plugin-search" label="搜索">
          <Input id="plugin-search" ref={searchRef} onChange={(event) => setQuery(event.target.value)} placeholder="名称、用途、来源或权限" value={query} />
        </Field>
        <Field htmlFor="plugin-availability" label="状态">
          <Select
            id="plugin-availability"
            onValueChange={setAvailability}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'enabled', label: '已启用' },
              { value: 'disabled', label: '已关闭' },
              { value: 'online', label: '连接正常' },
              { value: 'attention', label: '需要处理' },
            ]}
            value={availability}
          />
        </Field>
        <div className="plugins-mode-filter"><span>能力类型</span><SegmentedControl aria-label="能力类型筛选" items={kindFilters} onValueChange={setKind} value={kind} /></div>
      </div>

      {filtered.length ? (
        <div className="plugins-browser" data-detail-open={Boolean(selected)}>
          <div aria-label="能力列表" className="plugins-list" role="group">
            {filtered.map((item) => {
              const id = itemKey(item);
              const selectedItem = id === selectedId;
              return (
                <button aria-pressed={selectedItem} className="plugins-list__item" data-selected={selectedItem || undefined} key={id} onClick={(event) => { selectedTriggerRef.current = event.currentTarget; setSelectedId(id); }} type="button">
                  <span className="plugins-list__copy">
                    <small>{publicCapabilitySourceLabel(item.source.label)} · {capabilityKindLabel(item.kind)}</small>
                    <strong>{publicCapabilityDisplayName(item)}</strong>
                    <span>{publicCapabilityDescription(item)}</span>
                  </span>
                  <span className="plugins-list__aside">
                    <StatusBadge {...availabilityBadge(item)} />
                    {!capabilityNeedsRoomContext(item) ? <>
                      <StatusBadge label={capabilityEffectiveLabel(item.disclosure.effective)} tone={item.disclosure.effective === 'enabled' ? 'success' : 'neutral'} />
                      <small>{capabilityScopeLabel(item.effectiveScope)}</small>
                    </> : null}
                    <ChevronRight aria-hidden="true" size={15} />
                  </span>
                </button>
              );
            })}
          </div>
          {selected ? (
            <ToolDetail
              assistantName={identity.assistantName}
              defaultPending={defaultSettingsPending}
              projectPending={defaultSettingsPending}
              projectPreference={
                catalog.data?.projectScope.projectId
                  ? defaults.data?.projectPreferences[catalog.data.projectScope.projectId]?.[selected.canonicalId] ?? 'inherit'
                  : 'inherit'
              }
              projectAvailable={Boolean(defaults.data && catalog.data?.projectScope.projectId)}
              projectOwnerId={catalog.data?.projectScope.projectId}
              defaultPreference={defaults.data?.preferences[selected.canonicalId] ?? 'inherit'}
              defaultsAvailable={Boolean(defaults.data)}
              item={selected}
              sessionOwnerId={catalog.data?.sessionPolicy?.sessionId}
              sessionPreference={catalog.data?.sessionPolicy?.disclosurePreferences.session[selected.canonicalId] ?? 'inherit'}
              onClose={() => { setSelectedId(''); selectedTriggerRef.current?.focus(); }}
              onDefaultPreferenceChange={(preference) => void updateDefaultPreference(selected, preference)}
              onProjectPreferenceChange={(preference) => void updateProjectPreference(selected, preference)}
            />
          ) : (
            <aside aria-label="能力详情占位" className="plugins-detail plugins-detail--empty">
              <Wrench aria-hidden="true" size={20} />
              <strong>选择一项能力查看详情</strong>
              <span>这里会显示用途、可用状态、风险提示，以及对话和项目设置。</span>
            </aside>
          )}
        </div>
      ) : <EmptyState
        action={query || kind !== 'all' || availability !== 'all' ? <Button onClick={() => { setQuery(''); setKind('all'); setAvailability('all'); searchRef.current?.focus(); }} size="small" variant="quiet">清除筛选</Button> : undefined}
        description={items.length ? '换一个关键词或清除筛选，查看全部能力。' : '当前没有可用的技能或工具。'}
        icon={Search}
        title="没有找到能力"
      />}
    </>
  );

  const runtimeNotice = !pluginRuntimeAvailable ? (
    <InlineNotice title="插件服务暂未连接" tone="warning">
      暂时无法确认已安装状态。恢复连接后刷新即可继续，填写的来源会保留。
      <Button loading={installed.isFetching} onClick={retryPackages} size="small" variant="quiet">刷新状态</Button>
    </InlineNotice>
  ) : null;

  const packageStatusBadge = (
    <StatusBadge
      label={packagesError ? '暂时无法读取' : installed.data === undefined ? (installed.isFetching ? '正在读取' : '等待读取') : !pluginRuntimeAvailable ? '安装状态未知' : `${installedItems.length} 个已安装`}
      tone={packagesError || !pluginRuntimeAvailable ? 'warning' : 'neutral'}
    />
  );

  const sourceInstallBlock = (
    <div className="plugin-lifecycle__install">
      <Field htmlFor="pi-package-source" label="Pi Package 来源">
        <Input
          id="pi-package-source"
          onChange={(event) => setPackageSource(event.target.value)}
          placeholder="npm:@scope/package@1.2.3、Git URL 或本地目录"
          value={packageSource}
        />
      </Field>
      <Switch checked={enableAfterInstall} label="安装后立即启用" onCheckedChange={setEnableAfterInstall} />
      <Button
        disabled={!packageSource.trim() || lifecyclePending || !pluginOperationsAvailable}
        leadingIcon={<PackageCheck size={15} />}
        loading={validate.isPending || preview.isPending}
        onClick={() => void previewPackageSource()}
        size="small"
      >检查并预览</Button>
      {validatedExtension.id ? (
        <div className="plugin-lifecycle__validation">
          <StatusBadge label="Pi 已解析" tone="success" />
          <strong>{publicPluginDisplayName(stringValue(validatedExtension.displayName, stringValue(validatedExtension.id)))}</strong>
          <span>v{stringValue(validatedExtension.version)} · {validatedResourceCount} 项资源 · 新对话加载 Skill、Prompt 与主题</span>
        </div>
      ) : null}
    </div>
  );

  const catalogRowsBlock = (
    <div className="plugin-catalog" aria-label="受管插件目录">
      {versionItems.map((item) => {
        const id = stringValue(item.id);
        const extensionApp = extensionAppForPackage(id);
        const security = asRecord(item.security);
        const source = asRecord(item.source);
        const canOpenExtensionApp = Boolean(
          extensionApp
          && item.installed === true
          && item.enabled === true
          && extensionAppInstallationMatches(extensionApp, item),
        );
        return (
          <article className="plugin-catalog__row" key={id}>
            <span className="plugin-catalog__identity">
              {extensionApp ? <ExtensionAppIdentity app={extensionApp} /> : null}
              <strong>{publicPluginDisplayName(stringValue(item.displayName, id))}</strong>
              <small>
                {publicPluginSourceLabel(stringValue(item.publisher))} · {publicPluginSourceLabel(stringValue(source.label))}
                {extensionApp ? <ExtensionAppBadge /> : null}
              </small>
              <span>{stringValue(item.description)}</span>
            </span>
            <span className="plugin-catalog__facts">
              <span><ShieldCheck size={14} />需要的权限：{stringArray(item.permissions).map(publicPluginPermissionLabel).join('、') || '无额外权限'}</span>
              <span><History size={14} />v{stringValue(item.latestVersion, '未发布')} · {arrayRecords(item.versions).length} 个版本</span>
              <span><ShieldAlert size={14} />{stringValue(security.notes, '尚无安全说明')}</span>
            </span>
            <span className="plugin-catalog__action">
              <StatusBadge {...catalogStateBadge(item)} />
              {canOpenExtensionApp ? (
                <>
                  <Button
                    leadingIcon={<ExternalLink size={15} />}
                    onClick={() => openPawOsRoute(desktop, extensionApp!.route)}
                    size="small"
                  >打开 {extensionApp!.label}</Button>
                  {item.updateAvailable === true ? (
                    <Button
                      disabled={lifecyclePending || !pluginOperationsAvailable}
                      leadingIcon={<PackageCheck size={15} />}
                      loading={validate.isPending || preview.isPending}
                      onClick={() => void previewCatalogAction(item)}
                      size="small"
                      variant="quiet"
                    >查看更新内容</Button>
                  ) : null}
                </>
              ) : (
                <Button
                  disabled={item.actionable !== true || (item.installed === true && item.updateAvailable !== true) || lifecyclePending || !pluginOperationsAvailable}
                  leadingIcon={<PackageCheck size={15} />}
                  loading={validate.isPending || preview.isPending}
                  onClick={() => void previewCatalogAction(item)}
                  size="small"
                >{item.updateAvailable === true ? '查看更新内容' : item.installed === true ? '已安装' : item.actionable === true ? '查看安装内容' : '查看说明'}</Button>
              )}
            </span>
          </article>
        );
      })}
    </div>
  );

  const authoringCalloutBlock = (
    <div className="plugin-authoring-callout">
      <span className="plugin-authoring-callout__icon"><Sparkles aria-hidden="true" size={18} /></span>
      <span><strong>制作自己的 App 与插件</strong><small>让 Agent 制作完整应用，或自己编写技能和提示词。</small></span>
      <Button
        leadingIcon={<MessageCircle size={16} />}
        onClick={() => desktop ? openPawOsRoute(desktop, '/plugins?view=studio') : navigate('/plugins?view=studio')}
      >获取或制作能力</Button>
    </div>
  );

  const proposalsBlock = (withHeading: boolean) => proposalItems.length ? (
    <div className="plugin-lifecycle__proposals">
      {withHeading ? <h3>{identity.assistantName}的建议</h3> : null}
      {proposalItems.map((proposal) => {
        const summary = asRecord(proposal.summary);
        const proposalVersion = stringValue(summary.version);
        const proposalPermissions = stringArray(summary.permissions);
        return (
          <button className="plugin-proposal" disabled={lifecyclePending} key={stringValue(proposal.proposalId)} onClick={() => { setPendingChange(proposal); setLifecycleError(''); setLifecycleReceipt(undefined); }} type="button">
            <span>
              <strong>{publicPluginDisplayName(stringValue(summary.displayName, stringValue(summary.pluginId)))}</strong>
              <small>
                {pluginActionLabel(stringValue(summary.action))}
                {proposalVersion ? ` · v${proposalVersion}` : ''}
                {proposalPermissions.length ? ` · ${proposalPermissions.length} 项权限` : ' · 无额外权限'}
              </small>
            </span>
            <ChevronRight aria-hidden="true" size={16} />
          </button>
        );
      })}
    </div>
  ) : (
    <EmptyState
      action={<Button loading={proposals.isFetching} onClick={() => void proposals.refetch()} size="small">重新检查建议</Button>}
      description={`有明确用途和来源的新能力建议会由${identity.assistantName}放在这里，安装前仍需你的确认。`}
      icon={Sparkles}
      title="暂时没有新建议"
    />
  );

  const approvalBlock = pendingChange.previewToken ? (
    <section aria-label="待确认的插件更改" className="plugin-lifecycle__approval">
      <header className="plugin-lifecycle__approval-heading">
        <span>
          <small>等待你的批准</small>
          <strong>{pluginActionLabel(stringValue(pendingSummary.action))}：{publicPluginDisplayName(pendingDisplayName)}</strong>
        </span>
        <ol aria-label="生命周期进度" className="plugin-lifecycle__stages">
          <li data-state="done">检查来源</li>
          <li data-state="done">预览影响</li>
          <li aria-current="step" data-state="current">你的确认</li>
          <li data-state="todo">应用并出具回执</li>
        </ol>
      </header>
      <dl className="plugin-lifecycle__approval-facts">
        {pendingCanonicalEvidence ? <div><dt>标识</dt><dd>{pendingPluginId}</dd></div> : null}
        {stringValue(pendingSummary.version) ? <div><dt>版本</dt><dd>v{stringValue(pendingSummary.version)}</dd></div> : null}
        <div>
          <dt>需要的权限</dt>
          <dd>{stringArray(pendingSummary.permissions).length
            ? stringArray(pendingSummary.permissions).map(publicPluginPermissionLabel).join('、')
            : '无额外权限'}</dd>
        </div>
        {pendingResourceCount ? <div><dt>Pi 资源</dt><dd>{pendingResourceCount} 项 · 新对话加载 Skill、Prompt 与主题</dd></div> : null}
        {stringValue(pendingSource.kind) ? <div><dt>来源</dt><dd>{publicPluginSourceLabel(stringValue(pendingSource.kind))}</dd></div> : null}
        {stringValue(pendingSummary.recommendationReason)
          ? <div><dt>推荐理由</dt><dd>{stringValue(pendingSummary.recommendationReason)}</dd></div>
          : null}
        {stringArray(pendingSummary.dependencies).length
          ? <div><dt>运行依赖</dt><dd>{stringArray(pendingSummary.dependencies).join('、')}</dd></div>
          : null}
        {stringArray(pendingSummary.risks).length
          ? <div><dt>风险提示</dt><dd>{stringArray(pendingSummary.risks).join('；')}</dd></div>
          : null}
        {stringArray(pendingSummary.capabilityOverlap).length
          ? <div><dt>能力重复</dt><dd>{stringArray(pendingSummary.capabilityOverlap).join('；')}</dd></div>
          : null}
        {stringArray(pendingSummary.verificationPlan).length
          ? <div><dt>验证计划</dt><dd>{stringArray(pendingSummary.verificationPlan).join('；')}</dd></div>
          : null}
        {typeof pendingSummary.expectedEnabled === 'boolean'
          ? <div><dt>当前状态</dt><dd>{pendingSummary.expectedEnabled ? '已启用' : '已停用'}</dd></div>
          : null}
        {stringValue(pendingSummary.action) === 'uninstall'
          ? <div><dt>保留的数据</dt><dd>只移除这个 Pi Package 的受管资源；不会删除项目文件、对话、WorkDocument 或个人数据。</dd></div>
          : null}
      </dl>
      {!pluginOperationsAvailable ? <p>{pluginActionBlockedReason}</p> : null}
      <div className="plugin-lifecycle__approval-actions">
        <Button disabled={lifecyclePending} onClick={() => setPendingChange({})} size="small" variant="quiet">取消</Button>
        <Button disabled={lifecyclePending || !pluginOperationsAvailable} leadingIcon={<ShieldCheck size={16} />} loading={apply.isPending} onClick={() => void applyPendingChange()} size="small" variant="primary">确认更改</Button>
      </div>
    </section>
  ) : null;

  const receiptBlock = lifecycleReceipt && !pendingChange.previewToken ? (
    <InlineNotice title="更改已应用" tone="success">
      <div className="plugin-lifecycle__receipt">
        <span>
          {lifecycleReceipt.summary}
          <small>{lifecycleReceipt.evidence}</small>
        </span>
        <Button onClick={() => setLifecycleReceipt(undefined)} size="small" variant="quiet">知道了</Button>
      </div>
    </InlineNotice>
  ) : null;

  const errorBlock = lifecycleError
    ? <InlineNotice title="插件操作未完成" tone="danger">{lifecycleError}</InlineNotice>
    : null;
  const skillsBrowseBlock = (
    <div className="skills-surface">
      {!skillRuntimeAvailable ? (
        <InlineNotice title="技能清单暂时无法更新" tone="warning">
          技能服务暂未连接，数量和当前状态尚未确认。重新读取后可继续，搜索条件会保留。
          <Button loading={skills.isFetching} onClick={() => void skills.refetch()} size="small" variant="quiet">重新读取技能</Button>
        </InlineNotice>
      ) : null}
      <MetricStrip items={[
        { label: '可查看', value: skillRuntimeAvailable ? skillItems.length : '—', detail: '当前可发现的 Skill', icon: Sparkles },
        { label: '随插件管理', value: skillRuntimeAvailable ? skillItems.filter((item) => stringValue(item.management) === 'package').length : '—', detail: '按 Package 统一变更', icon: PackageCheck },
        { label: '仅查看', value: skillRuntimeAvailable ? skillItems.filter((item) => stringValue(item.management) === 'inspect_only').length : '—', detail: '随产品提供或来自当前项目', icon: ShieldQuestion },
      ]} />
      <div className="skills-filters">
        <Field className="plugins-search" htmlFor="skill-search" label="搜索">
          <Input
            id="skill-search"
            ref={skillSearchRef}
            onChange={(event) => setSkillQuery(event.target.value)}
            placeholder="名称、用途、Package 或路径"
            value={skillQuery}
          />
        </Field>
        <Field htmlFor="skill-source" label="来源">
          <Select
            id="skill-source"
            onValueChange={setSkillSource}
            options={[
              { value: 'all', label: '全部来源' },
              { value: 'package', label: '插件提供' },
              { value: 'bundled', label: '随产品提供' },
              { value: 'project', label: '当前项目' },
            ]}
            value={skillSource}
          />
        </Field>
        <Field htmlFor="skill-status" label="状态">
          <Select
            id="skill-status"
            onValueChange={setSkillStatus}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'enabled', label: '已启用' },
              { value: 'disabled', label: '已停用' },
              { value: 'inspect_only', label: '仅查看' },
            ]}
            value={skillStatus}
          />
        </Field>
      </div>
      {filteredSkills.length ? (
        <div className="plugins-browser skills-browser" data-detail-open={Boolean(selectedSkill)}>
          <div aria-label="Skill 列表" className="plugins-list" role="group">
            {filteredSkills.map((item) => {
              const id = stringValue(item.skillId);
              const presentation = presentSkill(item);
              const name = presentation.name;
              const isSelected = id === selectedSkillId;
              return (
                <button
                  aria-pressed={isSelected}
                  className="plugins-list__item skills-list__item"
                  data-selected={isSelected || undefined}
                  key={id}
                  onClick={(event) => { selectedSkillTriggerRef.current = event.currentTarget; setSelectedSkillId(id); }}
                  type="button"
                >
                  <span className="plugins-list__copy">
                    <small>{skillOriginSummary(item)}</small>
                    <strong>{name}</strong>
                    <span>{presentation.description || '没有提供用途说明。'}</span>
                  </span>
                  <span className="plugins-list__aside">
                    <StatusBadge {...skillStatusBadge(item)} />
                    <ChevronRight aria-hidden="true" size={15} />
                  </span>
                </button>
              );
            })}
          </div>
          {selectedSkill ? (
            <SkillDetail
              detail={selectedSkillDetail}
              detailError={asError(skill.error)}
              detailPending={skill.isPending}
              installedPackage={selectedSkillPackage}
              item={selectedSkill}
              lifecyclePending={lifecyclePending}
              lifecycleUnavailable={!pluginOperationsAvailable}
              onAction={(action) => void previewInstalledAction(action, stringValue(selectedSkill.packageId))}
              onClose={() => { setSelectedSkillId(''); selectedSkillTriggerRef.current?.focus(); }}
              onOpenScenes={() => desktop ? openPawOsRoute(desktop, '/plugins?view=scenes') : navigate('/plugins?view=scenes')}
              onRetry={() => void skill.refetch()}
              onUpdate={() => {
                if (selectedSkillPackage && selectedSkillUpdate) {
                  void previewInstalledUpdate(selectedSkillPackage, selectedSkillUpdate);
                }
              }}
              update={selectedSkillUpdate}
            />
          ) : (
            <aside aria-label="Skill 详情占位" className="plugins-detail plugins-detail--empty">
              <Sparkles aria-hidden="true" size={20} />
              <strong>选择一个 Skill 查看正文</strong>
              <span>这里会显示来源、Package 状态、内容修订和可读的 Skill 指令。</span>
            </aside>
          )}
        </div>
      ) : skillRuntimeAvailable ? (
        <EmptyState
          action={skillQuery || skillSource !== 'all' || skillStatus !== 'all' ? <Button onClick={() => { setSkillQuery(''); setSkillSource('all'); setSkillStatus('all'); skillSearchRef.current?.focus(); }} size="small" variant="quiet">清除筛选</Button> : undefined}
          description={skillItems.length ? '换一个关键词或清除筛选，查看全部技能。' : '当前没有发现随产品提供、来自项目或已安装插件的技能。'}
          icon={Sparkles}
          title="没有找到 Skill"
        />
      ) : null}
    </div>
  );


  const installedGridBlock = (
    <div className="plugin-lifecycle__installed">
      {installedItems.length ? installedItems.map((plugin) => {
        const pluginId = stringValue(plugin.id);
        const extensionApp = extensionAppForPackage(pluginId);
        const displayName = publicPluginDisplayName(stringValue(plugin.displayName, pluginId));
        const enabled = plugin.enabled === true;
        const canOpenExtensionApp = Boolean(
          extensionApp
          && enabled
          && extensionAppInstallationMatches(extensionApp, plugin),
        );
        const permissions = stringArray(plugin.permissions);
        const resourceCount = packageResourceCount(plugin.resources);
        const resourceSummary = packageResourceSummary(plugin.resources);
        const source = asRecord(plugin.source);
        const sourceKind = stringValue(source.kind);
        const description = publicInstalledPluginDescription(plugin);
        const previousVersion = stringValue(asRecord(plugin.rollbackTarget).version, stringValue(plugin.previousVersion));
        const rollbackReady = plugin.rollbackAvailable === true;
        const update = availableUpdateFor(pluginId);
        const usageSummary = asRecord(plugin.usage);
        const loadedSessionCount = numberValue(usageSummary.loadedSessionCount);
        const lastLoadedAtMs = numberValue(usageSummary.lastLoadedAtMs);
        const invocationCount = numberValue(usageSummary.invocationCount);
        const succeededCount = numberValue(usageSummary.succeededCount);
        const failedCount = numberValue(usageSummary.failedCount);
        const cancelledCount = numberValue(usageSummary.cancelledCount);
        const averageDurationMs = numberValue(usageSummary.averageDurationMs);
        const lastInvocation = asRecord(usageSummary.lastInvocation);
        const lastInvocationAtMs = numberValue(lastInvocation.completedAtMs || lastInvocation.startedAtMs);
        const lastInvocationResource = stringValue(lastInvocation.resourceName);
        const lastInvocationDurationMs = numberValue(lastInvocation.durationMs);
        const invoked = invocationCount > 0 && Boolean(lastInvocationResource);
        return (
          <article
            aria-label={`${displayName} Package`}
            className="installed-plugin"
            data-selected={pluginId === packageContextId || undefined}
            key={pluginId}
          >
            <div className="installed-plugin__identity">
              {extensionApp ? <ExtensionAppIdentity app={extensionApp} /> : null}
              <strong>{displayName}</strong>
              <small>
                <span>v{stringValue(plugin.version)}</span>
                {sourceKind ? <span>{publicPluginSourceLabel(sourceKind)}</span> : null}
                {extensionApp ? <ExtensionAppBadge /> : null}
              </small>
              {description ? <p className="installed-plugin__description">{description}</p> : null}
            </div>
            <span className="installed-plugin__state">
              {update ? <StatusBadge label="有更新" tone="warning" /> : null}
              <StatusBadge label={enabled ? '已启用' : '已停用'} tone={enabled ? 'success' : 'neutral'} />
              {loadedSessionCount > 0 ? <StatusBadge label={`已加载 · ${loadedSessionCount} 个对话`} tone="success" /> : null}
              {invoked ? <StatusBadge label={`已调用 · ${lastInvocationResource}`} tone="success" /> : null}
            </span>
            <ul className="installed-plugin__facts">
              <li><ShieldCheck aria-hidden="true" size={13} />{permissions.length ? permissions.map(publicPluginPermissionLabel).join('、') : '无额外权限'}</li>
              <li><Boxes aria-hidden="true" size={13} />{resourceCount ? `${resourceCount} 项资源` : '无附带资源'}</li>
              {resourceSummary ? <li><Boxes aria-hidden="true" size={13} />{resourceSummary}</li> : null}
              <li><History aria-hidden="true" size={13} />{rollbackReady ? (previousVersion ? `可恢复到 v${previousVersion}` : '可恢复上一版本，具体版本将在预览中显示') : '没有可恢复的历史版本'}</li>
            </ul>
            {Object.keys(usageSummary).length ? (
              <dl className="installed-plugin__usage">
                <div><dt>最近加载</dt><dd>{loadedSessionCount > 0 ? `${lastLoadedAtMs ? formatPluginTime(lastLoadedAtMs) : '时间未提供'} · ${loadedSessionCount} 个对话` : '尚无加载证据'}</dd></div>
                <div><dt>调用统计</dt><dd>{invocationCount > 0 ? `${invocationCount} 次调用 · 成功 ${succeededCount} · 失败 ${failedCount} · 取消 ${cancelledCount} · 平均 ${averageDurationMs} ms` : '尚无调用证据'}</dd></div>
                {invoked ? <div><dt>最近调用</dt><dd>{pluginInvocationStatusLabel(stringValue(lastInvocation.status))} · {lastInvocationDurationMs} ms{lastInvocationAtMs ? ` · ${formatPluginTime(lastInvocationAtMs)}` : ''} · {publicPluginResourceKindLabel(stringValue(lastInvocation.resourceKind))} {lastInvocationResource}</dd></div> : null}
              </dl>
            ) : null}
            <Disclosure className="installed-plugin__details" summary={<><ChevronRight aria-hidden="true" size={14} />包标识与原始信息</>}>
              <pre>{JSON.stringify({
                id: pluginId,
                displayName: plugin.displayName,
                description: plugin.description,
                version: plugin.version,
                source: { kind: source.kind, label: source.label, requested: source.requested },
                permissions,
              }, null, 2)}</pre>
            </Disclosure>
            <div className="installed-plugin__actions">
              {canOpenExtensionApp ? (
                <Button
                  leadingIcon={<ExternalLink size={15} />}
                  onClick={() => openPawOsRoute(desktop, extensionApp!.route)}
                  size="small"
                >打开 {extensionApp!.label}</Button>
              ) : null}
              {update ? (
                <Button
                  disabled={lifecyclePending || !pluginOperationsAvailable}
                  leadingIcon={<CircleArrowUp size={15} />}
                  loading={(validate.isPending || preview.isPending) && stringValue(validate.variables?.catalogId) === pluginId}
                  onClick={() => void previewInstalledUpdate(plugin, update)}
                  size="small"
                >更新到 v{stringValue(update.latestVersion)}</Button>
              ) : null}
              <Button
                disabled={lifecyclePending || !pluginOperationsAvailable}
                leadingIcon={<Power size={15} />}
                onClick={() => void previewInstalledAction(enabled ? 'disable' : 'enable', pluginId)}
                size="small"
                variant="quiet"
              >{enabled ? '停用' : '启用'}</Button>
              <Button
                disabled={!rollbackReady || lifecyclePending || !pluginOperationsAvailable}
                leadingIcon={<RotateCcw size={15} />}
                onClick={() => void previewInstalledAction('rollback', pluginId)}
                size="small"
                variant="quiet"
              >恢复上一版本</Button>
              <Button
                disabled={lifecyclePending || !pluginOperationsAvailable}
                leadingIcon={<PackageX size={15} />}
                onClick={() => void previewInstalledAction('uninstall', pluginId)}
                size="small"
                variant="quiet"
              >卸载</Button>
            </div>
          </article>
        );
      }) : pluginRuntimeAvailable ? <EmptyState description="需要新能力时，可以先查看来源和权限，再决定是否安装。" icon={PackageCheck} title="还没有额外扩展" /> : null}
    </div>
  );

  const hooksStatusBadge = (
    <StatusBadge
      label={lifecycle.error ? '状态不可用' : lifecycle.data === undefined ? (lifecycle.isFetching ? '正在读取' : '等待读取') : `${lifecyclePolicies.filter((item) => item.enabled === true).length}/${lifecyclePolicies.length} 已启用`}
      tone={lifecycle.error ? 'warning' : 'neutral'}
    />
  );

  const hooksBlock = (
    <div className="lifecycle-hooks">
      <div className="lifecycle-hooks__policies">
        {lifecyclePolicies.map((policy) => (
          <article className="lifecycle-policy" key={stringValue(policy.eventType)}>
            <span className="lifecycle-policy__title">
              <strong>{lifecycleEventLabel(stringValue(policy.eventType))}</strong>
              <small>{lifecycleActionLabel(stringValue(policy.action))}</small>
            </span>
            <span className="lifecycle-policy__limits">
              <span><Sparkles size={14} />摘要长度：{Number(policy.tokenLimit || 0) > 320 ? '标准' : '简短'}</span>
              <span><Clock3 size={14} />{cooldownLabel(Number(policy.cooldownSeconds || 0))}</span>
            </span>
            <Switch
              checked={policy.enabled === true}
              disabled={updateLifecycle.isPending}
              label={`${lifecycleEventLabel(stringValue(policy.eventType))}：${policy.enabled === true ? '已启用' : '已停用'}`}
              onCheckedChange={(enabled) => void updateHook(stringValue(policy.eventType), enabled)}
            />
          </article>
        ))}
      </div>
      <div className="lifecycle-hooks__audit">
        <h3>
          最近状态
          {lifecycleEvents.length > 8 ? <small>最近 8 / 共 {lifecycleEvents.length} 条</small> : null}
        </h3>
        {lifecycleEvents.length ? lifecycleEvents.slice(0, 8).map((event) => (
          <div className="lifecycle-audit" key={stringValue(event.eventId)}>
            <span><strong>{lifecycleEventLabel(stringValue(event.eventType))}</strong><small>最近一次对话</small></span>
            <StatusBadge {...lifecycleStatusBadge(event)} />
          </div>
        )) : <EmptyState description="功能在对话中触发后，运行记录会显示在这里。" icon={History} title="还没有触发记录" />}
        {lifecycleEvents.length > 8 ? (
          <Disclosure
            className="lifecycle-audit-more"
            summary={<>
              <span>查看其余 {lifecycleEvents.length - 8} 条记录</span>
              <ChevronRight aria-hidden="true" size={15} />
            </>}
          >
            <div className="lifecycle-audit-more__items">
              {lifecycleEvents.slice(8).map((event) => (
                <div className="lifecycle-audit" key={stringValue(event.eventId)}>
                  <span><strong>{lifecycleEventLabel(stringValue(event.eventType))}</strong><small>更早的对话</small></span>
                  <StatusBadge {...lifecycleStatusBadge(event)} />
                </div>
              ))}
            </div>
          </Disclosure>
        ) : null}
      </div>
      {hookError ? <InlineNotice title="自动整理设置没有保存" tone="danger">{hookError}</InlineNotice> : null}
    </div>
  );

  /* -- Native App Center: the window titlebar and the App navigation already
     name the App and the page, so each console carries only a slim purpose
     heading. 已安装 starts with installed Packages; the capability inventory
     and automatic curation remain below it, with their own recovery controls.
     目录 and 建议 stay on their own routes. ------------------------------- */

  const studioBlock = <><PluginStudio disabled={lifecyclePending} onPendingChange={setStudioPending} onPrepare={() => { setPendingChange({}); setLifecycleReceipt(undefined); setLifecycleError(''); }} onPreview={(value) => { setPendingChange(value); setLifecycleError(''); }} />{approvalBlock}{receiptBlock}{errorBlock}</>;
  const nativeBody = nativePage === 'capabilities' ? (
      <NativeConsole
        icon={Wrench}
        title="功能开关"
        trailing={catalog.data ? <span className="plugins-count">{filtered.length} 项</span> : null}
      >
        <QueryState error={asError(catalog.error)} isPending={catalog.isPending} onRetry={() => void catalog.refetch()}>
          {capabilityOverviewBlock}
          {capabilityBrowseBlock}
        </QueryState>
      </NativeConsole>
  ) : nativePage === 'studio' ? studioBlock : nativePage === 'skills' ? (
    <NativeConsole
      icon={Sparkles}
      title="Skills"
      trailing={skills.data ? <span className="plugins-count">{skillCountLabel}</span> : null}
    >
      <QueryState error={asError(skills.error)} isPending={skills.isPending} onRetry={() => void skills.refetch()}>
        <div className="plugin-lifecycle">
          {skillsBrowseBlock}
          {approvalBlock}
          {receiptBlock}
          {errorBlock}
        </div>
      </QueryState>
    </NativeConsole>
  ) : nativePage === 'proposals' ? (
    <NativeConsole
      icon={Sparkles}
      title={`${identity.assistantName}的建议`}
      trailing={proposals.data ? <span className="plugins-count">{proposalItems.length} 条</span> : null}
    >
      <QueryState error={asError(proposals.error)} isPending={proposals.isPending} onRetry={() => void proposals.refetch()}>
        <div className="plugin-lifecycle">
          {proposalsBlock(false)}
          {approvalBlock}
          {receiptBlock}
          {errorBlock}
        </div>
      </QueryState>
    </NativeConsole>
  ) : (
    <>
      <NativeConsole icon={Boxes} title="插件安装与更新" trailing={packageStatusBadge}>
        <QueryState error={packagesError} isPending={packagesPending} onRetry={retryPackages}>
          <div className="plugin-lifecycle">
            {runtimeNotice}
            {approvalBlock}
            {receiptBlock}
            {errorBlock}
            {installedGridBlock}
            {sourceInstallBlock}
            {authoringCalloutBlock}
          </div>
        </QueryState>
      </NativeConsole>

      <Button onClick={() => openPawOsRoute(desktop, `/plugins?view=capabilities${sessionContextId ? `&sessionId=${encodeURIComponent(sessionContextId)}` : ''}`)} variant="quiet">管理 Agent 功能开关与默认设置</Button>

      <NativeConsole icon={Clock3} title="自动整理与提醒" trailing={hooksStatusBadge}>
        <QueryState error={asError(lifecycle.error)} isPending={lifecycle.isPending} onRetry={() => void lifecycle.refetch()}>
          {hooksBlock}
        </QueryState>
      </NativeConsole>
    </>
  );

  const webBody = skillsView ? (
    <>
      <ManagementSection
        description="查看 Pi Runtime 当前发现的 Skill，按来源、状态和内容修订筛选。Package Skill 的变更始终作用于整个 Package；Bundled 与项目 Skill 仅提供正文查看。"
        title="Skills"
        trailing={<span className="plugins-count">{skillCountLabel}</span>}
      >
        <QueryState error={asError(skills.error)} isPending={skills.isPending} onRetry={() => void skills.refetch()}>
          {skillsBrowseBlock}
          <div className="plugin-lifecycle skills-lifecycle">
            {approvalBlock}
            {receiptBlock}
            {errorBlock}
          </div>
        </QueryState>
      </ManagementSection>
    </>
  ) : (
    <>
      <QueryState error={asError(catalog.error)} isPending={catalog.isPending} onRetry={() => void catalog.refetch()}>
        <ManagementSection
          description="选择 Agent 可以使用的功能，分别管理当前对话、项目和全局默认。插件安装与更新在下方单独管理。"
          title="能力概览"
        >
          {capabilityOverviewBlock}
        </ManagementSection>

        <ManagementSection
          description="选择一项查看它能做什么、当前是否可用，以及由哪一层设置决定伙伴能否使用。"
          title="功能开关"
          trailing={<span className="plugins-count">{filtered.length} 项</span>}
        >
          {capabilityBrowseBlock}
        </ManagementSection>
      </QueryState>

      <div className="plugins-maintenance-entry">
        <span>
          <strong>扩展与自动整理</strong>
          <small>安装额外能力，或调整任务完成、上下文整理和工具失败后的自动记录。</small>
        </span>
        <Button
          aria-expanded={showMaintenance}
          leadingIcon={<Settings2 size={15} />}
          onClick={() => setShowMaintenance((current) => !current)}
          size="small"
          variant="quiet"
        >
          {showMaintenance ? '收起维护选项' : '管理扩展与自动整理'}
        </Button>
      </div>

      {showMaintenance ? <ManagementSection
        description="安装、更新、停用、恢复或卸载之前都会先说明来源、权限和影响，经你确认后才会应用。"
        title={nativePage === 'proposals' ? `${identity.assistantName}的建议` : '已安装与获取扩展'}
        trailing={packageStatusBadge}
      >
        <QueryState error={packagesError} isPending={packagesPending} onRetry={retryPackages}>
          {runtimeNotice}
          <div className="plugin-lifecycle">
            {nativePage !== 'proposals' ? <>
              {sourceInstallBlock}
              {catalogRowsBlock}
              {authoringCalloutBlock}
            </> : null}
            {proposalsBlock(true)}
            {approvalBlock}
            {receiptBlock}
            {errorBlock}
            {nativePage !== 'proposals' ? installedGridBlock : null}
          </div>
        </QueryState>
      </ManagementSection> : null}

      {showMaintenance ? <ManagementSection
        description="在一些关键时刻自动留下检查点或复盘建议。它不会替你写入长期记忆，也不会获得新的权限。"
        title="自动整理与提醒"
        trailing={hooksStatusBadge}
        >
        <QueryState
          error={asError(lifecycle.error)}
          isPending={lifecycle.isPending}
          onRetry={() => void lifecycle.refetch()}
        >
          {hooksBlock}
        </QueryState>
      </ManagementSection> : null}
    </>
  );

  return (
    <ManagementPage
      actions={<>
        <Button leadingIcon={<ShieldQuestion size={15} />} onClick={() => navigate('/approvals')} size="small" variant="quiet">审批中心</Button>
        <Button leadingIcon={<RefreshCw size={15} />} loading={refreshing} onClick={() => void refreshAll()} size="small">刷新</Button>
      </>}
      description={skillsView
        ? '浏览当前可发现的 Skill；Package Skill 的启用、更新和卸载仍按整个 Package 预览、确认并应用。'
        : '管理各类 Agent 可用的技能、工具与 Pi 扩展，包括发现、安装、启用范围和版本回退。高风险执行仍进入独立审批中心。'}
      eyebrow={skillsView ? '模型与扩展 · Skills' : '模型与扩展'}
      routeId="plugins"
      title={skillsView ? 'Skills 管理' : '插件管理'}
    >
      {nativeAppCenter ? nativeBody : nativePage === 'studio' ? studioBlock : webBody}
    </ManagementPage>
  );
}

function ExtensionAppIdentity({ app }: { app: PawExtensionAppManifest }) {
  return (
    <span aria-hidden="true" className="plugin-extension-app-identity">
      <PawAppIcon appId={app.id} size={24} />
      <span>{app.shortLabel}</span>
    </span>
  );
}

function ExtensionAppBadge() {
  return <span className="plugin-extension-app-badge">PAWOS App</span>;
}

function NativeConsole({
  children,
  icon: Icon,
  title,
  trailing,
}: {
  children: ReactNode;
  icon: LucideIcon;
  title: string;
  trailing?: ReactNode;
}) {
  return (
    <section aria-label={title} className="plugins-native-card">
      <header className="plugins-native-card__head">
        <span aria-hidden="true" className="plugins-native-card__glyph"><Icon size={15} /></span>
        <h2>{title}</h2>
        {trailing ? <span className="plugins-native-card__trailing">{trailing}</span> : null}
      </header>
      {children}
    </section>
  );
}
function SkillDetail({
  detail,
  detailError,
  detailPending,
  installedPackage,
  item,
  lifecyclePending,
  lifecycleUnavailable,
  onAction,
  onClose,
  onOpenScenes,
  onRetry,
  onUpdate,
  update,
}: {
  detail: Record<string, unknown>;
  detailError: Error | null;
  detailPending: boolean;
  installedPackage?: Record<string, unknown>;
  item: Record<string, unknown>;
  lifecyclePending: boolean;
  lifecycleUnavailable: boolean;
  onAction: (action: 'enable' | 'disable' | 'uninstall') => void;
  onClose: () => void;
  onOpenScenes: () => void;
  onRetry: () => void;
  onUpdate: () => void;
  update?: Record<string, unknown>;
}) {
  const sourceKind = stringValue(item.sourceKind);
  const packageId = stringValue(item.packageId);
  const actions = stringArray(item.actions);
  const body = stringValue(detail.body);
  const presentation = presentSkill(item);
  const originalDescription = stringValue(item.description);
  const contentRevision = stringValue(item.contentRevision, stringValue(item.digest, '未提供'));
  const packageVersion = stringValue(item.packageVersion);
  const canManagePackage = stringValue(item.management) === 'package' && Boolean(packageId);
  const enabled = item.enabled === true;

  return (
    <aside aria-label="Skill 详情" className="plugins-detail skills-detail">
      <div className="plugins-detail__toolbar">
        <span>Skill 详情</span>
        <IconButton
          icon={<PanelRightClose size={16} />}
          label="关闭 Skill 详情"
          onClick={onClose}
        />
      </div>
      <div className="plugins-detail__heading">
        <span className="plugins-detail__icon"><Sparkles aria-hidden="true" size={18} /></span>
        <div>
          <small>{skillOriginSummary(item)}</small>
          <h3>{presentation.name}</h3>
          <p>{presentation.description || '没有提供用途说明。'}</p>
        </div>
        <StatusBadge {...skillStatusBadge(item)} />
      </div>
      {item.installed !== false && item.enabled !== false ? (
        <Button onClick={onOpenScenes} size="small" variant="quiet">设置场景加载</Button>
      ) : null}
      {originalDescription && originalDescription !== presentation.description ? (
        <Disclosure className="skills-detail__original" summary={<><ChevronRight aria-hidden="true" size={14} />查看原始用途说明</>}>
          <p>{originalDescription}</p>
        </Disclosure>
      ) : null}
      <dl className="skills-detail__facts">
        <div><dt>技能标识</dt><dd className="skills-detail__path">{stringValue(item.name, stringValue(item.skillId))}</dd></div>
        <div><dt>来源</dt><dd>{skillSourceLabel(sourceKind)}</dd></div>
        {packageId ? <div><dt>Package</dt><dd>{packageId}{packageVersion ? ` · v${packageVersion}` : ''}</dd></div> : null}
        <div><dt>资源路径</dt><dd className="skills-detail__path">{stringValue(item.resourcePath, '未提供')}</dd></div>
        <div><dt>内容修订</dt><dd className="skills-detail__path">{contentRevision}</dd></div>
      </dl>
      <div className="skills-detail__scope">
        <strong>{canManagePackage ? 'Package 范围管理' : '仅查看'}</strong>
        <span>{publicSkillManagementReason(stringValue(item.managementReason, canManagePackage
          ? 'Skill 的生命周期由整个 Pi Package 统一管理。'
          : '这里查看技能正文；新对话的加载范围在场景加载中设置。'))}</span>
      </div>
      <div className="skills-detail__actions">
        {canManagePackage && actions.includes(enabled ? 'disable' : 'enable') ? (
          <Button
            disabled={lifecyclePending || lifecycleUnavailable || !installedPackage}
            leadingIcon={<Power size={15} />}
            onClick={() => onAction(enabled ? 'disable' : 'enable')}
            size="small"
          >{enabled ? '停用整个 Package' : '启用整个 Package'}</Button>
        ) : null}
        {canManagePackage && update && actions.includes('update') ? (
          <Button
            disabled={lifecyclePending || lifecycleUnavailable || !installedPackage}
            leadingIcon={<CircleArrowUp size={15} />}
            loading={lifecyclePending}
            onClick={onUpdate}
            size="small"
            variant="quiet"
          >更新 Package{stringValue(update.latestVersion) ? ` 到 v${stringValue(update.latestVersion)}` : ''}</Button>
        ) : null}
        {canManagePackage && actions.includes('uninstall') ? (
          <Button
            disabled={lifecyclePending || lifecycleUnavailable || !installedPackage}
            leadingIcon={<PackageX size={15} />}
            onClick={() => onAction('uninstall')}
            size="small"
            variant="quiet"
          >卸载整个 Package</Button>
        ) : null}
      </div>
      {detailPending ? (
        <div aria-label="正在加载 Skill 正文" className="skills-detail__body-state" role="status">正在加载 Skill 正文…</div>
      ) : detailError ? (
        <InlineNotice title="Skill 正文暂时无法读取" tone="danger">
          {publicErrorText(detailError, '服务器没有返回这项 Skill 的正文。')}
          <Button onClick={onRetry} size="small" variant="quiet">重试正文</Button>
        </InlineNotice>
      ) : (
        <pre className="skills-detail__body">{body || '服务器没有返回正文。'}</pre>
      )}
    </aside>
  );
}


function ToolDetail({
  assistantName,
  defaultPending,
  defaultPreference,
  defaultsAvailable,
  item,
  onClose,
  onDefaultPreferenceChange,
  onProjectPreferenceChange,
  projectAvailable,
  projectOwnerId,
  projectPending,
  projectPreference,
  sessionOwnerId,
  sessionPreference,
}: {
  assistantName: string;
  defaultPending: boolean;
  defaultPreference: CapabilityPreference;
  defaultsAvailable: boolean;
  item: ToolRecord;
  onClose: () => void;
  onDefaultPreferenceChange: (preference: CapabilityPreference) => void;
  onProjectPreferenceChange: (preference: CapabilityPreference) => void;
  projectAvailable: boolean;
  projectOwnerId?: string;
  projectPending: boolean;
  projectPreference: CapabilityPreference;
  sessionOwnerId?: string;
  sessionPreference: CapabilityPreference;
}) {
  const operations = operationLabelsFor(item);
  const unknownOperationCount = Math.max(
    0,
    stringArray(item.operations).length - operations.length,
  );
  const schema = asRecord(item.schema ?? item.inputSchema ?? item.parameters);
  const DetailIcon = item.kind === 'skill' ? Sparkles : item.kind === 'extension' ? Boxes : Wrench;
  const fixed = item.alwaysAvailable === true;
  const requiresRoom = capabilityNeedsRoomContext(item);

  return (
    <aside aria-label="能力详情" className="plugins-detail">
      <div className="plugins-detail__toolbar">
        <span>能力详情</span>
        <IconButton
          icon={<PanelRightClose size={16} />}
          label="关闭能力详情"
          onClick={onClose}
        />
      </div>

      <div className="plugins-detail__heading">
        <span className="plugins-detail__icon">
          <DetailIcon aria-hidden="true" size={18} />
        </span>
        <div>
          <small>{publicCapabilitySourceLabel(item.source.label)} · {capabilityKindLabel(item.kind)}</small>
          <h3>{publicCapabilityDisplayName(item)}</h3>
          <p>{publicCapabilityDescription(item)}</p>
        </div>
        <StatusBadge {...availabilityBadge(item)} />
      </div>

      {fixed ? (
        <section aria-label="固定能力策略" className="capability-precedence">
          <header>
            <span>
              <small>可用范围</small>
              <strong>{requiresRoom ? '进入 Room 后可用' : '默认可用'}</strong>
            </span>
            <StatusBadge label="基础能力" tone="success" />
          </header>
          <p>
            {requiresRoom ? '这项功能供 Room 中的伙伴分派、收集和验收工作；当前不在 Room 中，因此暂不提供给 Agent。' : '这项基础能力始终可用；实际执行前仍会检查当前权限。'}
          </p>
        </section>
      ) : (
        <>
          <section aria-label="能力可见范围" className="capability-precedence">
            <header>
              <span>
              <small>当前生效的设置</small>
                <strong>{capabilityEffectiveLabel(item.disclosure.effective)}</strong>
              </span>
              <StatusBadge
                label={capabilityScopeLabel(item.effectiveScope)}
                tone={item.disclosure.effective === 'enabled' ? 'success' : 'neutral'}
              />
            </header>
          <Field
            className="capability-default-field"
            description="用于没有项目或临时设置的对话；正在进行的任务不会因此中断或获得额外权限。"
            htmlFor={`capability-global-default-${item.canonicalId.replace(/[^a-z0-9_-]/giu, '-')}`}
            label="所有对话默认"
          >
            <Select
              aria-label={`${publicCapabilityDisplayName(item)}的所有对话默认`}
              disabled={!defaultsAvailable || defaultPending}
              id={`capability-global-default-${item.canonicalId.replace(/[^a-z0-9_-]/giu, '-')}`}
              onValueChange={onDefaultPreferenceChange}
              options={capabilityPreferenceOptions}
              value={defaultPreference}
            />
          </Field>
          {projectAvailable ? (
            <Field
              className="capability-default-field"
              description="当前项目会优先采用这里的选择；当前对话的临时选择仍优先。"
              htmlFor={`capability-project-default-${item.canonicalId.replace(/[^a-z0-9_-]/giu, '-')}`}
              label="当前项目默认"
            >
              <Select
                aria-label={`${publicCapabilityDisplayName(item)}的当前项目默认`}
                disabled={projectPending}
                id={`capability-project-default-${item.canonicalId.replace(/[^a-z0-9_-]/giu, '-')}`}
                onValueChange={onProjectPreferenceChange}
                options={capabilityPreferenceOptions}
                value={projectPreference}
              />
            </Field>
          ) : null}

            <dl>
              {sessionOwnerId ? (
                <div>
                  <dt>当前对话</dt>
                  <dd>{preferenceLabel(sessionPreference)}<small>仅影响当前对话</small></dd>
                </div>
              ) : null}
              {projectAvailable ? (
                <div>
                  <dt>当前项目</dt>
                  <dd>{preferenceLabel(projectPreference)}<small>适用于此项目中跟随默认的对话</small></dd>
                </div>
              ) : null}
              <div>
                <dt>所有对话</dt>
                <dd>{preferenceLabel(defaultPreference)}<small>由所有对话设置控制</small></dd>
              </div>
              <div>
                <dt>默认设置</dt>
                <dd>{item.effectiveScope === 'built_in_default' ? capabilityEffectiveLabel(item.disclosure.effective) : '由可用能力决定'}<small>仅在上层全部继承时使用</small></dd>
              </div>
            </dl>
            <p>更改从下一轮生效。已关闭的功能仍可保留安装；安装和对话使用分别管理。</p>
          </section>

        </>
      )}


      <dl className="plugins-detail__facts">
        <div>
          <dt><CheckCircle2 aria-hidden="true" size={15} />安装与在线状态</dt>
          <dd>{requiresRoom ? '需要 Room 上下文' : capabilityStatusLabel(item.status)}</dd>
        </div>
        <div>
          <dt><ShieldAlert aria-hidden="true" size={15} />风险</dt>
          <dd>{capabilityRiskLabel(item.risk)}</dd>
        </div>
        <div>
          <dt><ShieldCheck aria-hidden="true" size={15} />所需权限</dt>
          <dd>{item.requiredPermissions.length ? item.requiredPermissions.map(publicCapabilityPermissionLabel).join('、') : '不需要额外权限'}</dd>
        </div>
        <div>
          <dt><ShieldCheck aria-hidden="true" size={15} />执行授权</dt>
          <dd>
            {item.authorization.state === 'authorized' ? '已授权'
              : item.authorization.state === 'denied' ? '未授权'
                : item.authorization.reason === 'session_context_required' ? '由具体对话的权限决定' : '不适用'}
          </dd>
        </div>
        <div>
          <dt><MessageCircle aria-hidden="true" size={15} />伙伴可见范围</dt>
          <dd>
            {item.disclosure.effective === 'enabled' ? `会向${assistantName}显示` : `暂不向${assistantName}显示`}
          </dd>
        </div>
        <div>
          <dt><History aria-hidden="true" size={15} />生效来源</dt>
          <dd>{capabilityScopeLabel(item.effectiveScope)}</dd>
        </div>
      </dl>

      {operations.length || unknownOperationCount ? (
        <div className="plugins-detail__capabilities">
          <h4>支持的操作</h4>
          <ul>
            {operations.map((operation) => <li key={operation}>{operation}</li>)}
            {unknownOperationCount ? <li>其他 {unknownOperationCount} 项操作</li> : null}
          </ul>
        </div>
      ) : null}



      <section className="capability-disclosure">
          <Disclosure
            className="capability-disclosure__technical"
            contentClassName="capability-disclosure__technical-content"
            summary="查看技术参数"
          >
            <pre>{JSON.stringify({
              canonicalId: item.canonicalId,
              displayName: item.displayName,
              description: item.description,
              source: item.source,
              requiredPermissions: item.requiredPermissions,
              authorization: item.authorization,
              operations: stringArray(item.operations),
              ...(Object.keys(schema).length ? { schema } : {}),
            }, null, 2)}</pre>
          </Disclosure>
      </section>
    </aside>
  );
}


function itemKey(item: ToolRecord): string { return item.canonicalId; }
function publicCapabilityDisplayName(item: ToolRecord): string {
  const label = ({ Ask: '向你提问', Todo: '任务清单' } as Record<string, string>)[item.displayName] ?? item.displayName;
  return publicPluginDisplayName(label);
}
function publicCapabilityDescription(item: ToolRecord): string {
  // These are user-facing summaries of the built-in contracts. The complete
  // original description remains available in the technical disclosure.
  const summary: Record<string, string> = {
    'tool:overview': '查看 Agent、模型、记忆和输入服务的整体状态。',
    'tool:input': '查看输入方案和候选解释，按当前权限调整设置或词表。',
    'tool:voice': '查看语音服务状态，切换已配置的语音服务。',
    'tool:planning': '查看每日计划，更新任务进度。',
    'tool:agent_schedule': '预约 Agent 在指定时间继续任务，或查看已有预约。',
    'tool:memory': '把相关记忆加入对话，并允许 Agent 查询记忆；关闭后不再使用，已保存的记忆仍保留。',
    'tool:agent_role_book': '查看 Agent 角色说明，并为可复用的经验提出待审更新。',
    'tool:knowledge': '检索已授权文档，管理知识库内容。',
    'tool:models': '查看可用模型与服务，调整不含密钥的配置。',
    'tool:runtime': '查看运行服务的状态，执行暂停、重连或重启。',
    'tool:configuration': '查看历史与配置，导出或恢复不含密钥的备份。',
    'tool:agents': '委派子 Agent 处理独立任务，查看进展与结果。',
    'tool:session_search': '搜索以往对话的摘要，跳转到相关记录。',
    'tool:trace_diagnostics': '汇总对话、Room 和运行记录，定位异常并查看证据。',
    'tool:room_partner': '在 Room 中分派工作、查看伙伴进展、收集和验收结果。',
    'tool:browser': '让 Agent 查看网页并执行浏览器操作，保留操作记录。',
    'tool:plugins': '查找、制作、安装和管理插件。',
    'tool:lab_project': '查看 Lab 项目，创建和更新项目成果与视图。',
    'tool:desktop_semantic': '读取桌面窗口中的控件，操作已授权的应用。',
    'tool:workspace_lsp': '查找代码定义和引用，在授权工作区执行重命名等修改。',
    'tool:workspace_job': '在授权工作区启动后台任务、查看日志或停止运行。',
    'tool:ask': '在需要你做决定时，向你提出问题和选项。',
    'extension:session-review': '整理已有对话的成果和依据，供你复盘查看。',
    'extension:vertical-agent-sandbox': '在受控环境中运行和评测垂直场景示例。',
    'extension:community-catalog-preview': '预览社区插件目录；当前只供查看，尚不可安装。',
  };
  if (summary[item.canonicalId]) return summary[item.canonicalId]!;
  if (item.displayName === 'Session Workflow' && item.source.label === 'Bundled with the active Pi Runtime') {
    return '在当前对话中管理目标、计划、任务清单和工作流程。';
  }
  return item.description;
}
function publicCapabilitySourceLabel(label: string): string {
  if (label === 'Bundled with the active Pi Runtime') return '随运行环境提供';
  if (label === 'Not distributed') return '尚未发布';
  return publicPluginSourceLabel(label);
}
function publicCapabilityPermissionLabel(permission: string): string {
  return ({
    native_approval: '遵循对话的执行权限',
    workspace_scope: '仅限授权工作区',
    'session.read': '读取对话内容',
    'sandbox.run': '在受控环境中执行',
    'memory.review': '提交记忆复盘建议',
  } as Record<string, string>)[permission] ?? permission;
}
function capabilityNameRank(item: ToolRecord, needle: string): number {
  if (!needle) return 0;
  const names = [publicCapabilityDisplayName(item), item.displayName].map((name) => name.toLocaleLowerCase('zh-CN'));
  if (names.some((name) => name === needle)) return 0;
  if (names.some((name) => name.startsWith(needle))) return 1;
  if (names.some((name) => name.includes(needle))) return 2;
  return 3;
}
function publicPluginDisplayName(label: string): string {
  return ({
    'Session Review': '对话复盘',
    'Timeline Inspector': '时间线检查',
    '@paw/pi-session-workflow': '对话工作流',
    '@paw/zhanggui-wenshu': '掌柜问数',
    'Vertical Agent Sandbox': '垂直场景沙盒',
  } as Record<string, string>)[label] ?? label;
}
function publicPluginSourceLabel(label: string): string {
  return ({
    'Personal Agent Workbench': '系统内置',
    'Product bundle': '随产品提供',
    bundled: '随产品提供',
    managed: '由运行环境管理',
    npm: 'npm 包',
    git: 'Git 仓库',
    local: '本地目录',
  } as Record<string, string>)[label] ?? label;
}
function publicPluginPermissionLabel(permission: string): string {
  return publicCapabilityPermissionLabel(permission);
}
function publicInstalledPluginDescription(plugin: Record<string, unknown>): string {
  return ({
    '@paw/pi-session-workflow': '管理当前对话的目标、计划、任务清单和工作流程。',
    '@paw/zhanggui-wenshu': '为掌柜问数提供经营问答技能，并连接 SGG 沙盒。',
    'session-review': '整理对话成果及其依据，供你复盘查看。',
    'vertical-agent-sandbox': '为当前对话连接由运行环境管理的垂直场景沙盒。',
  } as Record<string, string>)[stringValue(plugin.id)] ?? stringValue(plugin.description);
}
function publicSkillManagementReason(reason: string): string {
  return ({
    'This Skill is supplied by Pi and has no independent enable switch.': '技能随产品提供；新对话的加载范围在场景加载中设置。',
    'This project Skill is discovered from the project workspace and has no independent Package lifecycle.': '技能来自当前项目，不作为独立插件启停或卸载；可在场景加载中选择是否使用。',
    'This Skill belongs to the Package; lifecycle changes apply to the whole Package and all of its resources.': '启停、更新或卸载会影响整个插件及其所有技能与资源。',
  } as Record<string, string>)[reason] ?? reason;
}
function skillOriginSummary(item: Record<string, unknown>): string {
  return [...new Set([skillSourceLabel(stringValue(item.sourceKind)), skillInstallStateLabel(item)])].join(' · ');
}
function skillSourceLabel(value: string): string {
  return ({
    package: '插件提供',
    bundled: '随产品提供',
    project: '当前项目',
  } as Record<string, string>)[value] ?? (value || '未知来源');
}
function skillInstallStateLabel(item: Record<string, unknown>): string {
  if (stringValue(item.management) === 'inspect_only') {
    return stringValue(item.sourceKind) === 'bundled' ? '随产品提供' : '项目发现';
  }
  if (item.installed === false) return '未安装';
  return item.enabled === true ? '已启用' : '已停用';
}
function skillStatusBadge(item: Record<string, unknown>): {
  label: string;
  tone: 'success' | 'warning' | 'danger' | 'neutral';
} {
  if (item.installed === false) return { label: '未安装', tone: 'warning' };
  if (stringValue(item.management) === 'inspect_only') return { label: '仅查看', tone: 'neutral' };
  if (item.enabled === true) return { label: '已启用', tone: 'success' };
  if (item.enabled === false) return { label: '已停用', tone: 'warning' };
  return { label: '状态未知', tone: 'neutral' };
}
function stringArray(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []; }
function numberValue(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}
function packageResourceCount(value: unknown): number {
  const resources = asRecord(value);
  return ['extensions', 'skills', 'prompts', 'themes']
    .reduce((total, resourceKind) => total + stringArray(resources[resourceKind]).length, 0);
}
function packageResourceSummary(value: unknown): string {
  const resources = asRecord(value);
  return [
    ['extensions', '扩展'],
    ['skills', '技能'],
    ['prompts', '提示词'],
    ['themes', '主题'],
  ].map(([key, label]) => [label, stringArray(resources[key]).length] as const)
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${label} ${count}`)
    .join(' · ');
}
function formatPluginTime(value: number): string {
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'short',
    timeStyle: 'short',
  }).format(new Date(value));
}
function pluginInvocationStatusLabel(status: string): string {
  if (status === 'succeeded') return '最近调用成功';
  if (status === 'failed') return '最近调用失败';
  if (status === 'cancelled' || status === 'canceled') return '最近调用取消';
  return '最近调用终态未知';
}
function publicPluginResourceKindLabel(kind: string): string {
  return ({ extension: 'Extension', tool: 'Tool', command: 'Command', skill: 'Skill', prompt: 'Prompt', theme: 'Theme' } as Record<string, string>)[kind] ?? kind;
}
function operationLabelsFor(item: ToolRecord): string[] {
  const labels = toolOperationLabels[item.canonicalId];
  return stringArray(item.operations)
    .map((operation) => labels?.[operation] ?? operationLabels[operation])
    .filter((operation): operation is string => Boolean(operation));
}
function capabilityNeedsRoomContext(item: ToolRecord): boolean {
  return item.disclosure.effective === 'disabled'
    && (item.disclosure.reason === 'room_context_required' || item.reasons.includes('room_context_required'));
}
function availabilityBadge(item: ToolRecord): { label: string; tone: 'success' | 'warning' | 'danger' | 'neutral' } {
  if (capabilityNeedsRoomContext(item)) return { label: '进入 Room 后可用', tone: 'neutral' };
  const status = item.status.toLowerCase();
  if (status === 'online' || status === 'ready' || status === 'installed') return { label: '连接正常', tone: 'success' };
  if (status === 'offline') return { label: capabilityStatusLabel(status), tone: 'danger' };
  if (status === 'unconfigured') return { label: capabilityStatusLabel(status), tone: 'warning' };
  return { label: capabilityStatusLabel(status), tone: 'neutral' };
}
function pluginActionLabel(action: string): string { if (action === 'install') return '安装插件'; if (action === 'update') return '更新插件'; if (action === 'enable') return '启用插件'; if (action === 'disable') return '停用插件'; if (action === 'uninstall') return '卸载插件'; if (action === 'rollback') return '回滚插件'; return '变更插件'; }
function catalogStateBadge(item: Record<string, unknown>): { label: string; tone: 'success' | 'warning' | 'neutral' } { if (item.updateAvailable === true) return { label: '有更新', tone: 'warning' }; if (item.installed === true) return { label: '已是最新', tone: 'success' }; if (item.actionable === true) return { label: '可安装', tone: 'neutral' }; return { label: '仅供审阅', tone: 'neutral' }; }
function lifecycleEventLabel(value: string): string { return ({ session_start: '开始对话', turn_end: '完成一轮回复', compaction: '整理长对话', project_complete: '任务完成', tool_failed: '工具没有成功', idle: '暂时空闲' } as Record<string, string>)[value] ?? value; }
function lifecycleActionLabel(value: string): string { return ({ audit_only: '只记录发生了什么', context_checkpoint: '为下一轮保留上下文', memory_review_suggestion: '为下一轮准备记忆建议' } as Record<string, string>)[value] ?? value; }
function cooldownLabel(seconds: number): string { if (!seconds) return '可以随时触发'; if (seconds >= 60) return `至少间隔 ${Math.round(seconds / 60)} 分钟`; return `至少间隔 ${seconds} 秒`; }
function lifecycleStatusBadge(item: Record<string, unknown>): { label: string; tone: 'success' | 'warning' | 'neutral' } { const status = stringValue(item.status); if (status === 'suggested') return { label: '待下一轮复盘', tone: 'warning' }; if (status === 'recorded') return { label: '已记录', tone: 'success' }; if (status === 'skipped') return { label: '无事实已跳过', tone: 'neutral' }; if (status === 'cooldown') return { label: '冷却中', tone: 'neutral' }; return { label: status === 'disabled' ? '策略停用' : status, tone: 'neutral' }; }
function errorMessage(error: unknown): string { return publicErrorText(error, '插件操作失败，请稍后重试。'); }
function asError(error: unknown): Error | null { return error instanceof Error ? error : error ? new Error('暂时无法读取这部分内容。') : null; }
function firstError(...errors: unknown[]): Error | null { return errors.map(asError).find((error): error is Error => Boolean(error)) ?? null; }
