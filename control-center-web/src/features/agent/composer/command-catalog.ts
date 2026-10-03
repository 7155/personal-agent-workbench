import type {
  AgentCommand,
  AgentProductCommandName,
  ModelCatalog,
  SessionSummary,
  ToolManifest,
} from '../types';
import { permissionLabel } from './permission-policy';
import { toolAvailableForCurrentSession } from './tool-policy';

export type ComposerCommand = ResolvedPiCommand | ProductCommand;

interface CommandAvailability {
  enabled: boolean;
  disabledReason?: string;
}

type ResolvedPiCommand = AgentCommand & CommandAvailability;

interface ProductCommand extends CommandAvailability {
  name: AgentProductCommandName;
  invocation: `/${AgentProductCommandName}`;
  description: string;
  source: 'product';
  behavior: 'execute' | 'insert';
}

const productCommandDefinitions: Omit<ProductCommand, keyof CommandAvailability>[] = [
  { name: 'new', invocation: '/new', description: '创建一段独立对话', source: 'product', behavior: 'execute' },
  { name: 'resume', invocation: '/resume', description: '打开连续对话列表并恢复其他对话', source: 'product', behavior: 'execute' },
  { name: 'name', invocation: '/name', description: '重命名当前对话', source: 'product', behavior: 'insert' },
  { name: 'branch', invocation: '/branch', description: '从历史用户消息创建独立分支', source: 'product', behavior: 'execute' },
  { name: 'compact', invocation: '/compact', description: '保留连续会话并缩短上下文', source: 'product', behavior: 'insert' },
  { name: 'model', invocation: '/model', description: '选择当前对话的模型', source: 'product', behavior: 'execute' },
  { name: 'thinking', invocation: '/thinking', description: '调整当前模型的思考强度', source: 'product', behavior: 'execute' },
  { name: 'permissions', invocation: '/permissions', description: '查看或切换当前对话权限', source: 'product', behavior: 'execute' },
  { name: 'tools', invocation: '/tools', description: '查看当前权限可用的工具', source: 'product', behavior: 'execute' },
  { name: 'session', invocation: '/session', description: '查看当前任务、计划与运行统计', source: 'product', behavior: 'execute' },
  { name: 'status', invocation: '/status', description: '打开当前对话任务中心', source: 'product', behavior: 'execute' },
  { name: 'subagents', invocation: '/subagents', description: '打开子 Agent 运行图与启动配置', source: 'product', behavior: 'execute' },
  { name: 'settings', invocation: '/settings', description: '打开控制中心配置', source: 'product', behavior: 'execute' },
  { name: 'hotkeys', invocation: '/hotkeys', description: '查看 Web Agent 命令和键盘操作', source: 'product', behavior: 'execute' },
  { name: 'stop', invocation: '/stop', description: '停止当前处理', source: 'product', behavior: 'execute' },
  { name: 'help', invocation: '/help', description: '查看命令及其来源', source: 'product', behavior: 'execute' },
];

export function buildCommandCatalog({
  session,
  catalog,
  piCommands,
  tools,
  toolCatalogStatus,
  busy,
  sending,
}: {
  session?: SessionSummary;
  catalog?: ModelCatalog;
  piCommands: AgentCommand[];
  tools: ToolManifest[];
  toolCatalogStatus: 'loading' | 'ready' | 'failed';
  busy: boolean;
  sending: boolean;
}): ComposerCommand[] {
  const advertisedPiCommandNames = new Set(piCommands.map((command) => command.name));
  const productCommands = productCommandDefinitions
    .filter((command) => (
      command.name !== 'subagents' || advertisedPiCommandNames.has('subagents')
    ))
    .map((command): ProductCommand => ({
      ...command,
      ...(command.name === 'branch' && (session?.runtimeEngine === 'durable' || catalog?.runtimeEngine === 'durable')
        ? { enabled: false, disabledReason: 'Pi Durable 暂不支持历史分支。' }
        : productCommandAvailability(command.name, {
        session,
        catalog,
        tools,
        toolCatalogStatus,
        busy,
        sending,
      })),
    }));
  const reserved = new Set(
    productCommands.map((command) => command.invocation.toLowerCase()),
  );
  const piAvailability = genericCommandAvailability({ session, busy, sending });
  const resolvedPiCommands = piCommands
    .filter((command) => !reserved.has(command.invocation.toLowerCase()))
    .map((command): ResolvedPiCommand => ({
      ...command,
      ...piAvailability,
    }));
  return [...productCommands, ...resolvedPiCommands];
}

export function nextEnabledCommandIndex(
  commands: ComposerCommand[],
  current: number,
  delta: 1 | -1,
): number {
  if (!commands.some((command) => command.enabled)) return current;
  let candidate = current;
  do {
    candidate = (candidate + delta + commands.length) % commands.length;
  } while (!commands[candidate]?.enabled && candidate !== current);
  return candidate;
}

export function commandTitle(source: ComposerCommand['source']): string {
  return ({
    product: '控制中心',
    extension: 'Pi 扩展',
    prompt: 'Pi 提示模板',
    skill: 'Pi Skill',
  })[source];
}

function productCommandAvailability(
  name: AgentProductCommandName,
  context: {
    session?: SessionSummary;
    catalog?: ModelCatalog;
    tools: ToolManifest[];
    toolCatalogStatus: 'loading' | 'ready' | 'failed';
    busy: boolean;
    sending: boolean;
  },
): CommandAvailability {
  const { session, catalog, tools, toolCatalogStatus, busy, sending } = context;
  if (
    (busy || sending)
    && name !== 'resume'
    && name !== 'session'
    && name !== 'status'
    && name !== 'subagents'
    && name !== 'stop'
  ) {
    return {
      enabled: false,
      disabledReason: '当前处理中，仅可切换对话、查看状态或停止',
    };
  }
  if (
    name === 'new'
    || name === 'settings'
    || name === 'help'
    || name === 'hotkeys'
  ) return { enabled: true };
  if (!session) return { enabled: false, disabledReason: '请先选择对话' };
  if (name === 'stop') {
    return busy
      ? { enabled: true }
      : { enabled: false, disabledReason: '当前没有正在处理的任务' };
  }
  if ((name === 'model' || name === 'thinking') && !catalog) {
    return { enabled: false, disabledReason: '模型目录暂不可用' };
  }
  if (name === 'tools') {
    if (toolCatalogStatus !== 'ready') {
      return { enabled: false, disabledReason: '工具目录暂不可用' };
    }
    const hasAvailableTool = tools.some(
      (tool) => toolAvailableForCurrentSession(tool, session),
    );
    if (!hasAvailableTool) {
      return {
        enabled: false,
        disabledReason: `${permissionLabel(session)}没有可用工具`,
      };
    }
  }
  return { enabled: true };
}

function genericCommandAvailability({
  session,
  busy,
  sending,
}: {
  session?: SessionSummary;
  busy: boolean;
  sending: boolean;
}): CommandAvailability {
  if (!session) return { enabled: false, disabledReason: '请先选择对话' };
  if (busy || sending) {
    return {
      enabled: false,
      disabledReason: '当前处理中，仅可查看状态或停止',
    };
  }
  return { enabled: true };
}
