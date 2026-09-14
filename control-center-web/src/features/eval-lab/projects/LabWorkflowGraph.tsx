import { ArrowRight, ArrowUpRight, Check, CheckCircle2, Circle, CircleAlert, Clock3, Database, FileText, FlaskConical, Focus, GitBranch, Layers3, LoaderCircle, Minus, PackageCheck, Plus, X } from 'lucide-react';
import { useCallback, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Button, IconButton } from '@/components/primitives';
import { ApplicationMethodDiff } from '../golden/ApplicationMethod';
import type { LabProjectWorkflow, LabWorkflowKind, LabWorkflowMetric, LabWorkflowNode, LabWorkflowStatus } from './project-workflow-types';
import './lab-workflow-graph.css';

const statusNames: Record<LabWorkflowStatus, string> = { pending: '待准备', queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', unavailable: '待补齐' };
const kindNames: Record<LabWorkflowKind, string> = { materials: '原始材料', corpus: '语料处理', index: '检索索引', dataset: '评测集', calibration: '基线校准', experiment: '优化实验', artifact: '实验成果', application: '应用版本', job: '验证任务', step: '项目步骤' };
const nodeWidth = 300; const columnGap = 48; const rowGap = 32;
function StatusIcon({ status, size = 15 }: { status: LabWorkflowStatus; size?: number }) {
  const Icon = status === 'completed' ? CheckCircle2 : status === 'running' ? LoaderCircle : status === 'queued' ? Clock3
    : status === 'failed' || status === 'interrupted' || status === 'unavailable' ? CircleAlert : status === 'cancelled' ? X : Circle;
  return <Icon size={size} aria-hidden="true" className={status === 'running' ? 'lab-flow__running-icon' : undefined} />;
}
export function workflowDecisionName(decision?: string) {
  if (!decision) return '效果尚未判定';
  return ({ keep: '保留候选', reject: '不保留候选', no_improvement: '无提升 · 沿用基线', inconclusive: '结论不足 · 沿用基线', unknown: '效果尚未判定', blocked: '判定受阻' } as Record<string, string>)[decision.toLowerCase()] ?? decision;
}
export function workflowNodeStatus(node: LabWorkflowNode): string {
  if (node.source === 'artifact' && node.kind === 'step') return ({ running: '计划进行中', completed: '计划已记录完成', pending: '计划待准备', unavailable: '计划待补齐' } as Record<string, string>)[node.status] ?? `计划：${statusNames[node.status]}`;
  if (node.source === 'artifact' && node.status === 'completed') return node.kind === 'experiment' ? '历史已完成' : '已保存';
  return statusNames[node.status];
}
function kindIcon(kind: LabWorkflowKind) { return kind === 'experiment' || kind === 'calibration' ? FlaskConical : kind === 'application' ? PackageCheck : kind === 'artifact' ? FileText : kind === 'job' ? GitBranch : Database; }

/** Position only actual owner nodes. Parallel tasks occupy sibling rows; no future rounds are invented. */
export function layoutWorkflow(nodes: LabWorkflowNode[], edges: { source: string; target: string }[] = []) {
  const positions = new Map<string, { x: number; y: number; height: number; stage: number }>();
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const owner = (id: string) => {
    const seen = new Set<string>(); let node = byId.get(id);
    while (node?.parentId && byId.has(node.parentId) && !seen.has(node.parentId)) { seen.add(node.id); node = byId.get(node.parentId); }
    return node?.id ?? id;
  };
  const roots = nodes.filter((node) => owner(node.id) === node.id);
  const dependencies = new Map(roots.map((node) => [node.id, new Set<string>()]));
  const connect = (from: string, to: string) => {
    const source = owner(from); const target = owner(to);
    if (source !== target && dependencies.has(source) && dependencies.has(target)) dependencies.get(target)!.add(source);
  };
  edges.forEach((edge) => connect(edge.source, edge.target));
  nodes.forEach((node) => node.dependencies.forEach((id) => connect(id, node.id)));
  const ranks = new Map<string, number>();
  let pending = [...roots];
  while (pending.length) {
    const ready = pending.filter((node) => [...dependencies.get(node.id)!].every((id) => ranks.has(id)));
    if (!ready.length) break;
    ready.forEach((node) => ranks.set(node.id, Math.max(0, ...[...dependencies.get(node.id)!].map((id) => ranks.get(id)! + 1))));
    pending = pending.filter((node) => !ranks.has(node.id));
  }
  // A corrupt dependency/parent cycle stays visible; it never becomes a completed step.
  const unresolvedRank = ranks.size ? Math.max(...ranks.values()) + 1 : 0;
  pending.forEach((node) => ranks.set(node.id, unresolvedRank));
  const heights = new Map<number, number>(); const visited = new Set<string>();
  function place(node: LabWorkflowNode, stage: number, depth = 0) {
    if (visited.has(node.id)) return;
    visited.add(node.id);
    const height = node.kind === 'experiment' ? 194 : node.parentId ? 136 : 158;
    const y = heights.get(stage) ?? 40;
    positions.set(node.id, { x: 28 + stage * (nodeWidth + columnGap) + Math.min(depth, 1) * 14, y, height, stage });
    heights.set(stage, y + height + rowGap);
    nodes.filter((child) => child.parentId === node.id).forEach((child) => place(child, stage, depth + 1));
  }
  roots.forEach((node) => place(node, ranks.get(node.id) ?? 0));
  nodes.forEach((node) => { if (!visited.has(node.id)) place(node, unresolvedRank); });
  return { positions, width: Math.max(700, (Math.max(0, ...[...positions.values()].map((pos) => pos.stage)) + 1) * (nodeWidth + columnGap) + 10), height: Math.max(420, ...heights.values()) };
}

export function WorkflowMetricComparison({ metric }: { metric: LabWorkflowMetric }) {
  const max = Math.max(Math.abs(metric.baseline ?? 0), Math.abs(metric.candidate ?? 0), 0.000001);
  const display = (value: number | null) => value === null ? '未回报' : metric.unit === 'ratio' ? `${Number((value * 100).toFixed(2)).toLocaleString()}%` : `${Number(value.toFixed(4)).toLocaleString()}${metric.unit ?? ''}`;
  if (metric.value !== undefined) return <div className="lab-flow-metric"><div className="lab-flow-metric__title"><strong>{metric.label}</strong><span>本次结果{metric.sampleCount !== undefined ? ` · ${metric.sampleCount} 题` : ''}</span></div><b>{display(metric.value)}</b><small>未指定配对基线</small></div>;
  const delta = metric.baseline !== null && metric.candidate !== null ? metric.candidate - metric.baseline : null;
  return <div className="lab-flow-metric"><div className="lab-flow-metric__title"><strong>{metric.label}</strong><span>{delta === null ? '暂不能比较' : `变化 ${delta > 0 ? '+' : ''}${Number((delta * (metric.unit === 'ratio' ? 100 : 1)).toFixed(4)).toLocaleString()}${metric.unit === 'ratio' ? ' 个百分点' : metric.unit ?? ''}`}</span></div>
    {(['baseline', 'candidate'] as const).map((role) => <div className="lab-flow-metric__row" key={role}><span>{role === 'baseline' ? '基线' : '候选'}</span><span className="lab-flow-metric__track" aria-hidden="true"><i data-role={role} style={{ width: metric[role] === null ? 0 : `${Math.abs(metric[role]) / max * 100}%` }} /></span><b>{display(metric[role])}</b></div>)}
  </div>;
}

export function LabWorkflowGraph({ projectId, connection, workflow, onOpenNode, onOpenMaterials, onOpenRuns, onOpenApps, onOpenExperiments }: {
  projectId: string; connection: string; workflow?: LabProjectWorkflow;
  onOpenNode: (node: LabWorkflowNode) => void; onOpenMaterials: () => void; onOpenRuns: () => void; onOpenApps: () => void; onOpenExperiments: () => void;
}) {
  const storageKey = `paw.lab.workflow-selection.v1:${connection}:${projectId}`;
  const readSelection = () => { try { return localStorage.getItem(storageKey) ?? ''; } catch { return ''; } };
  const [selection, setSelection] = useState(() => ({ key: storageKey, id: readSelection() }));
  const selectedId = selection.key === storageKey ? selection.id : readSelection();
  const [zoom, setZoom] = useState(1);
  const viewport = useRef<HTMLDivElement>(null);
  const positionedProject = useRef('');
  const edgeId = useId().replace(/:/g, '');
  const nodes = workflow?.nodes ?? [];
  const layout = useMemo(() => layoutWorkflow(nodes, workflow?.edges), [nodes, workflow?.edges]);
  const selected = nodes.find((node) => node.id === selectedId) ?? nodes.find((node) => node.id === workflow?.currentNodeId) ?? nodes.find((node) => node.kind === 'experiment') ?? nodes[0];
  const current = nodes.find((node) => node.id === workflow?.currentNodeId);
  const select = (node: LabWorkflowNode) => { setSelection({ key: storageKey, id: node.id }); try { localStorage.setItem(storageKey, node.id); } catch { /* Selection remains usable in this window. */ } };
  const centerNode = useCallback((id: string) => {
    const canvas = viewport.current; const pos = layout.positions.get(id);
    if (!canvas || !pos || !canvas.clientWidth || !canvas.clientHeight || typeof canvas.scrollTo !== 'function') return false;
    // Layout coordinates belong to this scroll container; never scroll its hosts.
    canvas.scrollTo({ left: Math.max(0, (pos.x + nodeWidth / 2) * zoom - canvas.clientWidth / 2),
      top: Math.max(0, (pos.y + pos.height / 2) * zoom - canvas.clientHeight / 2), behavior: 'instant' });
    return true;
  }, [layout, zoom]);
  const initialNodeId = selected?.id;
  useLayoutEffect(() => {
    if (!initialNodeId || positionedProject.current === storageKey) return;
    const position = () => {
      if (positionedProject.current === storageKey) return true;
      if (!centerNode(initialNodeId)) return false;
      positionedProject.current = storageKey; return true;
    };
    if (position() || !viewport.current || typeof ResizeObserver === 'undefined') return;
    // A hidden host may have no viewport yet. Position once when it becomes visible.
    const observer = new ResizeObserver(() => { if (position()) observer.disconnect(); });
    observer.observe(viewport.current); return () => observer.disconnect();
  }, [storageKey, initialNodeId, centerNode]);
  const focusNode = (node?: LabWorkflowNode) => { if (!node) return; select(node); if (centerNode(node.id)) positionedProject.current = storageKey; };
  return <section className="lab-flow" aria-label="项目工作流">
    <header className="lab-flow__header"><div><h2>项目工作流</h2><p>{current ? <>当前：<strong>{current.title}</strong><span className={`lab-flow-status lab-flow-status--${current.status}`}><StatusIcon status={current.source === 'artifact' && current.status === 'running' ? 'pending' : current.status} />{workflowNodeStatus(current)}</span></> : workflow ? '查看每个已保存节点，沿着依赖继续工作。' : '正在等待执行器返回完整工作流；已有材料和成果仍可打开。'}</p></div>
      <Button size="small" onClick={onOpenExperiments}><FlaskConical size={15} />实验与优化</Button>
    </header>
    {workflow ? <div className="lab-flow__activity" aria-label="后台任务状态"><span><LoaderCircle size={14} />运行中 <b>{workflow.counts.running}</b></span><span><Clock3 size={14} />排队中 <b>{workflow.counts.queued}</b></span><span><CheckCircle2 size={14} />已完成 <b>{workflow.counts.completed}</b></span><span><CircleAlert size={14} />失败 <b>{workflow.counts.failed}</b></span><small>后台任务 · 子验证展开在实验下方</small></div> : null}
    {workflow && workflow.complete === false ? <p className="lab-flow__incomplete" role="status">部分执行记录暂不可读取{workflow.unavailableOwners?.length ? `：${workflow.unavailableOwners.join('、')}` : ''}。已返回的节点仍可查看。</p> : null}
    <div className="lab-flow__canvas-shell">
      <p className="lab-flow__canvas-caption">左→右是依赖顺序；点击节点查看改动、指标、验证任务和来源。流程较长时可横向滚动，定位当前会自动居中。</p>
      <div className="lab-flow__canvas" ref={viewport} role="region" aria-label="实验节点画布" tabIndex={0}>
        <div className="lab-flow__scaled" style={{ width: layout.width * zoom, height: layout.height * zoom }}><div className="lab-flow__plane" style={{ width: layout.width, height: layout.height, transform: `scale(${zoom})` }}>

          <svg className="lab-flow__edges" width={layout.width} height={layout.height} aria-hidden="true"><defs><marker id={`${edgeId}-arrow`} markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7" /></marker></defs>{workflow?.edges.map((edge) => {
            const from = layout.positions.get(edge.source); const to = layout.positions.get(edge.target); if (!from || !to) return null;
            const active = selected?.id === edge.source || selected?.id === edge.target;
            const same = from.stage === to.stage; const x1 = same ? from.x + 14 : from.x + nodeWidth; const y1 = same ? from.y + from.height : from.y + from.height / 2;
            const x2 = same ? to.x + 14 : to.x; const y2 = same ? to.y : to.y + to.height / 2;
            return <path key={`${edge.source}:${edge.target}`} className={active ? 'is-selected' : undefined} markerEnd={`url(#${edgeId}-arrow)`} d={same ? `M${x1} ${y1} C${x1} ${y1 + 14},${x2} ${y2 - 14},${x2} ${y2}` : `M${x1} ${y1} C${x1 + 30} ${y1},${x2 - 30} ${y2},${x2} ${y2}`} />;
          })}</svg>
          {!nodes.length ? <div className="lab-flow__empty"><GitBranch size={32} /><h3>{workflow ? '还没有实验节点' : '工作流记录尚未返回'}</h3><p>项目 Agent 规划的任务与实际执行记录会出现在这里。可以在项目对话中调整步骤、方向和依赖。</p><div><Button onClick={onOpenMaterials}>查看项目材料</Button><Button onClick={onOpenRuns}>查看运行记录</Button><Button onClick={onOpenApps}>查看应用交付</Button></div></div> : null}
          {nodes.map((node) => { const pos = layout.positions.get(node.id)!; const Icon = kindIcon(node.kind); const childCount = nodes.filter((child) => child.parentId === node.id).length;
            return <button type="button" key={node.id} className={`lab-flow-node lab-flow-node--${node.kind}`} data-status={node.source === 'artifact' && node.status === 'running' ? 'pending' : node.status} data-child={Boolean(node.parentId)} aria-pressed={selected?.id === node.id} aria-label={`${node.title} · ${workflowNodeStatus(node)}`} onClick={() => select(node)} style={{ left: pos.x, top: pos.y, width: nodeWidth, height: pos.height }}>
              <span className="lab-flow-node__type"><Icon size={15} />{kindNames[node.kind]}{node.ref.version !== undefined ? <span>v{node.ref.version}</span> : null}</span>
              <strong className="lab-flow-node__title">{node.title}</strong><span className="lab-flow-node__summary">{node.summary || '等待执行器补充记录'}</span>
              <span className="lab-flow-node__bottom"><span className={`lab-flow-status lab-flow-status--${node.status}`}><StatusIcon status={node.source === 'artifact' && node.status === 'running' ? 'pending' : node.status} />{workflowNodeStatus(node)}</span>{node.kind === 'experiment' ? <span>{workflowDecisionName(node.decision)}</span> : childCount > 0 ? <span>{childCount} 个子任务</span> : node.source === 'artifact' ? <span>{node.kind === 'step' ? 'Agent 计划' : '已保存版本'}</span> : null}</span>
              {node.kind === 'experiment' && childCount > 0 ? <span className="lab-flow-node__children"><GitBranch size={12} />{childCount} 个验证任务</span> : null}
            </button>;
          })}
        </div></div>
      </div>
      <div className="lab-flow__canvas-tools" aria-label="画布工具"><IconButton icon={<Minus size={16} />} label="缩小画布" disabled={zoom <= 0.7} onClick={() => setZoom((value) => Math.max(0.7, value - 0.15))} /><button onClick={() => setZoom(1)} aria-label="恢复默认缩放">{Math.round(zoom * 100)}%</button><IconButton icon={<Plus size={16} />} label="放大画布" disabled={zoom >= 1.4} onClick={() => setZoom((value) => Math.min(1.4, value + 0.15))} /><span /><Button size="small" disabled={!current && !selected} onClick={() => focusNode(current ?? selected)}><Focus size={15} />定位当前</Button></div>
    </div>
    {selected ? <LabWorkflowNodeDetail selected={selected} nodes={nodes} onOpenNode={onOpenNode} onSelect={(node) => focusNode(node)} /> : null}
  </section>;
}

