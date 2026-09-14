import { useQuery } from '@tanstack/react-query';
import {
  Bot,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  GitFork,
  LoaderCircle,
  Network,
  PanelRightClose,
  PanelsTopLeft,
  Plus,
  Settings2,
  ShieldAlert,
  TriangleAlert,
} from 'lucide-react';
import { forwardRef, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Disclosure, IconButton } from '@/components/primitives';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import type { SessionSummary, ToolManifest } from '@/features/agent/types';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { SubagentConsoleDialog } from '../status/SubagentConsole';
import {
  hasActiveSubagentRuns,
  subagentRuns,
  subagentTree,
  type SubagentTreeNode,
} from '../status/subagent-data';
import {
  isContractInvalid,
  subagentFailurePolicy,
  subagentPresentationState,
  subagentStateLabel,
  subagentTemplateLabel,
} from '../status/subagent-presentation';
import { SubagentLaunchPanel } from './SubagentLaunchPanel';
import './session-subagent.css';

export const SessionSubagentPanel = forwardRef<HTMLElement, {
  sessionId: string;
  session?: SessionSummary;
  tools: readonly ToolManifest[];
  open: boolean;
  compactEmpty?: boolean;
  modal?: boolean;
  onOpenRun?: (run: AgentSubagentRunV1) => void;
  onClose: () => void;
}>(function SessionSubagentPanel({
  sessionId,
  session,
  tools,
  open,
  compactEmpty = false,
  modal = false,
  onOpenRun,
  onClose,
}, ref) {
  const transport = useControlTransport();
  const pageVisible = usePageVisibility();
  const runsQuery = useQuery({
    queryKey: ['agent', 'status-panel', 'subagents', sessionId],
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.subagents.list',
      query: { sessionId, limit: 50 },
      signal,
    }),
    enabled: open && pageVisible && Boolean(sessionId),
    refetchInterval: open && pageVisible
      ? (query) => hasActiveSubagentRuns(subagentRuns(query.state.data)) ? 1_000 : 5_000
      : false,
    retry: false,
  });
  const runs = useMemo(() => subagentRuns(runsQuery.data), [runsQuery.data]);
  const tree = useMemo(() => subagentTree(runsQuery.data), [runsQuery.data]);
  const nodes = tree.roots.length
    ? tree.roots
    : runs.map((run) => ({ run, children: [] }));
  const [selectedRunId, setSelectedRunId] = useState('');
  useEffect(() => {
    if (selectedRunId && runs.some((run) => run.id === selectedRunId)) return;
    const next = runs.find((run) => run.state === 'running' || run.state === 'queued') ?? runs[0];
    setSelectedRunId(next?.id ?? '');
  }, [runs, selectedRunId]);
  const selectedNode = findTreeNode(nodes, selectedRunId) ?? nodes[0];
  const selectedRun = selectedNode?.run;
  const selectedParent = selectedRun?.parentRunId
    ? runs.find((run) => run.id === selectedRun.parentRunId)
    : undefined;
  const activeCount = runs.filter((run) => run.state === 'queued' || run.state === 'running').length;
  const attentionCount = runs.filter((run) => (
    isContractInvalid(run) || run.state === 'failed' || run.state === 'timed_out'
  )).length;
  const returnedCount = runs.filter((run) => !['queued', 'running'].includes(run.state)).length;
  const showCompactEmpty = compactEmpty && !runsQuery.isPending && !runsQuery.error && runs.length === 0;

  return (
    <aside
      id="agent-subagent-panel"
      ref={ref}
      className="session-subagent-panel"
      data-open={open}
      aria-hidden={!open}
      aria-label="Session 子 Agent 工作台"
      aria-modal={modal || undefined}
      inert={open ? undefined : true}
      role={modal ? 'dialog' : undefined}
      tabIndex={-1}
    >
      {open ? <>
        <header>
          <span>
            <strong>子 Agent 工作台</strong>
            <small>{activeCount ? `${activeCount} 个节点正在运行` : runs.length ? '运行图已同步' : '由当前 Session 启动与治理'}</small>
          </span>
          <div className="session-subagent-panel__actions">
            <a aria-label="打开子 Agent 设置" title="打开子 Agent 设置" href="#/configuration?section=subagents">
              <Settings2 size={16} />
            </a>
            <IconButton icon={<PanelRightClose size={17} />} label="收起子 Agent 工作台" onClick={onClose} tooltip />
          </div>
        </header>

        <div className="session-subagent-panel__body">
          {showCompactEmpty ? (
            <div className="session-subagent-empty">
              <p>还没有子 Agent</p>
              <p>把一项明确的工作交给子 Agent，完成后在这里查看结果。</p>
              <Disclosure className="session-subagent-launch" data-compact-empty summary={<><Plus size={16} /><span>配置子 Agent</span><ChevronDown className="session-subagent-launch__chevron" size={15} /></>}>
                <SubagentLaunchPanel
                  availableTools={tools}
                  parents={[{
                    sessionId,
                    label: session?.title || '当前 Session',
                    detail: session?.mode === 'coordinator' ? '主持 Session' : '助手 Session',
                    canControl: session?.canControl,
                    canWrite: session?.mode === 'coordinator'
                      && session.canControl !== false
                      && session.executionMode !== 'read_only'
                      && Boolean(session.workspaceRoots?.length),
                    workspaceRoots: session?.workspaceRoots ?? [],
                    piSkillsEnabled: session?.piSkillsEnabled,
                    codexSkillsEnabled: session?.codexSkillsEnabled,
                  }]}
                />
              </Disclosure>
            </div>
          ) : <><section className="session-subagent-graph" aria-label="子 Agent 运行图">
            <header>
              <span><Network size={17} /><strong>子 Agent 运行图</strong></span>
              <div aria-label="子 Agent 运行统计">
                <small><b>{activeCount}</b> 运行</small>
                <small><b>{returnedCount}</b> 返回</small>
                <small data-attention={attentionCount > 0 || undefined}><b>{attentionCount}</b> 待处理</small>
              </div>
            </header>

            <div className="session-subagent-tree">
              <div className="session-subagent-tree__root">
                <span><Network size={16} /></span>
                <div><strong>{session?.title || '当前 Session'}</strong><small>运行时所有者 · Pi Session</small></div>
              </div>
              {runsQuery.isPending ? <GraphState icon={<LoaderCircle size={17} />} label="正在读取运行图" spinning /> : null}
              {runsQuery.error ? (
                <GraphState icon={<TriangleAlert size={17} />} label="运行图暂时无法读取">
                  <Button size="small" loading={runsQuery.isFetching} onClick={() => void runsQuery.refetch()}>重新读取</Button>
                </GraphState>
              ) : null}
              {!runsQuery.isPending && !runsQuery.error && nodes.length === 0 ? (
                <GraphState icon={<CircleDashed size={17} />} label="还没有子 Agent；可在下方配置并启动。" />
              ) : null}
              {nodes.length ? <ol aria-label="子 Agent 节点">
                <SubagentGraphNodes
                  nodes={nodes}
                  runs={runs}
                  selectedRunId={selectedRunId}
                  onSelect={setSelectedRunId}
                />
              </ol> : null}
            </div>
          </section>

          {selectedRun ? (
            <SubagentNodeDetail
              childCount={selectedNode?.children.length ?? 0}
              parent={selectedParent}
              run={selectedRun}
              sessionId={sessionId}
              onOpen={onOpenRun ? () => onOpenRun(selectedRun) : undefined}
            />
          ) : null}

          <Disclosure className="session-subagent-launch" summary={<><Plus size={16} /><span>配置新的子 Agent</span><ChevronDown className="session-subagent-launch__chevron" size={15} /></>}>
            <SubagentLaunchPanel
              availableTools={tools}
              parents={[{
                sessionId,
                label: session?.title || '当前 Session',
                detail: session?.mode === 'coordinator' ? '主持 Session' : '助手 Session',
                canControl: session?.canControl,
                canWrite: session?.mode === 'coordinator'
                  && session.canControl !== false
                  && session.executionMode !== 'read_only'
                  && Boolean(session.workspaceRoots?.length),
                workspaceRoots: session?.workspaceRoots ?? [],
                piSkillsEnabled: session?.piSkillsEnabled,
                codexSkillsEnabled: session?.codexSkillsEnabled,
              }]}
            />
          </Disclosure>
          </>}
        </div>
      </> : null}
    </aside>
  );
});

