import { routeRegistry, type RouteId } from '@/app/route-registry';

export type PawOsAppId =
  | 'project-workbench'
  | 'schedules'
  | 'agent'
  | 'agent-capsule'
  | 'memory'
  | 'knowledge'
  | 'input-studio'
  | 'app-center'
  | 'system-monitor'
  | 'trace-agent'
  | 'eval-lab'
  | 'system-settings'
  | 'files'
  | 'browser'
  | 'terminal';

export type PawOsExtensionAppId = `extension:${string}`;
export type PawOsDesktopAppId = PawOsAppId | PawOsExtensionAppId;

export type PawOsAppPresentation =
  | 'workspace'
  | 'conversation'
  | 'collaboration'
  | 'library'
  | 'studio'
  | 'utility'
  | 'system';

export type PawOsAppDefinition = {
  id: PawOsAppId;
  label: string;
  shortLabel: string;
  routeIds: readonly RouteId[];
  defaultRouteId: RouteId | null;
  homeRoute?: string;
  presentation: PawOsAppPresentation;
  accent: 'cyan' | 'blue' | 'violet' | 'amber' | 'green' | 'rose' | 'slate';
  tagline: string;
};

export const wayfinderRouteId: RouteId = 'project-field';

export const pawOsAppRegistry: readonly PawOsAppDefinition[] = [
  {
    id: 'schedules', label: '定时任务', shortLabel: '定时',
    routeIds: ['schedules'], defaultRouteId: 'schedules', presentation: 'utility', accent: 'blue',
    tagline: '自动执行、PR 跟进、周期评测与后台维护',
  },
  {
    id: 'project-workbench',
    label: '项目工作台',
    shortLabel: '项目',
    routeIds: ['overview', 'planning', 'work-documents'],
    defaultRouteId: 'overview',
    presentation: 'workspace',
    accent: 'green',
    tagline: '概览、任务与工作文档',
  },
  {
    id: 'agent',
    label: 'Agent',
    shortLabel: 'Agent',
    routeIds: ['agent', 'rooms'],
    defaultRouteId: 'agent',
    presentation: 'conversation',
    accent: 'violet',
    tagline: '对话、上下文与多 Agent 协作',
  },
  {
    id: 'agent-capsule',
    label: 'Agent Capsule',
    shortLabel: '胶囊',
    routeIds: ['agent-capsule'],
    defaultRouteId: 'agent-capsule',
    presentation: 'conversation',
    accent: 'violet',
    tagline: '任何界面框选、识别与对话',
  },
  {
    id: 'memory',
    label: 'Memory',
    shortLabel: '记忆',
    routeIds: ['memory'],
    defaultRouteId: 'memory',
    homeRoute: '/memory',
    presentation: 'library',
    accent: 'rose',
    tagline: '可追溯的个人记忆',
  },
  {
    id: 'knowledge',
    label: 'Knowledge',
    shortLabel: '知识',
    routeIds: ['knowledge'],
    defaultRouteId: 'knowledge',
    presentation: 'library',
    accent: 'blue',
    tagline: '资料、索引与检索空间',
  },
  {
    id: 'input-studio',
    label: 'Input Studio',
    shortLabel: '输入',
    routeIds: ['input', 'voice', 'history'],
    defaultRouteId: 'input',
    presentation: 'studio',
    accent: 'green',
    tagline: '语音、输入法、词库与输入记录',
  },
  {
    id: 'app-center',
    label: 'App Center',
    shortLabel: '应用',
    routeIds: ['plugins'],
    defaultRouteId: 'plugins',
    presentation: 'utility',
    accent: 'violet',
    tagline: '插件、Pi Package 与 App Surface',
  },
  {
    id: 'system-monitor',
    label: 'System Monitor',
    shortLabel: '监控',
    routeIds: ['context-debug', 'observability', 'diagnostics'],
    defaultRouteId: 'observability',
    presentation: 'system',
    accent: 'cyan',
    tagline: '上下文、运行记录与问题排查',
  },
  {
    id: 'trace-agent',
    label: 'Trace Agent',
    shortLabel: '优化',
    routeIds: ['trace-agent'],
    defaultRouteId: 'trace-agent',
    presentation: 'workspace',
    accent: 'green',
    tagline: '工作对话、经验沉淀与可验证的能力改进',
  },
  {
    id: 'eval-lab',
    label: 'Agent Lab',
    shortLabel: '评测',
    routeIds: ['eval-lab'],
    defaultRouteId: 'eval-lab',
    presentation: 'utility',
    accent: 'amber',
    tagline: '垂直场景的实验、比较与优化',
  },
  {
    id: 'system-settings',
    label: 'System Settings',
    shortLabel: '设置',
    routeIds: ['configuration', 'governance', 'approvals'],
    defaultRouteId: 'configuration',
    presentation: 'system',
    accent: 'slate',
    tagline: '通用配置、外观与安全审批',
  },
  {
    id: 'files',
    label: 'Files',
    shortLabel: '文件',
    routeIds: [],
    defaultRouteId: null,
    presentation: 'utility',
    accent: 'blue',
    tagline: '项目文件与工作区浏览',
  },
  {
    id: 'browser',
    label: 'Browser',
    shortLabel: '浏览器',
    routeIds: ['browser'],
    defaultRouteId: 'browser',
    presentation: 'utility',
    accent: 'cyan',
    tagline: '受控浏览与网页工作台',
  },
  {
    id: 'terminal',
    label: 'Terminal',
    shortLabel: '终端',
    routeIds: [],
    defaultRouteId: null,
    presentation: 'utility',
    accent: 'slate',
    tagline: '项目命令与本地任务',
  },
] as const;

export const primaryDockAppIds: readonly PawOsAppId[] = [
  'agent',
  'eval-lab',
  'project-workbench',
  'memory',
  'knowledge',
  'files',
  'browser',
  'terminal',
];

/**
 * The six product entry points used when presenting the PAWOS core workflow.
 * System utilities and optional tools remain registered and launchable, but
 * are intentionally not counted as core product Apps.
 */
export const coreAppIds: readonly PawOsAppId[] = [
  'agent',
  'eval-lab',
  'project-workbench',
  'memory',
  'knowledge',
  'input-studio',
];

const appById = new Map(pawOsAppRegistry.map((app) => [app.id, app]));
const appByRoute = new Map<RouteId, PawOsAppDefinition>();

for (const app of pawOsAppRegistry) {
  for (const routeId of app.routeIds) appByRoute.set(routeId, app);
}

export function pawOsApp(appId: PawOsAppId): PawOsAppDefinition {
  const app = appById.get(appId);
  if (!app) throw new Error(`Unknown PAWOS App: ${appId}`);
  return app;
}

export function pawOsAppForRoute(routeId: RouteId): PawOsAppDefinition | null {
  if (routeId === wayfinderRouteId) return null;
  return appByRoute.get(routeId) ?? null;
}

export function routePath(routeId: RouteId): string {
  return routeRegistry.find((route) => route.id === routeId)?.path ?? '/project-field';
}
