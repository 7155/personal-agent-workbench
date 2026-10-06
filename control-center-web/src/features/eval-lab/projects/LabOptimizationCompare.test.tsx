import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LabOptimizationCompare } from './LabOptimizationCompare';
import { optimizationDraft, testedDimensions } from './optimization-parameters';
import type { LabWorkflowNode } from './project-workflow-types';

afterEach(cleanup);
const run: LabWorkflowNode = { id: 'one', title: '便宜但质量下降', kind: 'experiment', status: 'completed', summary: '降低模型费用', source: 'runtime', dependencies: [], ref: { kind: 'golden_job', id: 'job-1' }, decision: 'reject', factors: [{ name: 'model', before: '原模型', after: '候选模型', reason: '降低成本' }], metrics: [{ label: '开发通过率', unit: 'ratio', baseline: 1, candidate: .5 }, { label: '费用估算', unit: 'USD', baseline: 2, candidate: .1 }] };
function mount(nodes = [run], selected?: LabWorkflowNode) { const onSelect = vi.fn(); const onDraft = vi.fn(); const onOpenNode = vi.fn(); const onOpenKnowledge = vi.fn(); render(<LabOptimizationCompare nodes={nodes} selected={selected} onSelect={onSelect} onDraft={onDraft} onOpenNode={onOpenNode} onOpenKnowledge={onOpenKnowledge} />); return { onSelect, onDraft, onOpenNode, onOpenKnowledge }; }
describe('optimization objectives, comparison and parameters', () => {
  it('links sliders, persists weights and carries exact preferences without changing historical decisions', () => {
    const onDraft = vi.fn();
    const props = { nodes: [run], onSelect: vi.fn(), onOpenNode: vi.fn(), onDraft, preferenceKey: 'weight-test' };
    const view = render(<LabOptimizationCompare {...props} />);
    fireEvent.click(screen.getByText('设置本轮目标与约束'));
    fireEvent.change(screen.getByLabelText('本轮目标'), { target: { value: '我的质量门槛' } });
    fireEvent.change(screen.getByRole('slider', { name: '成本权重' }), { target: { value: '60' } });
    expect(screen.getByRole('slider', { name: '效果权重' })).toHaveValue('33');
    expect(screen.getByRole('slider', { name: '速度权重' })).toHaveValue('7');
    expect(screen.getByLabelText('成本优先的取舍')).toHaveTextContent('我的质量门槛');
    expect(screen.getByRole('region', { name: '优化轮次对照表' })).toHaveTextContent('不保留候选');
    view.unmount(); render(<LabOptimizationCompare {...props} />);
    expect(screen.getByRole('slider', { name: '成本权重' })).toHaveValue('60');
    fireEvent.click(screen.getByRole('button', { name: '选择优化参数' }));
    fireEvent.change(screen.getByLabelText('下一轮想改什么'), { target: { value: '比较模型' } });
    fireEvent.click(screen.getByRole('button', { name: '带入 Agent 草稿' }));
    expect(onDraft).toHaveBeenCalledWith(expect.stringContaining('效果 33% / 降低成本 60% / 提升速度 7%'));
    expect(onDraft).toHaveBeenCalledWith(expect.stringContaining('我的质量门槛'));
    sessionStorage.removeItem('paw.lab.objective.v1:weight-test');
  });

  it('filters from the decision visualization and preserves search and parameter drafts across views', () => {
    mount([run, { ...run, id: 'two', title: '质量保持', decision: 'keep' }]);
    const summary = screen.getByRole('region', { name: '实验结论分布' });
    fireEvent.click(within(summary).getByRole('button', { name: '未保留 1' }));
    expect(screen.queryByRole('button', { name: '质量保持' })).not.toBeInTheDocument();
    const table = screen.getByRole('region', { name: '优化轮次对照表' });
    expect(table).toHaveTextContent('−50 个百分点'); expect(table).toHaveTextContent('−95%');
    fireEvent.change(screen.getByLabelText('查找实验'), { target: { value: '便宜' } });
    fireEvent.click(screen.getByRole('button', { name: '选择优化参数' }));
    expect(screen.queryByRole('region', { name: '优化轮次对照表' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('下一轮想改什么'), { target: { value: '保留我的草稿' } });
    fireEvent.click(screen.getByRole('button', { name: '返回结果对比' }));
    expect(screen.getByLabelText('查找实验')).toHaveValue('便宜');
    expect(within(screen.getByRole('region', { name: '实验结论分布' })).getByRole('button', { name: '未保留 1' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: '选择优化参数' }));
    expect(screen.getByLabelText('下一轮想改什么')).toHaveValue('保留我的草稿');
  });
  it('shows the user-authored goal in the summary instead of stale default copy', () => {
    mount(); fireEvent.click(screen.getByText('设置本轮目标与约束'));
    fireEvent.change(screen.getByLabelText('本轮目标'), { target: { value: '单次任务预算降到 0.1 美元' } });
    expect(screen.getByLabelText('效果优先的取舍')).toHaveTextContent('单次任务预算降到 0.1 美元');
  });
  it('keeps preference separate from methods and never makes a cheap rejected run green', () => {
    mount(); fireEvent.click(screen.getByRole('button', { name: /成本优先/ }));
    expect(screen.getByRole('button', { name: /成本优先/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('保持基线质量与必过检查，便宜但不达标的候选不保留')).toBeVisible();
    const table = screen.getByRole('region', { name: '优化轮次对照表' });
    expect(within(table).getByText('不保留候选')).toBeVisible();
    expect(within(table).getByText('50%')).toBeVisible();
    expect(within(table).queryByText('保留候选')).not.toBeInTheDocument();
  });
  it('reads structured factors, not titles; does not confuse embedding model and generation model', () => {
    expect(testedDimensions({ ...run, title: 'Skill Tool 全部优化', factors: [{ name: 'embedding_model', before: 'A', after: 'B', reason: '' }] })).toEqual(['Embedding']);
    expect(testedDimensions({ ...run, factors: [], optimization: { scope: 'chunking' } })).toEqual(['切片']);
    expect(testedDimensions({ ...run, factors: [], optimization: { scope: 'prompt' } })).toEqual(['Prompt']);
    const retrieval = { ...run, ref: { kind: 'knowledge_job', id: 'retrieval' }, factors: [{ name: 'rerank', before: '未记录', after: 'false', reason: '' }] };
    expect(testedDimensions(retrieval)).toEqual(['RAG']);
    expect(testedDimensions({ ...retrieval, factors: [{ name: 'rerank', before: 'true', after: 'false', reason: '' }] })).toEqual(['RAG', '重排']);
  });
  it('shows measured-but-undecided results without claiming an optimization gain', () => {
    const undecided = { ...run, decision: 'unknown', metrics: [{ label: 'MRR', unit: 'ratio', baseline: null, candidate: .4545 }] };
    mount([undecided]);
    const table = screen.getByRole('region', { name: '优化轮次对照表' });
    expect(table).toHaveTextContent('45.45%');
    expect(table).toHaveTextContent('已测量 · 待结论');
    expect(table).not.toHaveTextContent('保留候选');
  });
  it('keeps unmatched measurements single and never fabricates a pair or a tradeoff plot', () => {
    const single = { ...run, metrics: [{ label: 'MRR', baseline: null, candidate: null, value: .7 }] };
    mount([single], single);
    expect(screen.getByRole('region', { name: '本轮结果' })).toHaveTextContent('单次结果 · 未配对');
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByText(/取舍图需要本轮配对/)).toBeVisible();
  });
  it('shows real before/after and routes exact job to original results', () => {
    const { onOpenNode } = mount([run], run);
    const changes = screen.getByRole('region', { name: '改动前后' });
    expect(changes).toHaveTextContent('原模型'); expect(changes).toHaveTextContent('候选模型');
    expect(screen.getByRole('img', { name: /基线 100%.*候选 50%/ })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '查看逐题结果与原始运行' })); expect(onOpenNode).toHaveBeenCalledWith(run);
    fireEvent.click(screen.getByRole('button', { name: /速度优先/ }));
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });
  it('carries cost objective and constraints into an unsent embedding draft', () => {
    const { onDraft, onOpenKnowledge } = mount();
    fireEvent.click(screen.getByRole('button', { name: /成本优先/ }));
    fireEvent.click(screen.getByText('设置本轮目标与约束'));
    fireEvent.change(screen.getByLabelText('必须保持'), { target: { value: '任务通过 3/3，引用不能减少' } });
    fireEvent.click(screen.getByRole('button', { name: '选择优化参数' }));
    const guide = screen.getByRole('region', { name: '优化参数指南' });
    expect(guide).toHaveTextContent('先在知识库设置配置，再建新索引');
    expect(within(guide).queryByText('维度')).not.toBeInTheDocument();
    fireEvent.click(within(guide).getByLabelText('显示按需高级项'));
    expect(guide).toHaveTextContent('维度');
    fireEvent.change(within(guide).getByLabelText('下一轮想改什么'), { target: { value: '比较一个更小的模型' } });
    fireEvent.click(within(guide).getByRole('button', { name: '带入 Agent 草稿' }));
    expect(onDraft).toHaveBeenCalledOnce(); const text = onDraft.mock.calls[0][0];
    expect(text).toContain('优化倾向：成本优先'); expect(text).toContain('任务通过 3/3，引用不能减少');
    expect(text).toContain('实现手段：Embedding 模型'); expect(text).toContain('不启动模型调用或索引构建');
    fireEvent.click(within(guide).getByRole('button', { name: '打开知识库实验' })); expect(onOpenKnowledge).toHaveBeenCalledOnce();
  });
  it('retains zero metrics and does not label queued work as kept', () => {
    mount([{ ...run, status: 'queued', decision: 'keep', metrics: [{ label: '费用', baseline: 0, candidate: null, unit: 'USD' }] }]);
    expect(screen.getByText('$0')).toBeVisible();
    expect(screen.queryByText('保留候选')).not.toBeInTheDocument();
    expect(screen.getAllByText('未测量').length).toBeGreaterThan(0);
  });
  it('binds a candidate to the selected exact run and carries quality safeguards', () => {
    expect(optimizationDraft('Prompt', '补充引用', run)).toContain('golden_job:job-1');
    expect(optimizationDraft('Prompt', '补充引用', run)).toContain('优化倾向：效果优先');
  });
});