function SubagentGraphNodes({
  nodes,
  runs,
  selectedRunId,
  onSelect,
  level = 0,
}: {
  nodes: readonly SubagentTreeNode[];
  runs: readonly AgentSubagentRunV1[];
  selectedRunId: string;
  onSelect: (runId: string) => void;
  level?: number;
}) {
  return nodes.map((node) => {
    const run = node.run;
    const state = subagentPresentationState(run);
    const parent = run.parentRunId ? runs.find((item) => item.id === run.parentRunId) : undefined;
    const owner = parent ? subagentTemplateLabel(parent.templateId) : 'Root';
    const failureReason = (run.state === 'failed' || run.state === 'timed_out') ? run.error.trim() : '';
    return (
      <li key={run.id} data-depth={level} data-state={state}>
        <span className="session-subagent-tree__edge" aria-hidden="true" />
        <button
          aria-current={selectedRunId === run.id ? 'true' : undefined}
          aria-label={`${subagentTemplateLabel(run.templateId)} · ${run.task || '未公开任务说明'} · ${subagentStateLabel(run)}${failureReason ? ` · ${failureReason}` : ''}`}
          className="session-subagent-node"
          data-selected={selectedRunId === run.id || undefined}
          onClick={() => onSelect(run.id)}
          type="button"
        >
            <span className="session-subagent-node__icon"><RunIcon run={run} /></span>
            <span className="session-subagent-node__identity">
              <strong>{subagentTemplateLabel(run.templateId)}</strong>
              <small>{owner} 派发 · {run.launchDigest.contextMode === 'fork' ? <><GitFork size={11} />Fork</> : '新上下文'}</small>
            </span>
            <span className="session-subagent-node__task">{run.task || '未公开任务说明'}</span>
            <i>{subagentStateLabel(run)}</i>
            {node.children.length ? <b>{node.children.length}</b> : null}
            {(run.state === 'failed' || run.state === 'timed_out') && run.error.trim() ? (
              <small className="session-subagent-node__error" title={run.error}>{run.error}</small>
            ) : null}
        </button>
        {node.children.length ? <ol aria-label={`${run.task} 的 Pattern 子调用`}>
          <SubagentGraphNodes
            nodes={node.children}
            runs={runs}
            selectedRunId={selectedRunId}
            onSelect={onSelect}
            level={level + 1}
          />
        </ol> : null}
      </li>
    );
  });
}

