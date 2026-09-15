import { useState, type ReactNode } from 'react';
import { ArrowUpRight, CheckCircle2, ChevronRight, CircleAlert, Clock3, FileText, FlaskConical, FolderOpen, GitBranch, LoaderCircle, MessageSquare, PackageCheck } from 'lucide-react';
import { Button } from '@/components/primitives';
import { LabWorkflowNodeDetail, WorkflowMetricComparison, workflowDecisionName, workflowNodeStatus } from './LabWorkflowGraph';
import type { LabWorkflowNode } from './project-workflow-types';
import type { LabProject } from './types';
import './lab-project-space.css';

const dimensions = ['RAG', 'Skill', 'Prompt', '模型', 'Tool', 'MCP / Workflow'] as const;
type Dimension = typeof dimensions[number];
/** A title is not evidence that a dimension was evaluated. Use the saved comparison scope/factors. */
export function testedDimensions(node: LabWorkflowNode): Dimension[] {
  if (node.kind !== 'experiment') return [];
  const scope = node.optimization?.scope ?? '';
  const factors = node.factors?.map((factor) => factor.name.toLowerCase()) ?? [];
  return dimensions.filter((dimension) => {
    if (dimension === 'Skill') return /(^|_)skill($|_)/.test(scope) || node.applicationMethodComparison?.changed === true;
    if (dimension === 'Prompt') return /prompt/.test(scope) || factors.some((name) => /prompt|提示词|回答规则/.test(name));
    if (dimension === '模型') return /model/.test(scope) || factors.some((name) => /model|模型/.test(name));
    if (dimension === 'RAG') return /retrieval|rag|chunk|index/.test(scope) || node.ref.kind === 'knowledge_job' || factors.some((name) => /retrieval|rag|chunk|检索|索引|切片|语料/.test(name));
    if (dimension === 'Tool') return /tool/.test(scope) || factors.some((name) => /tool|工具/.test(name));
    return /workflow|mcp/.test(scope) || factors.some((name) => /workflow|mcp|工作流/.test(name));
  });
}

