import type { ControlRequest } from '@/platform/transport';
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
import scene from './scene.json';
import './preview.css';

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
const project = {
  schemaVersion: 'rag-ime.agent-lab-project.v1', projectId: 'readme-enterpriseops', title: '企业客户支持',
  description: '根据客户支持任务，比较模型、工具准备和提示词；质量达标后再权衡成本。',
  revision: 1, briefVersion: 1, materialCount: 0, artifactCount: artifacts.length, guideSessionId: '', createdAtMs: stamp, updatedAtMs: stamp,
  materialSetId: '', materialSet: { materialSetId: '', version: 0, materials: [], createdAtMs: null }, materialVersions: [],
  intake: { state: 'needs_materials', requestedPath: '', resolvedPath: '', readCount: 0, readBytes: 0, skippedCount: 0, partial: false, issues: [], checkedAtMs: null },
  artifacts: artifacts.map(({ content, ...header }) => header), bindings: [], workspaceBinding: null,
  historyOrigin: { sceneId: 'enterpriseops', sourceHash: scene.sourceSha256, experimentCount: 1, importedAtMs: stamp, snapshotArtifactId: 'source', snapshotArtifactRevision: 1 },
  workspace: { artifactOrder: ['overview', 'metrics', 'source'], primaryArtifactId: 'overview', layout: 'focus' },
  workState: { status: 'history_only', label: '历史结果', reason: '公开历史回执；没有导入当前业务材料。' },
  nextAction: { kind: 'prepare_rerun', label: '准备复跑', reason: '真实运行需要当前材料和执行环境。' },
};
const refuse = () => { throw new Error('README 配图仅展示公开历史元数据，不执行项目或模型命令。'); };
const transport = new MockControlTransport({ routes: {
  'agent.eval-lab.projects.get': (request: ControlRequest) => ({ ok: true, project: request.query?.projectId ? project : null, items: [project], supportedViews: ['table', 'json'], ...(request.query?.artifactId ? { artifact: artifacts.find((a) => a.artifactId === request.query?.artifactId) } : {}) }),
  'agent.eval-lab.projects.command': refuse,
  'agent.eval-lab.apps.get': () => ({ ok: true, items: [], app: null, version: null, versions: [], calls: [] }),
  'agent.session.prompt': refuse,
} });
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ControlTransportProvider transport={transport}><div className="readme-label">公开历史回执 · 界面展示 · 未重新运行模型</div><div className="readme-lab"><LabProjectWorkbench initialProjectId="readme-enterpriseops" /></div></ControlTransportProvider></QueryClientProvider>);
