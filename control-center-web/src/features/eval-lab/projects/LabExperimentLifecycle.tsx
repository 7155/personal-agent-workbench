import { ArrowRight, CheckCircle2, CircleDot, FileText, FlaskConical, PackageCheck, Play, Plus } from 'lucide-react';
import { useMemo, type ReactNode } from 'react';
import { Button } from '@/components/primitives';
import type { ArtifactSummary, LabProject } from './types';
import type { ProjectGuidanceMode } from './project-guidance';

type Props = {
  project: LabProject;
  onOpenArtifact: (artifactId: string) => void;
  onOpenRuns: () => void;
  onDirection: (direction: string) => void;
  onOpenMaterials?: () => void;
  onOpenKnowledge?: () => void;
  onAddMaterials?: () => void;
  onOpenApps?: () => void;
  onContinue?: (mode: ProjectGuidanceMode) => void;
  busy?: boolean;
};
const directions = [
  ['prompt', 'Prompt 优化'], ['model', 'Model 优化'], ['retrieval', '检索优化'],
  ['tool', 'Tool 优化'], ['workflow', 'MCP / Workflow 优化'],
] as const;

// These matches locate presentation documents only. A matching title never
// establishes dataset approval, a completed run or a successful experiment.
const artifactFor = (artifacts: ArtifactSummary[], ...names: string[]) => artifacts.find((item) => names.some((name) => item.title.includes(name)));
const titleOf = (artifact?: ArtifactSummary) => artifact?.title ?? '尚无记录';

