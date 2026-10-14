import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ArtifactSurface } from './ArtifactSurface';
import { ExperimentReport } from './ExperimentReport';
import { LabProjectHome } from './LabProjectHome';
import type { JsonValue, LabArtifact, LabProjectSummary } from './types';

afterEach(cleanup);
const data: JsonValue = { schemaVersion: 'paw.lab-imported-experiments.v1', executionPerformed: false, experiments: [
  { experimentId: 'old', title: '过去的诊断', projectionState: 'history', comparison: { decision: 'diagnostic_only' } },
  { experimentId: 'current', title: '已记录的三阶段比较', projectionState: 'current', factors: [{ name: 'model', before: 'Model A', after: 'Model B', reason: '降低成本' }], dataset: { caseCount: 4, split: 'validation', heldOutConsumed: false }, comparison: { decision: 'keep', decisionReason: '通过冻结标准', stageChain: [
    { stage: 'baseline', agentSuccessRate: .5, costUsd: 2, exactCitationFactsCovered: 7, citationFactCount: 9, decision: 'reject' },
    { stage: 'model_only', agentSuccessRate: .5, costUsd: .12, exactCitationFactsCovered: 8, citationFactCount: 9, decision: 'reject' },
    { stage: 'prompt', agentSuccessRate: 1, costUsd: .1, exactCitationFactsCovered: 9, citationFactCount: 9, decision: 'keep' },
  ], validationBoundary: { candidateAware: true, providerBillAvailable: false } } },
] };
const report: LabArtifact = { artifactId: 'changes', title: '改动与结论', kind: 'experiment_history', view: 'markdown', content: '# 完整说明\n成本节省 999%，这一句不参与图表。', revision: 1, summary: '', actions: [], templateRef: null, createdAtMs: 1, updatedAtMs: 1 };

