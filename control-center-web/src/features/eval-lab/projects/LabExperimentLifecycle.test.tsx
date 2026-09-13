import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LabExperimentLifecycle } from './LabExperimentLifecycle';

afterEach(cleanup);

const project = { projectId: 'p', title: '知识库实验', materialCount: 1, artifactCount: 6, bindings: [{ bindingId: 'b', ownerRef: { kind: 'scene_trial', id: 'x' } }], artifacts: [
  { artifactId: 'overview', title: '实验总览', summary: '题集 enterprise-rag-v1 已审核' }, { artifactId: 'metrics', title: '指标对照', summary: 'Recall 0.4 → 0.8' }, { artifactId: 'changes', title: '逐步实验卡', summary: 'Prompt-v4 修复引用门禁' }, { artifactId: 'snapshot', title: '原始评测记录', summary: 'run-candidate-1 · 4 cases' }, { artifactId: 'failure', title: '失败诊断记录', summary: 'Case-02 引用缺失，触发 citation gate' }, { artifactId: 'best', title: '最优方案总结', summary: 'Prompt-v4 · Keep' },
] } as never;

describe('LabExperimentLifecycle', () => {
  it('recognizes completed Knowledge resources without requiring duplicate text-material intake', () => {
    const onOpenKnowledge = vi.fn();
    render(<LabExperimentLifecycle project={{ ...(project as Record<string, unknown>), materialCount: 0,
      knowledgeResources: { corpusCount: 1, indexCount: 1, datasetCount: 1, documentCount: 209, chunkCount: 20017 } } as never}
      onOpenArtifact={vi.fn()} onOpenRuns={vi.fn()} onDirection={vi.fn()} onContinue={vi.fn()} onOpenKnowledge={onOpenKnowledge} />);
    expect(screen.getByText(/已接入 209 篇知识文档，已有索引 20017 个切片/)).toBeVisible();
    expect(screen.queryByText('待添加材料')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '用示例走通流程' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看知识库' })); expect(onOpenKnowledge).toHaveBeenCalledOnce();
  });
  it('does not turn a completed dataset draft into an experiment or metric decision', () => {
    const draft = { projectId: 'draft', title: '待审核评测集', materialCount: 1,
      artifactCount: 0, artifacts: [], bindings: [{ bindingId: 'golden',
        ownerRef: { kind: 'golden_suite', id: 'suite-draft' }, execution: {
          status: 'completed', label: '评测集准备完成', canContinue: true,
          latestJob: { jobId: 'draft-1', kind: 'draft', state: 'completed' },
        } }],
    } as never;
    render(<LabExperimentLifecycle project={draft} onOpenArtifact={vi.fn()} onOpenRuns={vi.fn()} onDirection={vi.fn()} />);
    expect(screen.getByText('等待评测集')).toBeInTheDocument();
    expect(screen.queryByText(/真实运行回执 ·/)).not.toBeInTheDocument();
    expect(screen.queryByText('本轮判定已记录')).not.toBeInTheDocument();
  });
  it('connects evidence, run records, round directions and the selected result', () => {
    const onOpenArtifact = vi.fn(); const onOpenRuns = vi.fn(); const onDirection = vi.fn();
    render(<LabExperimentLifecycle project={project} onOpenArtifact={onOpenArtifact} onOpenRuns={onOpenRuns} onDirection={onDirection} />);
    expect(screen.getByRole('heading', { name: '实验进展' })).toBeInTheDocument();
    expect(screen.getByText(/Case-02 引用缺失/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /查看原始运行记录/ }));
    expect(onOpenRuns).toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Prompt 优化' }));
    expect(onDirection).toHaveBeenCalledWith('Prompt 优化');
    fireEvent.click(screen.getByRole('button', { name: '查看失败记录' }));
    expect(onOpenArtifact).toHaveBeenCalledWith('failure');
    fireEvent.click(screen.getByRole('button', { name: '打开总结' }));
    expect(onOpenArtifact).toHaveBeenCalledWith('best');
  });
  it('offers real continuation actions without inventing another round', () => {
    const onContinue = vi.fn(); const onAddMaterials = vi.fn();
    render(<LabExperimentLifecycle project={{ ...(project as Record<string, unknown>), materialCount: 0 } as never} onOpenArtifact={vi.fn()} onOpenRuns={vi.fn()} onDirection={vi.fn()} onContinue={onContinue} onAddMaterials={onAddMaterials} />);
    expect(screen.queryByRole('button', { name: '下一轮' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '自动推进优化' }));
    fireEvent.click(screen.getByRole('button', { name: '带我逐步完成' }));
    fireEvent.click(screen.getByRole('button', { name: '用示例走通流程' }));
    expect(onContinue.mock.calls).toEqual([['auto'], ['guided'], ['sample']]);
    fireEvent.click(screen.getByRole('button', { name: '添加材料' }));
    expect(onAddMaterials).toHaveBeenCalledOnce();
  });
  it('exposes the real model execution state before asking for the next optimization round', () => {
    const running = { ...(project as Record<string, unknown>), bindings: [{ bindingId: 'golden', ownerRef: { kind: 'golden_suite', id: 'suite-1' }, execution: {
      status: 'running', label: '模型运行中', reason: '开发题 · 基线 · 第 1 / 4 题', canContinue: false,
      latestJob: { jobId: 'job-1', kind: 'experiment', state: 'running', progress: '开发题 · 基线 · 第 1 / 4 题' },
    } }] } as never;
    const onOpenRuns = vi.fn();
    render(<LabExperimentLifecycle project={running} onOpenArtifact={vi.fn()} onOpenRuns={onOpenRuns} onDirection={vi.fn()} />);
    expect(screen.getByText('模型运行中')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Prompt 优化' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '查看模型运行' }));
    expect(onOpenRuns).toHaveBeenCalled();
  });
  it('projects a completed execution into the lifecycle when presentation artifacts lag behind', () => {
    const completed = { ...(project as Record<string, unknown>), artifacts: [
      { artifactId: 'materials', title: '冻结材料目录', summary: '6 份材料' },
      { artifactId: 'guide', title: '项目进度与讲解', summary: '项目说明' },
    ], bindings: [{ bindingId: 'golden', ownerRef: { kind: 'golden_suite', id: 'suite-1' }, execution: {
      status: 'completed', label: '已完成', reason: '模型运行已完成，当前判定为 no_improvement。', canContinue: true,
      latestJob: { jobId: 'job-1', kind: 'experiment', state: 'completed', progress: '已完成', decision: 'no_improvement' },
    } }] } as never;
    render(<LabExperimentLifecycle project={completed} onOpenArtifact={vi.fn()} onOpenRuns={vi.fn()} onDirection={vi.fn()} />);
    expect(screen.getByText('本轮无提升，沿用基线')).toBeInTheDocument();
    expect(screen.getByText('运行已完成')).toBeInTheDocument();
    expect(screen.getByText('判定已记录')).toBeInTheDocument();
    expect(screen.getByText('沿用基线')).toBeInTheDocument();
    expect(screen.queryByText('等待评测集')).not.toBeInTheDocument();
  });
});