export function LabExperimentLifecycle({ project, onOpenArtifact, onOpenRuns, onDirection, onOpenMaterials, onOpenKnowledge, onAddMaterials, onOpenApps, onContinue, busy = false }: Props) {
  const knowledge = project.knowledgeResources;
  const hasKnowledge = Boolean(knowledge?.documentCount);
  const hasMaterials = project.materialCount > 0 || hasKnowledge;
  const records = useMemo(() => ({
    materials: artifactFor(project.artifacts, '冻结材料', '材料目录'),
    overview: artifactFor(project.artifacts, '实验总览', '评测数据'),
    metrics: artifactFor(project.artifacts, '指标对照', '指标'),
    changes: artifactFor(project.artifacts, '改动与结论', '逐步实验卡', '变更'),
    snapshot: artifactFor(project.artifacts, '原始评测记录', '原始运行', '聊天记录'),
    best: artifactFor(project.artifacts, '最优方案', '最终方案', '总结'),
    failure: artifactFor(project.artifacts, '失败', '诊断', '问题'),
  }), [project.artifacts]);
  const binding = project.bindings.find((item) => ['queued', 'running'].includes(item.execution?.status ?? ''))
    ?? project.bindings.find((item) => item.ownerRef.kind === 'golden_suite');
  const execution = binding?.execution;
  const running = execution?.status === 'running' || execution?.status === 'queued';
  const completed = execution?.status === 'completed' && execution.latestJob?.kind === 'experiment';
  const noImprovement = completed && execution.latestJob?.decision === 'no_improvement';
  const failed = ['failed', 'cancelled', 'interrupted', 'unavailable'].includes(execution?.status ?? '');
  const outcome = noImprovement ? '本轮无提升，沿用基线' : completed ? '本轮评测已完成' : execution?.label ?? '尚未开始运行';
  const openRecord = (artifact: ArtifactSummary, label = '查看记录') => <Button size="small" variant="secondary" onClick={() => onOpenArtifact(artifact.artifactId)}>{label}<ArrowRight size={14} /></Button>;
  const step = (number: number, title: string, description: string, status: string, available: boolean, action: ReactNode, artifact?: ArtifactSummary) => (
    <li className="lab-lifecycle-stage" data-state={available ? 'available' : 'pending'}>
      <span className="lab-lifecycle-stage__index" aria-hidden="true">{number}</span>
      <div className="lab-lifecycle-stage__body">
        <div className="lab-lifecycle-stage__heading"><h3>{title}</h3><span className="lab-lifecycle-stage__status">{available ? <CheckCircle2 size={14} /> : <CircleDot size={14} />}{status}</span></div>
        <p>{description}</p>
        {artifact ? <span className="lab-lifecycle-stage__record"><FileText size={13} />{artifact.title}</span> : null}
        <div className="lab-lifecycle-stage__actions">{action}</div>
      </div>
    </li>
  );
  return <section className="lab-lifecycle" aria-label="实验闭环">
    <header className="lab-lifecycle__header"><div><h2>实验进展</h2><p>固定评测条件，比较质量与成本，再把验证过的方案交付为应用。</p></div><Button size="small" variant="secondary" onClick={onOpenRuns}><Play size={14} />查看原始运行记录</Button></header>
    <div className="lab-lifecycle-current" data-state={running ? 'running' : failed ? 'attention' : completed ? 'completed' : 'pending'} role="status">
      <FlaskConical size={19} /><div><strong>{outcome}</strong><p>{execution?.reason || project.nextAction?.reason || '先接入项目材料，再和项目 Agent 确定评测任务与通过标准。'}</p></div>
      {running || failed ? <Button size="small" onClick={onOpenRuns}>{running ? '查看模型运行' : '处理运行问题'}</Button> : null}
    </div>
    {onContinue ? <div className="lab-lifecycle-continue"><div><h3>从当前进度继续</h3><p>Agent 检查已有工作，按实际结果推进。你可以随时在项目对话中补充方向或停止。</p></div><div><Button variant="primary" disabled={busy || running} onClick={() => onContinue('auto')}>自动推进优化</Button><Button variant="secondary" disabled={busy || running} onClick={() => onContinue('guided')}>带我逐步完成</Button></div></div> : null}
    <ol className="lab-lifecycle-stages" aria-label="从材料到应用的评测流程">
      {step(1, '材料与知识库', hasKnowledge ? `已接入 ${knowledge!.documentCount} 篇知识文档${knowledge!.chunkCount ? `，已有索引 ${knowledge!.chunkCount} 个切片` : ''}。可沿用当前资源继续评测。` : project.materialCount ? `已接入 ${project.materialCount} 份材料，可查看内容、来源和接入问题。` : '添加业务资料、已有代码或失败案例，让评测围绕你的实际任务展开。', hasMaterials ? '已接入材料' : '待添加材料', hasMaterials,
        <>{onAddMaterials ? <Button size="small" variant={hasMaterials ? 'secondary' : 'primary'} onClick={onAddMaterials}><Plus size={14} />添加材料</Button> : null}{project.materialCount && onOpenMaterials ? <Button size="small" variant="secondary" onClick={onOpenMaterials}>查看材料</Button> : null}{hasKnowledge && onOpenKnowledge ? <Button size="small" variant="secondary" onClick={onOpenKnowledge}>查看知识库</Button> : null}{!hasMaterials && onContinue ? <Button size="small" variant="secondary" disabled={busy || running} onClick={() => onContinue('sample')}>用示例走通流程</Button> : null}{records.materials ? openRecord(records.materials, '查看材料记录') : null}</>, records.materials)}
      {step(2, '评测集与通过标准', '使用你提供的题目，或让 Agent 起草评测集。先核对题目和标准，再开始比较。', records.overview ? '有评测文档' : completed ? '回执已保存在运行中' : '等待评测集', Boolean(records.overview) || completed,
        <>{records.overview ? openRecord(records.overview, '查看评测文档') : <Button size="small" variant="secondary" disabled={busy || running} onClick={() => onDirection('准备评测集与基线运行')}>与 Agent 准备评测</Button>}{binding ? <Button size="small" variant="secondary" onClick={onOpenRuns}>查看评测与运行</Button> : null}</>, records.overview)}
      {step(3, '运行与原始记录', '在同一评测条件下运行基线与候选，保留每个案例的回答、工具调用和结果。', completed ? '运行已完成' : running ? '运行中' : records.snapshot ? '有运行文档' : '待运行', completed || Boolean(records.snapshot),
        <>{records.snapshot ? openRecord(records.snapshot, '查看运行文档') : null}<Button size="small" variant="secondary" onClick={onOpenRuns}><Play size={14} />{completed ? '查看本轮回执' : '查看运行'}</Button></>, records.snapshot)}
      {step(4, '指标对照与下一步优化', '每轮只改变一个主要方向。比较通过率、成本和失败案例，并保留此前结果。', records.metrics ? '有指标文档' : completed ? '判定已记录' : '待评测结果', Boolean(records.metrics) || completed,
        <>{records.metrics ? openRecord(records.metrics, '查看指标对照') : null}<Button size="small" variant="secondary" onClick={onOpenRuns}>查看历史运行</Button><div className="lab-lifecycle-directions" aria-label="选择下一轮优化方向">{directions.map(([id, label]) => <Button key={id} size="small" variant="secondary" disabled={busy || running} onClick={() => onDirection(label)} data-direction={id}>{label}</Button>)}</div>{running ? <span className="lab-lifecycle-help">当前运行结束后，可以选择下一轮优化方向。</span> : <span className="lab-lifecycle-help">选择方向会交给项目 Agent；新的结果以实际运行记录为准。</span>}</>, records.metrics)}
      {step(5, '方案与应用交付', noImprovement ? '本轮没有获得更好的方案，保留基线。你仍可以查看改动依据，或继续验证其他方向。' : '汇总已验证的配置与改动，先试用，再添加至 PAW 或导出独立应用。', records.best ? '有方案文档' : noImprovement ? '沿用基线' : '待整理方案', Boolean(records.best) || noImprovement,
        <>{records.best ? openRecord(records.best, '打开总结') : records.changes ? openRecord(records.changes, '查看改动与结论') : null}{onOpenApps ? <Button size="small" variant="secondary" onClick={onOpenApps}><PackageCheck size={14} />前往应用交付</Button> : null}</>, records.best ?? records.changes)}
    </ol>
    <details className="lab-lifecycle-causality">
      <summary>失败证据与修改依据{records.failure ? ' · 已有记录' : ''}</summary>
      <p>{records.failure?.summary || '完成一次基线运行后，再根据实际失败案例选择修改方向。当前尚无关联的失败文档。'}</p>
      {records.failure ? openRecord(records.failure, '查看失败记录') : null}
      <div className="lab-lifecycle-causality__links"><span>变更：{titleOf(records.changes)}</span><span>指标：{titleOf(records.metrics)}</span><span>原始记录：{titleOf(records.snapshot)}</span></div>
    </details>
    <footer>{project.artifactCount} 份成果文档 · {project.bindings.length} 个执行绑定<span>文档状态与真实运行状态分别展示。</span></footer>
  </section>;
}
