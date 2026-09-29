/** Read-only README illustration. Only allowlisted public experiment metadata is loaded. */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { LabProjectWorkbench } from '@/features/eval-lab/projects/LabProjectWorkbench';
import '@/design/tokens.css';
import '@/design/typography.css';
import '@/components/primitives/primitives.css';
import '@/features/eval-lab/eval-lab.css';
import scene from '../readme-lab/scene.json';
import '../readme-lab/preview.css';

const params = new URLSearchParams(location.search);
document.documentElement.dataset.theme = params.get('theme') === 'dark' ? 'dark' : 'light';
const experiment = scene.experiments[0];
const record = {
  ...experiment, experimentId: experiment.id, projectionState: 'current',
  baseline: { ...experiment.baseline, evidenceRefs: experiment.evidence.map((row) => `eval/interview-metrics/runs/${row.file}`) },
  candidate: { ...experiment.candidate, evidenceRefs: experiment.evidence.map((row) => `eval/interview-metrics/runs/${row.file}`) },
  comparison: {
    decision: experiment.decision,
    decisionReason: '同一组 3 个 Validation 任务：仅换模型有 1 个任务失败；明确枚举约束后，候选通过 3/3。成本是 Runtime 核对过的估算，不是账单。',
    costAuthority: experiment.costAuthority,
    validationBoundary: experiment.validationBoundary,
    stageChain: experiment.stages.map((stage) => ({ ...stage.metrics, ...stage })),
  },
};
const snapshot = { schemaVersion: 'paw.lab-imported-experiments.v1', executionPerformed: false, sourceHash: scene.sourceSha256, experiments: [record] };
const stamp = Date.parse('2026-09-04T12:00:00Z');
const artifacts = [
  { artifactId: 'overview', title: '实验总览', kind: 'experiment_history', view: 'table', content: { columns: ['experiment', 'state', 'dataset', 'split', 'cases', 'decision'].map((key) => ({ key, label: key })), rows: [{ experiment: experiment.title, state: 'current', dataset: experiment.dataset.id, split: experiment.dataset.split, cases: experiment.dataset.caseCount, decision: experiment.decision }] } },
  { artifactId: 'metrics', title: '指标对照', kind: 'experiment_history', view: 'table', content: { columns: ['experiment', 'metric', 'baseline', 'candidate'].map((key) => ({ key, label: key })), rows: Object.keys(experiment.baseline.metrics).map((key) => ({ experiment: experiment.title, metric: key, baseline: experiment.baseline.metrics[key as keyof typeof experiment.baseline.metrics], candidate: experiment.candidate.metrics[key as keyof typeof experiment.candidate.metrics] })) } },
  { artifactId: 'source', title: '原始评测记录', kind: 'experiment_snapshot', view: 'json', content: snapshot },
].map((row) => ({ ...row, revision: 1, summary: '', actions: [], templateRef: null, createdAtMs: stamp, updatedAtMs: stamp }));
const stageTitles = ['Sol · 起始基线', 'Luna · 仅替换模型', 'Luna · 明确枚举约束'];
const nodes = experiment.stages.map((stage, index) => {
  const before = experiment.stages[Math.max(0, index - 1)];
  const pair = (baseline: number, candidate: number) => index === 0 ? { baseline: null, candidate: null, value: candidate } : { baseline, candidate };
  return { id: stage.stage, kind: 'experiment', status: 'completed', source: 'artifact', title: stageTitles[index],
    summary: index === 0 ? '初始方案的历史结果。' : index === 1 ? '固定其他控制，只更换模型；质量未达标，不保留。' : '固定 Luna，只明确 Prompt 枚举约束。',
    dependencies: index === 0 ? [] : [experiment.stages[index - 1].stage],
    ref: { kind: 'experiment_record', id: experiment.id }, decision: stage.decision,
    optimization: { scope: index === 0 ? '' : index === 1 ? 'model' : 'prompt' },
    factors: index === 0 ? [] : [experiment.factors[index - 1]],
    reasons: [record.comparison.decisionReason],
    evidenceRefs: [{ kind: 'artifact', id: 'source', version: 1 }],
    metrics: [
      { label: '任务通过率', unit: 'ratio', ...pair(before.metrics.taskSuccessCount / experiment.dataset.caseCount, stage.metrics.taskSuccessCount / experiment.dataset.caseCount), sampleCount: experiment.dataset.caseCount },
      { label: '通过业务检查', ...pair(before.metrics.verifierPassCount, stage.metrics.verifierPassCount), sampleCount: 31 },
      { label: '费用估算', unit: 'USD', ...pair(before.costUsd, stage.costUsd) },
    ],
  };
});
const project = {
  schemaVersion: 'rag-ime.agent-lab-project.v1', projectId: 'comparison-enterpriseops', title: '企业客户支持',
  description: '根据客户支持任务，比较模型、工具准备和提示词；质量达标后再权衡成本。',
  revision: 1, briefVersion: 1, materialCount: 0, artifactCount: artifacts.length, guideSessionId: '', createdAtMs: stamp, updatedAtMs: stamp,
  materialSetId: '', materialSet: { materialSetId: '', version: 0, materials: [], createdAtMs: null }, materialVersions: [],
  intake: { state: 'needs_materials', requestedPath: '', resolvedPath: '', readCount: 0, readBytes: 0, skippedCount: 0, partial: false, issues: [], checkedAtMs: null },
  artifacts: artifacts.map(({ content, ...header }) => header), bindings: [], workspaceBinding: null,
  historyOrigin: { sceneId: 'enterpriseops', sourceHash: scene.sourceSha256, experimentCount: 1, importedAtMs: stamp, snapshotArtifactId: 'source', snapshotArtifactRevision: 1 },
  workspace: { artifactOrder: ['overview', 'metrics', 'source'], primaryArtifactId: 'overview', layout: 'focus' },
  workflow: { schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: stamp, nodes, edges: [], counts: { running: 0, queued: 0, completed: 3, failed: 0 }, currentNodeId: null },
  workState: { status: 'history_only', label: '历史结果', reason: '公开历史回执；没有导入当前业务材料。' },
  nextAction: { kind: 'prepare_rerun', label: '准备复跑', reason: '真实运行需要当前材料和执行环境。' },
};
const refuse = () => { throw new Error('README 配图仅展示公开历史元数据，不执行项目或模型命令。'); };
const transport = new MockControlTransport({ routes: {
  'agent.eval-lab.projects.get': (request) => ({ ok: true, project: request.query?.projectId ? project : null, items: [project], supportedViews: ['table', 'json'], ...(request.query?.artifactId ? { artifact: artifacts.find((a) => a.artifactId === request.query?.artifactId) } : {}) }),
  'agent.eval-lab.projects.command': refuse,
  'agent.eval-lab.apps.get': () => ({ ok: true, items: [], app: null, version: null, versions: [], calls: [] }),
  'agent.session.prompt': refuse,
} });
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ControlTransportProvider transport={transport}><div className="readme-label">Lab 改版预览 · 公开历史数据 · 未重新运行模型</div><div className="readme-lab"><LabProjectWorkbench initialProjectId="comparison-enterpriseops" /></div></ControlTransportProvider></QueryClientProvider>);
