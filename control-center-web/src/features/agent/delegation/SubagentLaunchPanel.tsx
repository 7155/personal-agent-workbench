import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Bot,
  Braces,
  Check,
  ChevronDown,
  GitFork,
  LockKeyhole,
  Play,
  Plus,
  ShieldCheck,
  Sparkles,
  Wrench,
} from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Disclosure, SegmentedControl } from '@/components/primitives';
import type { AgentTemplateV1 } from '@/contracts/generated/agent-template.v1';
import type { ToolManifest } from '@/features/agent/types';
import { toolItems } from '@/features/agent/types';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { isTeamDeployment } from '@/features/team/deployment';
import './subagent-launch.css';

export interface SubagentParentOption {
  sessionId: string;
  label: string;
  detail?: string;
  /** TeamGateway marks another member's Session as visible but not operable. */
  canControl?: boolean;
  canWrite?: boolean;
  defaultAccess?: 'read_only' | 'write';
  accessPolicyDetail?: string;
  workspaceRoots?: string[];
  piSkillsEnabled?: boolean;
  codexSkillsEnabled?: boolean;
}

export function SubagentLaunchPanel({
  parents,
  availableTools,
  collapsible = false,
  defaultOpen = false,
  surface = 'session',
}: {
  parents: readonly SubagentParentOption[];
  availableTools?: readonly ToolManifest[];
  collapsible?: boolean;
  defaultOpen?: boolean;
  surface?: 'session' | 'room';
}) {
  const body = <SubagentLaunchForm
    availableTools={availableTools}
    parents={parents}
    surface={surface}
  />;
  if (!collapsible) return body;
  return <Disclosure
    className="subagent-launch-shell"
    defaultOpen={defaultOpen}
    summary={<>
      <span className="subagent-launch-shell__icon"><Bot size={17} /></span>
      <span><strong>配置子 Agent</strong><small>模板、上下文、工具与权限</small></span>
      <ChevronDown className="subagent-launch-shell__chevron" size={16} />
    </>}
  >{body}</Disclosure>;
}

