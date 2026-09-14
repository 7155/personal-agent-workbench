import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LabWorkflowGraph, layoutWorkflow } from './LabWorkflowGraph';
import { isLabProjectWorkflow, type LabProjectWorkflow, type LabWorkflowNode } from './project-workflow-types';

const originalScrollTo = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTo');
afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); if (originalScrollTo) Object.defineProperty(HTMLElement.prototype, 'scrollTo', originalScrollTo); else Reflect.deleteProperty(HTMLElement.prototype, 'scrollTo'); });
const node = (id: string, patch: Partial<LabWorkflowNode> = {}): LabWorkflowNode => ({ id, kind: 'experiment', title: id, status: 'completed', summary: `${id} 的原始记录`, dependencies: [], ref: { kind: 'golden_job', id }, source: 'runtime', ...patch });
const workflow = (nodes: LabWorkflowNode[]): LabProjectWorkflow => ({ schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: 100, nodes, edges: nodes.flatMap((item) => item.dependencies.map((source) => ({ source, target: item.id }))), counts: { running: 2, queued: 1, completed: 1, failed: 1 }, currentNodeId: nodes.find((item) => item.status === 'running')?.id ?? null });
function mount(data?: LabProjectWorkflow, onOpenNode = vi.fn()) {
  return render(graph(data, onOpenNode));
}
const graph = (data?: LabProjectWorkflow, onOpenNode = vi.fn(), projectId = 'project') => <LabWorkflowGraph projectId={projectId} connection="test" workflow={data} onOpenNode={onOpenNode} onOpenMaterials={vi.fn()} onOpenRuns={vi.fn()} onOpenApps={vi.fn()} onOpenExperiments={vi.fn()} />;
function scrollSurface() {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(500);
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(200);
  const scroll = vi.fn(function (this: HTMLElement, options: ScrollToOptions) { this.scrollLeft = options.left ?? this.scrollLeft; this.scrollTop = options.top ?? this.scrollTop; });
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: scroll });
  return { scroll, ancestorScroll: vi.spyOn(HTMLElement.prototype, 'scrollIntoView'), windowScroll: vi.spyOn(window, 'scrollTo') };
}