function SubagentNodeDetail({
  run,
  parent,
  childCount,
  sessionId,
  onOpen,
}: {
  run: AgentSubagentRunV1;
  parent?: AgentSubagentRunV1;
  childCount: number;
  sessionId: string;
  onOpen?: () => void;
}) {
  const failurePolicy = subagentFailurePolicy(run);
  const owner = parent ? subagentTemplateLabel(parent.templateId) : 'Root';
  const active = run.state === 'queued' || run.state === 'running';
  return (
    <section className="session-subagent-detail" aria-label="子 Agent 节点详情">
      <header>
        <span className="session-subagent-node__icon"><RunIcon run={run} /></span>
        <span><strong>{subagentTemplateLabel(run.templateId)}</strong><small>第 {run.attemptNumber} 次尝试 · {subagentStateLabel(run)}</small></span>
        <div className="session-subagent-detail__actions">
          {onOpen ? <IconButton icon={<PanelsTopLeft size={15} />} label="弹成卫星窗" onClick={onOpen} tooltip /> : null}
          <SubagentConsoleDialog run={run} sessionId={sessionId} triggerLabel={active ? '打开进度' : '打开结果'} />
        </div>
      </header>
      <p className="session-subagent-detail__task">{run.task || '未公开任务说明'}</p>
      <div className="session-subagent-detail__flow" aria-label="节点流转">
        <span>由{owner}派发</span><i aria-hidden="true">→</i><span>返回给 {owner}</span>
        {childCount ? <small>等待 {childCount} 个下游节点返回</small> : <small>当前节点直接交付</small>}
      </div>
      <dl>
        <div><dt>上下文</dt><dd>{run.launchDigest.contextMode === 'fork' ? 'Fork 当前 Session' : '新上下文'}</dd></div>
        <div><dt>用量</dt><dd>{run.usage.turnCount} 回合 · {run.usage.toolCount} 工具 · {formatCount(run.usage.totalTokens)} Token</dd></div>
        <div><dt>交付</dt><dd>{run.expectedOutput || '未声明公开交付形态'}</dd></div>
        {run.todoTask ? <div><dt>导航</dt><dd>{run.todoPhase ? `${run.todoPhase} · ` : ''}{run.todoTask}</dd></div> : null}
      </dl>
      {run.error ? <p className="session-subagent-detail__error" role="alert">{run.error}</p> : null}
      {failurePolicy ? <p className="session-subagent-detail__recovery"><ShieldAlert size={13} />{failurePolicy}</p> : null}
    </section>
  );
}

function findTreeNode(
  nodes: readonly SubagentTreeNode[],
  runId: string,
): SubagentTreeNode | undefined {
  for (const node of nodes) {
    if (node.run.id === runId) return node;
    const child = findTreeNode(node.children, runId);
    if (child) return child;
  }
  return undefined;
}

function GraphState({ icon, label, spinning = false, children }: {
  icon: ReactNode;
  label: string;
  spinning?: boolean;
  children?: ReactNode;
}) {
  return <div className="session-subagent-graph__state" data-spinning={spinning || undefined}>
    {icon}<span>{label}</span>{children}
  </div>;
}

function RunIcon({ run }: { run: AgentSubagentRunV1 }) {
  if (run.state === 'running') return <LoaderCircle size={15} />;
  if (run.state === 'queued') return <CircleDashed size={15} />;
  if (isContractInvalid(run) || run.state === 'failed' || run.state === 'timed_out') return <TriangleAlert size={15} />;
  if (run.state === 'completed') return <CheckCircle2 size={15} />;
  return <Bot size={15} />;
}

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.max(0, Math.round(value)));
}