export function LabWorkflowNodeDetail({ selected, nodes, onOpenNode, onSelect, onOpenSource = onOpenNode }: { selected: LabWorkflowNode; nodes: LabWorkflowNode[]; onOpenNode: (node: LabWorkflowNode) => void; onSelect: (node: LabWorkflowNode) => void; onOpenSource?: (node: LabWorkflowNode) => void }) {
  const related = nodes.filter((node) => node.parentId === selected.id || selected.children?.includes(node.id));
  const parents = nodes.filter((node) => selected.dependencies.includes(node.id));
  const focusNode = onSelect;
  return <section className="lab-flow-detail" aria-label="所选节点详情"><header><div><span className={`lab-flow-status lab-flow-status--${selected.status}`}><StatusIcon status={selected.source === 'artifact' && selected.status === 'running' ? 'pending' : selected.status} />{workflowNodeStatus(selected)}</span><h3>{selected.title}</h3></div><Button size="small" onClick={() => onOpenNode(selected)}>{selected.status === 'completed' ? '查看结果' : selected.kind === 'application' ? '打开应用交付' : '打开任务详情'}<ArrowUpRight size={15} /></Button></header>
      <div className="lab-flow-detail__body"><div className="lab-flow-detail__record"><h4>{selected.kind === 'experiment' ? '这一轮改了什么' : '记录与结果'}</h4><p>{selected.summary || '执行器尚未补充此节点的详细说明。'}</p>{selected.kind === 'experiment' ? <p className="lab-flow-detail__decision"><Check size={14} />{workflowDecisionName(selected.decision)}</p> : null}
        {selected.optimization ? <dl className="lab-flow-detail__configuration"><dt>本轮方向</dt><dd>{selected.optimization.scope}</dd>{selected.optimization.baselineModel ? <><dt>基线模型</dt><dd>{selected.optimization.baselineModel}</dd></> : null}{selected.optimization.candidateModel ? <><dt>候选模型</dt><dd>{selected.optimization.candidateModel}</dd></> : null}{selected.optimization.promptChanged !== undefined ? <><dt>提示词</dt><dd>{selected.optimization.promptChanged ? '已调整' : '保持一致'}</dd></> : null}</dl> : null}
        {selected.reasons?.length ? <ul className="lab-flow-detail__reasons">{selected.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul> : null}
        {parents.length ? <div className="lab-flow-detail__relations"><span>依赖</span>{parents.map((node) => <button key={node.id} onClick={() => focusNode(node)}>{node.title}<ArrowRight size={12} /></button>)}</div> : null}
        <small>{selected.source === 'runtime' ? '执行器记录' : selected.kind === 'step' ? 'Agent 已保存的计划状态 · 不代表后台实时运行' : '已保存成果'}{selected.updatedAtMs ? ` · ${new Date(selected.updatedAtMs).toLocaleString()}` : ''}</small>
      </div><div className="lab-flow-detail__evidence">{selected.metrics?.length ? <><h4>基线与候选</h4>{selected.metrics.map((metric, index) => <WorkflowMetricComparison key={`${metric.label}:${index}`} metric={metric} />)}</> : <div className="lab-flow-detail__no-metrics"><Layers3 size={20} /><p>{selected.kind === 'experiment' ? '当前记录没有可比较的指标。完成状态不代表方案有提升。' : '此节点的材料或结果可从详情打开。'}</p></div>}</div></div>
      {selected.applicationMethodComparison ? <ApplicationMethodDiff comparison={selected.applicationMethodComparison} /> : null}
      {selected.factors?.length ? <div className="lab-flow-detail__changes"><table><caption>{selected.metrics?.some((metric) => metric.value !== undefined) ? '本次配置与说明 · 未指定配对基线' : '具体改动与原因'}</caption><thead><tr><th>调整项</th><th>之前</th><th>之后 / 本次</th><th>原因</th></tr></thead><tbody>{selected.factors.map((factor, index) => <tr key={index}><th scope="row">{factor.name}</th><td>{factor.before || '未记录'}</td><td>{factor.after || '未记录'}</td><td>{factor.reason || '未记录'}</td></tr>)}</tbody></table></div> : null}
      {selected.evidenceRefs?.some((ref) => ['artifact', 'runtime_session', 'runtime_turn', 'application_call'].includes(ref.kind)) ? <div className="lab-flow-detail__relations"><span>来源</span>{selected.evidenceRefs.filter((ref) => ['artifact', 'runtime_session', 'runtime_turn', 'application_call'].includes(ref.kind)).map((ref, index) => <button key={`${ref.kind}:${ref.id}`} onClick={() => onOpenSource({ ...selected, ...(ref.kind === 'artifact' ? { kind: 'artifact' as const } : {}), ref })}>{ref.kind === 'runtime_session' ? '查看原 Session' : ref.kind === 'runtime_turn' ? '查看原回合' : ref.kind === 'application_call' ? '查看复用的原调用' : `查看来源成果 ${index + 1}`}<ArrowUpRight size={12} /></button>)}</div> : null}
      {related.length ? <div className="lab-flow-detail__tasks"><h4>子任务 · {related.length}</h4>{related.map((node) => <button key={node.id} onClick={() => focusNode(node)}><StatusIcon status={node.source === 'artifact' && node.status === 'running' ? 'pending' : node.status} /><strong>{node.title}</strong><span>{workflowNodeStatus(node)}</span><ArrowUpRight size={13} /></button>)}</div> : null}
    </section>;
}