describe('Lab project dependency canvas', () => {
  it('centers the current node within the scaled canvas without scrolling any ancestor', () => {
    const { scroll, ancestorScroll, windowScroll } = scrollSurface();
    const data = workflow([node('a'), node('b', { dependencies: ['a'] }), node('c', { dependencies: ['b'] }), node('d', { dependencies: ['c'] }), node('validation', { kind: 'job', parentId: 'd', status: 'running' })]);
    mount(data);
    const canvas = screen.getByRole('region', { name: '实验节点画布' });
    expect(scroll).toHaveBeenCalledOnce();
    expect(scroll.mock.contexts[0]).toBe(canvas);
    expect(canvas.scrollLeft).toBeCloseTo((1032 + 135) * 0.85 - 250);
    expect(canvas.scrollTop).toBeCloseTo((246 + 56) * 0.85 - 100);
    scroll.mockClear();
    fireEvent.click(screen.getByRole('button', { name: '放大画布' }));
    fireEvent.click(screen.getByRole('button', { name: '定位当前' }));
    expect(scroll).toHaveBeenCalledOnce();
    expect(scroll.mock.contexts[0]).toBe(canvas);
    expect(canvas.scrollLeft).toBeCloseTo(1032 + 135 - 250);
    expect(ancestorScroll).not.toHaveBeenCalled(); expect(windowScroll).not.toHaveBeenCalled();
  });
  it('reveals the saved selection once after data arrives and preserves user scrolling across polling', () => {
    const { scroll } = scrollSurface();
    localStorage.setItem('paw.lab.workflow-selection.v1:test:project', 'c');
    const view = mount(); expect(scroll).not.toHaveBeenCalled();
    const data = workflow([node('a', { status: 'running' }), node('b', { dependencies: ['a'] }), node('c', { dependencies: ['b'] })]);
    view.rerender(graph(data));
    const canvas = screen.getByRole('region', { name: '实验节点画布' });
    expect(scroll).toHaveBeenCalledOnce();
    expect(canvas.scrollLeft).toBeCloseTo((688 + 135) * 0.85 - 250);
    canvas.scrollLeft = 123; canvas.scrollTop = 456;
    view.rerender(graph({ ...data, observedAtMs: 200, currentNodeId: 'b', nodes: data.nodes.map((item) => ({ ...item })) }));
    expect(scroll).toHaveBeenCalledOnce();
    expect(canvas.scrollLeft).toBe(123); expect(canvas.scrollTop).toBe(456);
    localStorage.setItem('paw.lab.workflow-selection.v1:test:other-project', 'b');
    view.rerender(graph(data, vi.fn(), 'other-project'));
    expect(scroll).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'b · 已完成' })).toHaveAttribute('aria-pressed', 'true');
    expect(canvas.scrollLeft).toBeCloseTo((358 + 135) * 0.85 - 250);
  });
  it('explains an inconclusive decision in both the node and its result detail', () => {
    mount(workflow([node('样本不足', { decision: 'inconclusive' })]));
    expect(screen.getAllByText('结论不足 · 沿用基线')).toHaveLength(2);
    expect(screen.queryByText('inconclusive')).not.toBeInTheDocument();
  });
  it('waits for a hidden canvas to become measurable and centers only once', () => {
    const { scroll } = scrollSurface(); let visible = false; let resize!: () => void;
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => visible ? 500 : 0);
    const disconnect = vi.fn();
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) { resize = () => callback([], this); }
      observe() {} unobserve() {} disconnect = disconnect;
    });
    mount(workflow([node('a'), node('b', { dependencies: ['a'], status: 'running' })]));
    expect(scroll).not.toHaveBeenCalled();
    visible = true; resize();
    expect(scroll).toHaveBeenCalledOnce(); expect(disconnect).toHaveBeenCalledOnce();
    resize(); expect(scroll).toHaveBeenCalledOnce();
  });
  it('shows a single retrieval result and its denominator without inventing baseline or gains', () => {
    const data = workflow([node('原检索', { ref: { kind: 'knowledge_job', id: 'retrieval' }, metrics: [{ label: 'MRR', baseline: null, candidate: null, value: 0, sampleCount: 48 }] })]);
    expect(isLabProjectWorkflow(data)).toBe(true); mount(data);
    expect(screen.getByText('本次结果 · 48 题')).toBeVisible();
    expect(screen.getByText('未指定配对基线')).toBeVisible();
    expect(screen.queryByText('变化 +0')).not.toBeInTheDocument();
    expect(screen.queryByText('候选')).not.toBeInTheDocument();
  });
  it('shows all parallel and terminal task states without starting a completed node again', () => {
    const completed = node('枚举约束实验', { decision: 'no_improvement' });
    const onOpenNode = vi.fn(); mount(workflow([completed, node('引用验证', { kind: 'job', status: 'running', parentId: completed.id }), node('留出集验证', { kind: 'job', status: 'running', parentId: completed.id }), node('排队验证', { status: 'queued' }), node('失败实验', { status: 'failed' })]), onOpenNode);
    expect(screen.getByRole('button', { name: '引用验证 · 运行中' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '留出集验证 · 运行中' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '枚举约束实验 · 已完成' }));
    expect(within(screen.getByRole('region', { name: '所选节点详情' })).getByText('无提升 · 沿用基线')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看结果' }));
    expect(onOpenNode).toHaveBeenCalledWith(completed);
    expect(screen.queryByRole('button', { name: /重新运行|下一轮|Run again/ })).not.toBeInTheDocument();
  });
  it('keeps missing metrics distinct from zero and exposes exact changes', () => {
    mount(workflow([node('模型对照', { metrics: [{ label: '成本', baseline: 0, candidate: null, unit: '美元' }], factors: [{ name: '模型', before: '基线模型', after: '候选模型', reason: '仅测试模型因素' }], reasons: ['质量未达到冻结标准'] })]));
    const detail = within(screen.getByRole('region', { name: '所选节点详情' }));
    expect(detail.getByText('0美元')).toBeInTheDocument(); expect(detail.getByText('未回报')).toBeInTheDocument();
    expect(detail.getByText('暂不能比较')).toBeInTheDocument(); expect(detail.getByText('仅测试模型因素')).toBeInTheDocument();
    expect(detail.getByText('效果尚未判定')).toBeInTheDocument();
  });
  it('opens the exact reused call version with its App owner intact', () => {
    const current = node('追问整理', { kind: 'job', ref: { kind: 'application_call', id: 'followup-call', version: 7 }, evidenceRefs: [{ kind: 'application', id: 'polar-app', version: 7 }, { kind: 'application_call', id: 'original-call', version: 5 }], summary: '复用前次 34 个原文窗口' });
    const onOpenNode = vi.fn(); mount(workflow([current]), onOpenNode);
    fireEvent.click(screen.getByRole('button', { name: '查看复用的原调用' }));
    expect(onOpenNode).toHaveBeenCalledOnce();
    expect(onOpenNode).toHaveBeenCalledWith({ ...current, ref: { kind: 'application_call', id: 'original-call', version: 5 } });
    expect(screen.queryByRole('button', { name: /重新运行|重试/ })).not.toBeInTheDocument();
  });
  it('selects nodes with the keyboard and restores the selected result across remounts', async () => {
    const user = userEvent.setup(); const data = workflow([node('第一轮'), node('第二轮')]); const view = mount(data);
    const second = screen.getByRole('button', { name: '第二轮 · 已完成' }); second.focus(); await user.keyboard('{Enter}');
    expect(second).toHaveAttribute('aria-pressed', 'true'); view.unmount(); mount({ ...data, currentNodeId: '第一轮' });
    expect(screen.getByRole('button', { name: '第二轮 · 已完成' })).toHaveAttribute('aria-pressed', 'true');
  });
  it('lays out project-specific dependencies and parallel siblings without fixed stages', () => {
    const nodes = [node('故障日志', { kind: 'materials' }), node('代码修复', { kind: 'step', dependencies: ['故障日志'] }), node('云资源排查', { kind: 'step', dependencies: ['故障日志'] }), node('修复验收', { dependencies: ['代码修复', '云资源排查'] })];
    const { positions } = layoutWorkflow(nodes);
    expect(positions.get('代码修复')!.x).toBe(positions.get('云资源排查')!.x);
    expect(positions.get('代码修复')!.x).toBeGreaterThan(positions.get('故障日志')!.x);
    expect(positions.get('修复验收')!.x).toBeGreaterThan(positions.get('代码修复')!.x);
    mount(workflow(nodes)); expect(screen.queryByText('评测准备')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '云资源排查 · 已完成' })).toBeInTheDocument();
  });
  it('labels unbound Agent plan activity separately from live runtime jobs', () => {
    mount(workflow([node('人工核对选址', { kind: 'step', status: 'running', source: 'artifact' })]));
    expect(screen.getByRole('button', { name: '人工核对选址 · 计划进行中' })).toBeInTheDocument();
    expect(screen.getByText('Agent 已保存的计划状态 · 不代表后台实时运行')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '人工核对选址 · 运行中' })).not.toBeInTheDocument();
  });
  it('preserves visible unknown state without synthesizing baseline, candidates, or apps', () => {
    mount(); expect(screen.getByText('工作流记录尚未返回')).toBeInTheDocument();
    expect(screen.queryAllByRole('button').filter((button) => button.hasAttribute('aria-pressed'))).toHaveLength(0);
    expect(screen.getByRole('button', { name: '查看运行记录' })).toBeInTheDocument();
  });
  it('validates numeric and identity boundaries and still renders dependency cycles finitely', () => {
    const data = workflow([node('a'), node('b', { dependencies: ['a'] })]);
    expect(isLabProjectWorkflow(data)).toBe(true);
    expect(isLabProjectWorkflow({ ...data, nodes: [...data.nodes, data.nodes[0]] })).toBe(false);
    expect(isLabProjectWorkflow(workflow([node('bad', { metrics: [{ label: '分数', baseline: Infinity, candidate: 0 }] })]))).toBe(false);
    const cycle = [node('a', { dependencies: ['b'] }), node('b', { dependencies: ['a'] })];
    expect(layoutWorkflow(cycle).positions.size).toBe(2);
  });
});