function SubagentLaunchForm({
  parents,
  availableTools,
  surface,
}: {
  parents: readonly SubagentParentOption[];
  availableTools?: readonly ToolManifest[];
  surface: 'session' | 'room';
}) {
  const transport = useControlTransport();
  const teamMode = isTeamDeployment();
  const queryClient = useQueryClient();
  const [parentSessionId, setParentSessionId] = useState(parents[0]?.sessionId ?? '');
  const [templateId, setTemplateId] = useState('');
  const [contextMode, setContextMode] = useState<'fresh' | 'fork'>('fresh');
  const [access, setAccess] = useState<'read_only' | 'write'>('read_only');
  const [customTools, setCustomTools] = useState(false);
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [piSkillsEnabled, setPiSkillsEnabled] = useState<boolean | undefined>(undefined);
  const [codexSkillsEnabled, setCodexSkillsEnabled] = useState<boolean | undefined>(undefined);
  const [task, setTask] = useState('');
  const [expectedOutput, setExpectedOutput] = useState('');
  const [acceptance, setAcceptance] = useState('');
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const templateQuery = useQuery({
    queryKey: ['agent', 'subagent-launch', 'templates'],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.subagents.templates',
      signal,
    }),
    staleTime: 60_000,
    retry: false,
  });
  const toolQuery = useQuery({
    queryKey: ['agent', 'subagent-launch', 'tools', parentSessionId],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.tools.list',
      query: { sessionId: parentSessionId },
      signal,
    }),
    enabled: availableTools === undefined && Boolean(parentSessionId),
    staleTime: 30_000,
    retry: false,
  });
  const templates = useMemo(() => templateItems(templateQuery.data), [templateQuery.data]);
  const tools = useMemo(
    () => availableTools ? [...availableTools] : toolItems(toolQuery.data),
    [availableTools, toolQuery.data],
  );
  const template = templates.find((item) => item.templateId === templateId) ?? templates[0];
  const parent = parents.find((item) => item.sessionId === parentSessionId) ?? parents[0];
  const effectivePiSkillsEnabled = teamMode
    ? false
    : piSkillsEnabled ?? parent?.piSkillsEnabled ?? false;
  const effectiveCodexSkillsEnabled = teamMode
    ? false
    : codexSkillsEnabled ?? parent?.codexSkillsEnabled ?? false;

  useEffect(() => {
    if (!parents.some((item) => item.sessionId === parentSessionId)) {
      setParentSessionId(parents[0]?.sessionId ?? '');
    }
  }, [parentSessionId, parents]);

  useEffect(() => {
    setPiSkillsEnabled(undefined);
    setCodexSkillsEnabled(undefined);
  }, [parentSessionId]);

  useEffect(() => {
    if (!templates.length) return;
    const selected = templates.find((item) => item.templateId === templateId) ?? templates[0];
    if (selected.templateId !== templateId) setTemplateId(selected.templateId);
  }, [templateId, templates]);

  useEffect(() => {
    if (!template) return;
    const supportedModes = [...template.contextModes];
    if (!supportedModes.includes(contextMode)) {
      setContextMode(supportedModes.includes('fresh') ? 'fresh' : supportedModes[0]);
    }
    const requestedAccess = surface === 'room'
      ? parent?.defaultAccess ?? 'read_only'
      : template.defaultAccess;
    const nextAccess = template.allowedAccess.includes(requestedAccess)
      ? requestedAccess
      : template.allowedAccess[0];
    setAccess(nextAccess === 'write' && !parent?.canWrite ? 'read_only' : nextAccess);
  }, [parent?.canWrite, parent?.defaultAccess, surface, template?.templateId]);

  useEffect(() => {
    const ids = new Set(tools.map((item) => item.id));
    setSelectedTools((current) => current.filter((id) => ids.has(id)));
  }, [tools]);

  const writable = access === 'write';
  const selectedToolCount = customTools ? selectedTools.length : tools.length;
  const canLaunch = Boolean(
    parentSessionId
    && parent?.canControl !== false
    && template
    && task.trim()
    && expectedOutput.trim()
    && acceptance.trim()
    && (!customTools || selectedTools.length),
  );

  async function launch(): Promise<void> {
    if (!canLaunch || !template || !parent || launching) return;
    setLaunching(true);
    setError('');
    setNotice('');
    try {
      const response = await transport.request<Record<string, unknown>>({
        pathId: 'agent.subagents.create',
        body: {
          sessionId: parent.sessionId,
          agent: template.templateId,
          version: template.version,
          task: task.trim(),
          expectedOutput: expectedOutput.trim(),
          acceptanceCriteria: [acceptance.trim()],
          outputSchema: STANDARD_SUBAGENT_OUTPUT_SCHEMA,
          contextMode,
          ...(contextMode === 'fork' ? { forkEntryId: 'latest' } : {}),
          access,
          ...(parent.workspaceRoots?.length
            ? { workspaceRoots: parent.workspaceRoots }
            : {}),
          ...(customTools ? { allowedTools: selectedTools } : {}),
          ...(teamMode || piSkillsEnabled === undefined ? {} : { piSkillsEnabled }),
          ...(teamMode
            ? { codexSkillsEnabled: false }
            : codexSkillsEnabled === undefined ? {} : { codexSkillsEnabled }),
          wait: false,
        },
      });
      const batch = record(response.batch);
      const runs = Array.isArray(batch.runs) ? batch.runs.length : 1;
      setNotice(`${runs} 个子 Agent 已排队；实际配置会以 Launch Digest 固化。`);
      setTask('');
      await queryClient.invalidateQueries({
        queryKey: ['agent', 'status-panel', 'subagents', parent.sessionId],
      });
    } catch (reason) {
      setError(publicAgentErrorText(reason, '子 Agent 启动失败。'));
    } finally {
      setLaunching(false);
    }
  }

  return <section className="subagent-launch" data-surface={surface}>
    <header className="subagent-launch__header">
      <span className="subagent-launch__mark"><Bot size={18} /></span>
      <span><strong>子 Agent 启动配置</strong><small>{surface === 'room' ? 'Room 卫星层提供上限和默认；启动配置固化在父 Session。' : '配置属于 Session；Room 只引用启动收据与运行树。'}</small></span>
      <em><Braces size={13} />结构化交付</em>
    </header>

    {parents.length > 1 ? <label className="subagent-launch__field">
      <span>由谁启动 <small>决定父 Session 与权限上限</small></span>
      <select aria-label="子 Agent 父 Session" value={parentSessionId} onChange={(event) => {
        setParentSessionId(event.target.value);
        setCustomTools(false);
        setSelectedTools([]);
      }}>
        {parents.map((item) => <option key={item.sessionId} value={item.sessionId}>{item.label}{item.detail ? ` · ${item.detail}` : ''}</option>)}
      </select>
    </label> : null}

    <fieldset className="subagent-launch__templates">
      <legend>责任模板 <small>{surface === 'room' ? '模板限定能力；Room 分层权限决定继承的工作默认值' : '模板决定默认边界，不是装饰标签'}</small></legend>
      <div role="radiogroup" aria-label="子 Agent 模板">
        {templates.map((item) => <label key={item.templateId} data-selected={item.templateId === template?.templateId}>
          <input
            checked={item.templateId === template?.templateId}
            name={`subagent-template-${surface}`}
            onChange={() => setTemplateId(item.templateId)}
            type="radio"
            value={item.templateId}
          />
          <span><strong>{item.displayName}</strong><small>{item.summary}</small></span>
          <em>{item.defaultAccess === 'read_only' ? <><LockKeyhole size={12} />只读</> : <><Wrench size={12} />可写</>}</em>
        </label>)}
      </div>
      {templateQuery.isLoading ? <p className="subagent-launch__quiet">正在读取责任模板…</p> : null}
      {templateQuery.isError ? <p className="subagent-launch__error" role="alert">责任模板暂时不可用。</p> : null}
    </fieldset>

    {template ? <div className="subagent-launch__policy-grid">
      <PolicyField icon={<GitFork size={15} />} label="上下文">
        <SegmentedControl
          aria-label="子 Agent 上下文模式"
          items={[
            { value: 'fresh', label: '新上下文', disabled: !template.contextModes.includes('fresh') },
            { value: 'fork', label: 'Fork 当前', disabled: !template.contextModes.includes('fork') },
          ]}
          onValueChange={setContextMode}
          value={contextMode}
        />
        <small>{contextMode === 'fork' ? '从 Pi 最近的可验证分支锚点继承；父 Session 忙碌时不可用。' : '只接收有界任务说明，不复制整段历史。'}</small>
      </PolicyField>
      <PolicyField icon={<ShieldCheck size={15} />} label="工作权限">
        <SegmentedControl
          aria-label="子 Agent 工作权限"
          items={[
            { value: 'read_only', label: '只读', disabled: !template.allowedAccess.includes('read_only') },
            { value: 'write', label: '工作区写入', disabled: !template.allowedAccess.includes('write') || !parent?.canWrite },
          ]}
          onValueChange={setAccess}
          value={access}
        />
        <small>
          {surface === 'room' && parent?.accessPolicyDetail
            ? `${parent.accessPolicyDetail}；`
            : ''}
          {template.allowedAccess.length === 1
            ? `${template.displayName}固定只读。`
            : writable
              ? '本次按父层上限写入；仍受原生审批与系统硬边界约束。'
              : '本次已显式收窄为只读，不能修改文件或执行写操作。'}
        </small>
      </PolicyField>
    </div> : null}

    <Disclosure className="subagent-launch__tools" contentClassName="subagent-launch__tools-content" summary={<><span><Wrench size={15} /><strong>工具与技能</strong><small>{customTools ? `已收窄到 ${selectedToolCount} 个工具` : `继承模板与父 Session 的交集 · 当前可见 ${tools.length}`}</small></span><ChevronDown size={15} /></>}>
      <div className="subagent-launch__tool-mode">
        <label><input checked={!customTools} name={`subagent-tools-${surface}`} onChange={() => setCustomTools(false)} type="radio" />模板默认</label>
        <label><input checked={customTools} name={`subagent-tools-${surface}`} onChange={() => setCustomTools(true)} type="radio" />手动收窄</label>
      </div>
      {customTools ? <div className="subagent-launch__tool-list">
        {tools.map((tool) => <label key={tool.id}>
          <input checked={selectedTools.includes(tool.id)} onChange={() => setSelectedTools((current) => current.includes(tool.id) ? current.filter((id) => id !== tool.id) : [...current, tool.id])} type="checkbox" />
          <span><strong>{tool.displayName}</strong><small>{tool.id} · {tool.riskLevel}</small></span>
        </label>)}
        {!tools.length ? <p className="subagent-launch__quiet">这个父 Session 还没有可核对的工具目录。</p> : null}
      </div> : null}
      <div className="subagent-launch__skill-flags">
        {teamMode
          ? <p className="subagent-launch__quiet">应用与 Skills 继承父任务的固定版本</p>
          : <label><input checked={effectivePiSkillsEnabled} onChange={(event) => setPiSkillsEnabled(event.target.checked)} type="checkbox" /><span><strong>Pi Skills</strong><small>{piSkillsEnabled === undefined ? `继承父 Session · ${effectivePiSkillsEnabled ? '已开启' : '未开启'}` : '本次启动显式配置'}</small></span></label>}
        {teamMode ? null : <label><input checked={effectiveCodexSkillsEnabled} onChange={(event) => setCodexSkillsEnabled(event.target.checked)} type="checkbox" /><span><strong>Codex Skills</strong><small>{codexSkillsEnabled === undefined ? `继承父 Session · ${effectiveCodexSkillsEnabled ? '已开启' : '未开启'}` : '本次启动显式配置'}</small></span></label>}
      </div>
    </Disclosure>

    <div className="subagent-launch__brief">
      <label><span>有界任务</span><textarea aria-label="子 Agent 有界任务" maxLength={8_000} onChange={(event) => setTask(event.target.value)} placeholder="只描述这一位子 Agent 要完成的工作" rows={3} value={task} /></label>
      <label><span>预期交付</span><input aria-label="子 Agent 预期交付" maxLength={2_000} onChange={(event) => setExpectedOutput(event.target.value)} placeholder="例如：带文件与行号证据的审查结论" value={expectedOutput} /></label>
      <label><span>验收条件</span><input aria-label="子 Agent 验收条件" maxLength={1_000} onChange={(event) => setAcceptance(event.target.value)} placeholder="例如：区分已证实问题与剩余风险" value={acceptance} /></label>
    </div>

    <div className="subagent-launch__digest" aria-label="启动摘要预览">
      <span><Sparkles size={14} /><strong>Launch Digest</strong></span>
      <em>{template?.displayName ?? '等待模板'}</em>
      <em>{contextMode === 'fork' ? 'Fork' : 'Fresh'}</em>
      <em>{writable ? '工作区写入' : '只读'}</em>
      <em>{customTools ? `${selectedToolCount} 个指定工具` : '模板工具交集'}</em>
      {effectivePiSkillsEnabled ? <em>Pi Skills</em> : null}
      {effectiveCodexSkillsEnabled ? <em>Codex Skills</em> : null}
      <em>标准输出合同</em>
    </div>

    {error ? <p className="subagent-launch__error" role="alert">{error}</p> : null}
    {notice ? <p className="subagent-launch__notice" role="status"><Check size={14} />{notice}</p> : null}
    <footer>
      <small>最多并行 2 个；完成节点不会因恢复而重跑。</small>
      <Button disabled={!canLaunch} loading={launching} onClick={() => void launch()} leadingIcon={<Play size={15} />} variant="primary">启动子 Agent</Button>
    </footer>
  </section>;
}

function PolicyField({ children, icon, label }: { children: ReactNode; icon: ReactNode; label: string }) {
  return <section className="subagent-launch__policy"><header>{icon}<strong>{label}</strong></header>{children}</section>;
}

function templateItems(value: unknown): AgentTemplateV1[] {
  const source = record(value);
  const items = Array.isArray(source.items) ? source.items : [];
  return items.filter((item): item is AgentTemplateV1 => {
    const template = record(item);
    return template.schemaVersion === 'rag-ime.agent-template.v1'
      && typeof template.templateId === 'string'
      && typeof template.displayName === 'string'
      && Array.isArray(template.contextModes)
      && Array.isArray(template.allowedAccess);
  });
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

const STANDARD_SUBAGENT_OUTPUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['summary', 'evidenceRefs', 'residualRisks'],
  properties: {
    summary: { type: 'string', minLength: 1, maxLength: 12_000 },
    evidenceRefs: {
      type: 'array',
      maxItems: 32,
      items: { type: 'string', minLength: 1, maxLength: 1_024 },
    },
    residualRisks: {
      type: 'array',
      maxItems: 16,
      items: { type: 'string', minLength: 1, maxLength: 2_000 },
    },
  },
};
