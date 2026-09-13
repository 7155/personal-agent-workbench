export type RouteId =
  | 'project-field'
  | 'overview'
  | 'input'
  | 'agent'
  | 'agent-capsule'
  | 'rooms'
  | 'plugins'
  | 'approvals'
  | 'browser'
  | 'voice'
  | 'planning'
  | 'schedules'
  | 'work-documents'
  | 'memory'
  | 'knowledge'
  | 'governance'
  | 'history'
  | 'observability'
  | 'evolution-report'
  | 'eval-lab'
  | 'trace-agent'
  | 'context-debug'
  | 'diagnostics'
  | 'configuration';

export type RouteDefinition = {
  id: RouteId;
  path: `/${string}`;
  label: string;
  shortLabel: string;
  group: 'work' | 'capability' | 'operations';
  surface?: 'standalone';
};

export const routeGroupLabels: Record<RouteDefinition['group'], string> = {
  work: '工作',
  capability: '能力',
  operations: '系统',
};

export const routeRegistry: readonly RouteDefinition[] = [
  { id: 'project-field', path: '/project-field', label: '项目场', shortLabel: '项目', group: 'work' },
  { id: 'overview', path: '/overview', label: '概览', shortLabel: '概览', group: 'work' },
  { id: 'agent', path: '/agent', label: '对话', shortLabel: '对话', group: 'work' },
  { id: 'agent-capsule', path: '/agent-capsule', label: 'Agent Capsule', shortLabel: '胶囊', group: 'work' },
  { id: 'rooms', path: '/rooms', label: '多人协作', shortLabel: '协作', group: 'work' },
  { id: 'planning', path: '/planning', label: '任务', shortLabel: '任务', group: 'work' },
  { id: 'schedules', path: '/schedules', label: '定时任务', shortLabel: '定时', group: 'work' },
  { id: 'work-documents', path: '/work-documents', label: '工作文档', shortLabel: '文档', group: 'work' },
  { id: 'memory', path: '/memory', label: '我的记忆', shortLabel: '记忆', group: 'capability' },
  { id: 'knowledge', path: '/knowledge', label: '知识库', shortLabel: '知识', group: 'capability' },
  { id: 'plugins', path: '/plugins', label: '插件管理', shortLabel: '插件', group: 'capability' },
  { id: 'browser', path: '/browser', label: '浏览器', shortLabel: '浏览器', group: 'capability' },
  { id: 'voice', path: '/voice', label: '语音输入', shortLabel: '语音', group: 'capability' },
  { id: 'input', path: '/input', label: '输入法与词库', shortLabel: '输入', group: 'capability' },
  { id: 'history', path: '/history', label: '输入记录', shortLabel: '记录', group: 'capability' },
  { id: 'governance', path: '/governance', label: '安全与治理', shortLabel: '安全', group: 'operations' },
  { id: 'approvals', path: '/approvals', label: '审批中心', shortLabel: '审批', group: 'operations' },
  { id: 'context-debug', path: '/context-debug', label: '上下文检查', shortLabel: '上下文', group: 'operations' },
  { id: 'observability', path: '/observability', label: '运行记录', shortLabel: '运行', group: 'operations' },
  { id: 'evolution-report', path: '/evolution-report', label: '自我进化实验账本', shortLabel: '实验', group: 'operations', surface: 'standalone' },
  { id: 'eval-lab', path: '/eval-lab', label: 'Agent Lab', shortLabel: '评测', group: 'operations' },
  { id: 'trace-agent', path: '/trace-agent', label: 'Trace Agent', shortLabel: 'Trace', group: 'operations' },
  { id: 'diagnostics', path: '/diagnostics', label: '问题排查', shortLabel: '排查', group: 'operations' },
  { id: 'configuration', path: '/configuration', label: '设置', shortLabel: '设置', group: 'operations' },
] as const;