export function LabProjectSpace({ project, artifactId, artifactContent, onSelectArtifact, onOpenNode, onOpenGraph, onOpenRuns, onOpenApps, onOpenChat, onOpenFile, sourceContent, onOpenSource, onCloseSource, selectedNodeId, onSelectNode }: {
  selectedNodeId?: string; onSelectNode?: (id: string) => void;
  project: LabProject; artifactId: string; artifactContent: ReactNode;
  onSelectArtifact: (id: string) => void; onOpenNode: (node: LabWorkflowNode) => void;
  onOpenGraph: () => void; onOpenRuns: () => void; onOpenApps: () => void; onOpenChat: () => void; onOpenFile: (path: string) => void;
  sourceContent?: ReactNode; onOpenSource?: (node: LabWorkflowNode) => void; onCloseSource?: () => void;
}) {
  const nodes = project.workflow?.nodes ?? [];
  const experiments = nodes.filter((node) => node.kind === 'experiment');
  const [localSelection, setLocalSelection] = useState(() => project.artifacts.every((item) => item.view === 'json') ? experiments.at(-1)?.id ?? '' : '');
  const selection = selectedNodeId ?? localSelection;
  const setSelection = (id: string) => { setLocalSelection(id); onSelectNode?.(id); };
  const [activityOpen, setActivityOpen] = useState(false);
  const selected = nodes.find((node) => node.id === selection);
  const active = nodes.filter((node) => node.source === 'runtime' && ['running', 'queued', 'failed', 'interrupted'].includes(node.status));
  const counts = project.workflow?.counts;
  const current = nodes.find((node) => node.id === project.workflow?.currentNodeId);
  const selectNode = (node: LabWorkflowNode) => { setSelection(node.id); setActivityOpen(false); };
  const selectArtifact = (id: string) => { setSelection(''); onSelectArtifact(id); };
  return <section className="lab-space" aria-label="项目成果工作面">
    <div className="lab-space__activity"><button aria-expanded={activityOpen} onClick={() => setActivityOpen(!activityOpen)}><span className="lab-space__activity-mark" data-active={Boolean(counts?.running)} /><strong>{counts?.running ? `${counts.running} 项后台工作正在运行` : counts?.queued ? `${counts.queued} 项后台工作排队中` : '后台工作'}</strong>{counts ? <span>{counts.completed} 已完成{counts.failed ? ` · ${counts.failed} 失败` : ''}</span> : <span>状态尚未返回</span>}<ChevronRight size={14} /></button><Button size="small" onClick={onOpenGraph}><GitBranch size={14} />项目总览</Button></div>
    {project.workflow?.complete === false ? <p className="lab-project-notice" role="status">部分后台记录暂不可读取，当前只展示已返回的工作。</p> : null}
    {activityOpen ? <section className="lab-space-activity" aria-label="全部后台工作"><header><h3>后台工作与需要处理的任务</h3><Button size="small" onClick={onOpenRuns}>打开执行与恢复</Button></header>{active.length ? active.map((node) => <button key={node.id} onClick={() => selectNode(node)}>{node.status === 'running' ? <LoaderCircle size={15} /> : node.status === 'queued' ? <Clock3 size={15} /> : <CircleAlert size={15} />}<span><strong>{node.title}</strong><small>{node.summary}</small></span><b>{workflowNodeStatus(node)}</b></button>) : <p>{project.workflow ? '没有返回正在运行、排队或需恢复的后台任务。' : '当前后台状态尚未返回，可以打开执行页面重新读取。'}</p>}</section> : null}
    <div className="lab-space__body"><aside className="lab-space__objects" aria-label="项目对象">
      <header><strong>成果与工作</strong><span>{project.artifacts.length} 份成果</span></header>
      <nav aria-label="工作成果">{project.artifacts.map((artifact) => <button key={artifact.artifactId} aria-current={!selected && artifactId === artifact.artifactId ? 'page' : undefined} onClick={() => selectArtifact(artifact.artifactId)} title={artifact.title}><FileText size={14} /><span>{artifact.title}<small>v{artifact.revision} · {artifact.view === 'html' ? '交互成果' : artifact.view === 'table' ? '对照表' : artifact.view === 'markdown' ? '文档' : '结果记录'}</small></span></button>)}</nav>
      {experiments.length ? <><h3>优化实验 <span>{experiments.length}</span></h3><nav aria-label="优化实验">{experiments.map((node) => <button key={node.id} aria-current={selection === node.id ? 'page' : undefined} onClick={() => selectNode(node)} title={node.title}><FlaskConical size={14} /><span>{node.title}<small>{workflowNodeStatus(node)} · {workflowDecisionName(node.decision)}</small></span></button>)}</nav></> : null}
      <footer><button onClick={onOpenApps}><PackageCheck size={15} />应用与导出<ArrowUpRight size={13} /></button><button disabled={!project.directory?.path} onClick={() => project.directory && onOpenFile(project.directory.path)}><FolderOpen size={15} />项目文件夹<ArrowUpRight size={13} /></button>{project.directory?.status === 'partial' ? <small>文件快照部分更新；项目记录仍保留。</small> : project.directory?.status === 'unavailable' ? <small>项目文件快照暂不可读取。</small> : null}</footer>
    </aside><div className="lab-space__content">
      <section className="lab-space-dimensions" aria-label="优化方向的实际测试记录"><header><h3>优化方向</h3><Button size="small" variant="quiet" onClick={onOpenChat}><MessageSquare size={14} />与 Agent 调整方向</Button></header><div>{dimensions.map((dimension) => {
        const records = experiments.filter((node) => testedDimensions(node).includes(dimension));
        const running = records.find((node) => node.source === 'runtime' && ['running', 'queued'].includes(node.status));
        const completed = records.filter((node) => node.status === 'completed');
        const target = running ?? completed.at(-1) ?? records.at(-1);
        const completedLabel = `${completed.length} 次已有${completed.some((node) => node.metrics?.some((metric) => metric.value !== undefined)) ? '评测' : '对照'}`;
        return <button key={dimension} aria-label={`${dimension} ${running ? workflowNodeStatus(running) : completed.length ? completedLabel : records.length ? '对照未完成' : '未记录测试'}`} disabled={!target} onClick={() => target && selectNode(target)} data-running={Boolean(running)}><strong>{dimension}</strong><span>{running ? workflowNodeStatus(running) : completed.length ? completedLabel : records.length ? '对照未完成' : '未记录测试'}</span></button>;
      })}</div></section>
      {selected ? <><div className="lab-space__selection-toolbar"><Button size="small" onClick={() => setSelection('')}>返回当前成果</Button><span>{selected.source === 'runtime' ? '实际执行记录' : '已保存的历史记录'}</span></div><LabWorkflowNodeDetail selected={selected} nodes={nodes} onOpenNode={onOpenNode} onSelect={selectNode} onOpenSource={onOpenSource} /></>
        : project.artifacts.length ? <div className="lab-space__artifact">{project.artifacts.find((item) => item.artifactId === artifactId)?.view === 'json' ? <div className="lab-space__start"><h2>{project.artifacts.find((item) => item.artifactId === artifactId)?.title}</h2><p>{project.artifacts.find((item) => item.artifactId === artifactId)?.summary || '已保存此结果，打开可查看完整记录。'}</p><Button onClick={() => onOpenNode({ id: artifactId, title: '结果记录', kind: 'artifact', status: 'completed', summary: '', dependencies: [], ref: { kind: 'artifact', id: artifactId }, source: 'artifact' })}>打开完整结果</Button></div> : artifactContent}</div>
          : <div className="lab-space__start"><h2>{current?.title ?? '从当前项目继续'}</h2><p>{current?.summary || project.description}</p>{current ? <Button onClick={() => selectNode(current)}>查看当前工作</Button> : null}<Button onClick={onOpenChat}>打开项目对话</Button></div>}
      {!selected && experiments.length ? <section className="lab-space-experiments" aria-label="近期优化对照"><header><h3>已经做过的优化</h3><span>保存原始结果，不因打开页面重复执行</span></header>{experiments.slice(-3).reverse().map((node) => <article key={node.id}><header><div><span>{testedDimensions(node).join(' · ') || '方向未记录'}</span><h4>{node.title}</h4></div><span className="lab-space-experiments__decision"><CheckCircle2 size={14} />{workflowDecisionName(node.decision)}</span></header><p>{node.summary}</p>{node.factors?.[0] ? <div className="lab-space-experiments__change"><span>{node.factors[0].name}</span><del>{node.factors[0].before || '未记录'}</del><ChevronRight size={15} /><strong>{node.factors[0].after || '未记录'}</strong></div> : null}<div className="lab-space-experiments__metrics">{node.metrics?.slice(0, 2).map((metric, index) => <WorkflowMetricComparison key={index} metric={metric} />)}</div><footer><span>{workflowNodeStatus(node)} · {nodes.filter((child) => child.parentId === node.id).length} 个子任务</span><Button size="small" onClick={() => selectNode(node)}>改动、结果与来源<ArrowUpRight size={13} /></Button></footer></article>)}</section> : null}
      {sourceContent ? <aside className="lab-space-source" aria-label="来源阅读"><header><h3>来源阅读</h3><Button size="small" onClick={onCloseSource}>收起来源</Button></header>{sourceContent}</aside> : null}
    </div></div>
  </section>;
}
