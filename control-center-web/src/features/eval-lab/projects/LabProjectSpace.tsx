import { useState, type ReactNode } from 'react';
import { ChevronRight, CircleAlert, Clock3, FolderOpen, GitBranch, LoaderCircle } from 'lucide-react';
import { Button } from '@/components/primitives';
import { LabWorkflowNodeDetail, workflowNodeStatus } from './LabWorkflowGraph';
import type { LabWorkflowNode } from './project-workflow-types';
import type { LabProject } from './types';
import './lab-project-space.css';
import { LabProjectFlow } from './LabProjectFlow';
import { LabOptimizationCompare } from './LabOptimizationCompare';

export { testedDimensions } from './optimization-parameters';

export function LabProjectSpace({ project, artifactId, artifactContent, onSelectArtifact, onOpenNode, onOpenGraph, onOpenRuns, onOpenApps, onOpenChat, onOpenFile, sourceContent, onOpenSource, onCloseSource, selectedNodeId, onSelectNode, onDraftOptimization, onOpenKnowledge, preferenceKey }: {
  preferenceKey?: string;
  onDraftOptimization?: (text: string) => void; onOpenKnowledge?: () => void;
  selectedNodeId?: string; onSelectNode?: (id: string) => void;
  project: LabProject; artifactId: string; artifactContent: ReactNode;
  onSelectArtifact: (id: string) => void; onOpenNode: (node: LabWorkflowNode) => void;
  onOpenGraph: () => void; onOpenRuns: () => void; onOpenApps: () => void; onOpenChat: () => void; onOpenFile: (path: string) => void;
  sourceContent?: ReactNode; onOpenSource?: (node: LabWorkflowNode) => void; onCloseSource?: () => void;
}) {
  const nodes = project.workflow?.nodes ?? [];
  const experiments = nodes.filter((node) => node.kind === 'experiment');
  const [localSelection, setLocalSelection] = useState('');
  const selection = selectedNodeId ?? localSelection;
  const setSelection = (id: string) => { setLocalSelection(id); onSelectNode?.(id); };
  const [activityOpen, setActivityOpen] = useState(false);
  const [showArtifact, setShowArtifact] = useState(false);
  const [flowOpen, setFlowOpen] = useState(false);
  const selected = nodes.find((node) => node.id === selection);
  const active = nodes.filter((node) => node.source === 'runtime' && ['running', 'queued', 'failed', 'interrupted'].includes(node.status));
  const counts = project.workflow?.counts;
  const current = nodes.find((node) => node.id === project.workflow?.currentNodeId);
  const selectNode = (node: LabWorkflowNode) => { setShowArtifact(false); setSelection(node.id); setActivityOpen(false); };
  const selectArtifact = (id: string) => { setShowArtifact(true); setSelection(''); onSelectArtifact(id); };
  return <section className="lab-space" aria-label="项目成果工作面">
    <div className="lab-space__activity"><button aria-expanded={activityOpen} onClick={() => setActivityOpen(!activityOpen)}><span className="lab-space__activity-mark" data-active={Boolean(counts?.running)} /><strong>{counts?.running ? `${counts.running} 项后台工作正在运行` : counts?.queued ? `${counts.queued} 项后台工作排队中` : '后台工作'}</strong>{counts ? <span>{counts.completed} 已完成{counts.failed ? ` · ${counts.failed} 失败` : ''}</span> : <span>状态尚未返回</span>}<ChevronRight size={14} /></button><div className="lab-space__actions"><select aria-label="查看项目内容" value={showArtifact ? `artifact:${artifactId}` : 'compare'} onChange={(event) => { if (event.target.value === 'compare') { setShowArtifact(false); setSelection(''); } else selectArtifact(event.target.value.slice('artifact:'.length)); }}><option value="compare">优化对比 · {experiments.length} 轮</option>{project.artifacts.map((artifact) => <option key={artifact.artifactId} value={`artifact:${artifact.artifactId}`}>{artifact.title} · v{artifact.revision}</option>)}</select><button aria-expanded={flowOpen} onClick={() => setFlowOpen(!flowOpen)}>项目流程与材料状态</button><Button size="small" onClick={onOpenGraph}><GitBranch size={14} />项目总览</Button><button aria-label="项目文件夹" title="项目文件夹" disabled={!project.directory?.path} onClick={() => project.directory && onOpenFile(project.directory.path)}><FolderOpen size={15} /></button></div></div>
    {project.workflow?.complete === false ? <p className="lab-project-notice" role="status">部分后台记录暂不可读取，当前只展示已返回的工作。</p> : null}
    {activityOpen ? <section className="lab-space-activity" aria-label="全部后台工作"><header><h3>后台工作与需要处理的任务</h3><Button size="small" onClick={onOpenRuns}>打开执行与恢复</Button></header>{active.length ? active.map((node) => <button key={node.id} onClick={() => selectNode(node)}>{node.status === 'running' ? <LoaderCircle size={15} /> : node.status === 'queued' ? <Clock3 size={15} /> : <CircleAlert size={15} />}<span><strong>{node.title}</strong><small>{node.summary}</small></span><b>{workflowNodeStatus(node)}</b></button>) : <p>{project.workflow ? '没有返回正在运行、排队或需恢复的后台任务。' : '当前后台状态尚未返回，可以打开执行页面重新读取。'}</p>}</section> : null}
    {flowOpen ? <LabProjectFlow project={project} onSelectNode={selectNode} onOpenApps={onOpenApps} onOpenGraph={onOpenGraph} /> : null}
    {project.directory?.status === 'partial' || project.directory?.status === 'unavailable' ? <p className="lab-project-notice">项目文件快照暂未完整更新，已保存的项目记录仍保留。</p> : null}
    <div className="lab-space__content">
      {selected && selected.kind !== 'experiment' ? <><div className="lab-space__selection-toolbar"><Button size="small" onClick={() => setSelection('')}>返回优化对比</Button></div><LabWorkflowNodeDetail selected={selected} nodes={nodes} onOpenNode={onOpenNode} onSelect={selectNode} onOpenSource={onOpenSource} /></>
        : showArtifact ? <div className="lab-space__artifact">{artifactContent}</div>
          : <><LabOptimizationCompare key={preferenceKey} preferenceKey={preferenceKey} nodes={nodes} selected={selected} onSelect={(node) => node ? selectNode(node) : setSelection('')} onOpenNode={onOpenNode} onOpenSource={onOpenSource} onDraft={onDraftOptimization} onOpenKnowledge={onOpenKnowledge} />
            {!experiments.length && project.artifacts.length ? <div className="lab-space__artifact">{artifactContent}</div> : null}
            {!experiments.length && !project.artifacts.length ? <div className="lab-space__start"><h2>{current?.title ?? '从当前项目继续'}</h2><p>{current?.summary || project.description}</p>{current ? <Button onClick={() => selectNode(current)}>查看当前工作</Button> : null}<Button onClick={onOpenChat}>打开项目对话</Button></div> : null}</>}
      {sourceContent ? <aside className="lab-space-source" aria-label="来源阅读"><header><h3>来源阅读</h3><Button size="small" onClick={onCloseSource}>收起来源</Button></header>{sourceContent}</aside> : null}
    </div>
  </section>;
}