describe('engineering Lab report', () => {
  it.each(['overview', 'metrics'] as const)('renders recorded task counts using the frozen case denominator in %s', (mode) => {
    render(<ExperimentReport mode={mode} content={{ schemaVersion: 'paw.lab-imported-experiments.v1', experiments: [{ experimentId: 'support', dataset: { caseCount: 3 }, comparison: { stageChain: [
      { stage: 'sol_baseline', taskSuccessCount: 3, decision: 'baseline' },
      { stage: 'luna_model_only', taskSuccessCount: 2, decision: 'reject' },
      { stage: 'luna_prompt_adapted', taskSuccessRate: 1, decision: 'keep' },
    ] } }] }} />);
    const quality = screen.getByRole('group', { name: '各阶段质量' });
    expect(within(quality).getAllByText('100%')).toHaveLength(2);
    expect(within(quality).getByText('66.67%')).toBeInTheDocument();
    expect(within(quality).getByText('Luna · 优化提示词')).toBeInTheDocument();
  });
  it('does not derive a rate from missing, zero or invalid denominators', () => {
    render(<ExperimentReport mode="overview" content={{ schemaVersion: 'paw.lab-imported-experiments.v1', experiments: [{ experimentId: 'unknown', comparison: { stageChain: [
      { taskSuccessCount: 2 }, { taskSuccessCount: 0, taskCount: 0 }, { taskSuccessCount: 4, taskCount: 3 }, { taskSuccessCount: '2', taskCount: 3 }, { taskSuccessCount: 0, taskCount: 3 },
    ] } }] }} />);
    const quality = screen.getByRole('group', { name: '各阶段质量' });
    expect(quality.querySelectorAll('strong')).toHaveLength(5);
    expect([...quality.querySelectorAll('strong')].map((node) => node.textContent)).toEqual(['未记录', '未记录', '未记录', '未记录', '0%']);
  });
  it('copies exact evidence references and translates recorded comparison decisions', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<ExperimentReport mode="records" content={{ schemaVersion: 'paw.lab-imported-experiments.v1', experiments: [{ experimentId: 'run', comparison: { decision: 'no_improvement' }, baseline: { evidenceRefs: ['eval/runs/exact.json'] } }] }} />);
    expect(screen.getAllByText('未观察到提升').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '复制来源 eval/runs/exact.json' }));
    expect(await screen.findByRole('status')).toHaveTextContent('已复制来源路径');
    expect(writeText).toHaveBeenCalledWith('eval/runs/exact.json');
  });
  it('defaults to the current receipt and draws the explicit intermediate stage', () => {
    render(<ExperimentReport content={data} mode="changes" />);
    expect(screen.getByRole('combobox', { name: '实验记录' })).toHaveValue('current');
    expect(screen.getByRole('region', { name: '已记录的优化过程' }).querySelectorAll('li')).toHaveLength(3);
    expect(screen.getByText('证据覆盖 8 / 9')).toBeInTheDocument();
    expect(screen.getByText('$0.12')).toBeInTheDocument();
    expect(screen.getByText('Model A')).toBeInTheDocument();
    expect(screen.getByText(/看过候选后校准.*未做留出验证/)).toBeInTheDocument();
    expect(screen.getByText(/非实际账单/)).toBeInTheDocument();
  });
  it('visualizes a Markdown report from the bound snapshot without parsing prose, and preserves editing', () => {
    render(<ArtifactSurface artifact={report} reportSnapshot={data} busy={false} onDraft={vi.fn()} onSave={async () => true} onAction={vi.fn()} />);
    expect(screen.getByRole('heading', { name: '从改动到结论' })).toBeInTheDocument();
    const source = screen.getByText('查看完整说明文档').closest('details');
    expect(source).not.toHaveAttribute('open');
    expect(source).toHaveTextContent('999%');
    expect(screen.getByRole('group', { name: '各阶段成本' })).not.toHaveTextContent('999');
    fireEvent.click(screen.getByRole('button', { name: '编辑内容' }));
    expect(screen.getByRole('textbox', { name: '成果内容草稿' })).toHaveValue(String(report.content));
  });
  it('keeps the chosen experiment across report modes and never invents missing stages or values', () => {
    const onSelect = vi.fn();
    const view = render(<ExperimentReport content={data} mode="overview" onSelect={onSelect} />);
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'old' } });
    expect(onSelect).toHaveBeenCalledWith('old');
    view.rerender(<ExperimentReport content={data} mode="metrics" selectedId="old" onSelect={onSelect} />);
    expect(screen.getByRole('combobox')).toHaveValue('old');
    expect(screen.queryByRole('group', { name: '各阶段成本' })).not.toBeInTheDocument();
    expect(screen.getByText(/没有可绘制的数值指标/)).toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
  });
  it('keeps unknown documents and unsaved edits as authored text', () => {
    const { rerender } = render(<ArtifactSurface artifact={report} busy={false} onDraft={vi.fn()} onSave={async () => true} onAction={vi.fn()} />);
    expect(screen.getByRole('heading', { name: '完整说明' })).toBeInTheDocument();
    rerender(<ArtifactSurface artifact={report} reportSnapshot={data} draft={{ revision: 1, content: '# 本地新结论' }} busy={false} onDraft={vi.fn()} onSave={async () => true} onAction={vi.fn()} />);
    expect(screen.getByRole('heading', { name: '本地新结论' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '从改动到结论' })).not.toBeInTheDocument();
  });
  it('filters real project states and routes each next action to its working page', () => {
    const onOpen = vi.fn();
    const common = { revision: 1, briefVersion: 1, createdAtMs: 1, updatedAtMs: 1, materialCount: 0, artifactCount: 0, guideSessionId: '' };
    const projects: LabProjectSummary[] = [
      { ...common, projectId: 'draft', title: '地理调研' },
      { ...common, projectId: 'history', title: '企业知识库', artifactCount: 4, historyOrigin: { sceneId: 'rag', sourceHash: 'hash', experimentCount: 10, importedAtMs: 1, snapshotArtifactId: 's', snapshotArtifactRevision: 1 } },
    ];
    render(<LabProjectHome items={projects} onOpen={onOpen} onCreate={vi.fn()} />);
    fireEvent.change(screen.getByRole('textbox', { name: '搜索 Lab 项目' }), { target: { value: '地理' } });
    expect(screen.queryByRole('button', { name: /企业知识库，/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '添加材料' }));
    expect(onOpen).toHaveBeenCalledWith('draft', 'materials');
    fireEvent.change(screen.getByRole('textbox', { name: '搜索 Lab 项目' }), { target: { value: '不存在' } });
    expect(screen.getByRole('heading', { name: '没有匹配的项目' })).toBeInTheDocument();
    const clearFilters = screen.getByRole('button', { name: '清除筛选' });
    clearFilters.focus();
    expect(clearFilters).toHaveFocus();
    fireEvent.click(clearFilters);
    expect(screen.getByRole('textbox', { name: '搜索 Lab 项目' })).toHaveValue('');
    expect(screen.getByRole('textbox', { name: '搜索 Lab 项目' })).toHaveFocus();
    expect(within(screen.getByRole('region', { name: '最近的优化项目' })).getByRole('button', { name: /企业知识库，历史结果/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /历史实验快照/ }));
    expect(onOpen).toHaveBeenCalledWith('history', 'artifact', 's');
  });
});
