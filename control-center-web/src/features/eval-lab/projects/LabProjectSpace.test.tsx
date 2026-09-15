import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LabProjectSpace, testedDimensions } from './LabProjectSpace';
import { isProjectDirectory, type LabProject } from './types';
import type { LabWorkflowNode } from './project-workflow-types';

afterEach(cleanup);
const experiment: LabWorkflowNode = { id: 'experiment', kind: 'experiment', title: 'Skill 模型 Tool 全部优化', status: 'completed', summary: '只对照了已保存的提示词', dependencies: [], ref: { kind: 'golden_job', id: 'job' }, source: 'runtime', optimization: { scope: 'prompt' }, decision: 'no_improvement', metrics: [{ label: '通过题数', baseline: 2, candidate: 2 }], factors: [{ name: 'Prompt', before: '直接总结', after: '标出证据', reason: '补充引用' }] };
const project: LabProject = {
  schemaVersion: 'rag-ime.agent-lab-project.v1', projectId: 'p', title: '论文研究', description: '比较两篇论文', revision: 1, briefVersion: 1,
  materialCount: 0, artifactCount: 1, guideSessionId: '', createdAtMs: 1, updatedAtMs: 1, materialSetId: '', materialVersions: [], workspaceBinding: null,
  materialSet: { materialSetId: '', version: 0, materials: [], createdAtMs: null },
  intake: { state: 'needs_materials', requestedPath: '', resolvedPath: '', readCount: 0, readBytes: 0, skippedCount: 0, partial: false, issues: [], checkedAtMs: null },
  bindings: [], workspace: { artifactOrder: ['report'], primaryArtifactId: 'report', layout: 'split' },
  artifacts: [{ artifactId: 'report', title: '论文对照报告', revision: 1, kind: 'report', summary: '', actions: [], templateRef: null, createdAtMs: 1, updatedAtMs: 1, view: 'markdown' }],
  directory: { path: '/project folder', status: 'ready', sourceRevision: 2, generatedAtMs: 1, files: [], warnings: [] },
  workflow: { schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: 1, edges: [], nodes: [experiment, { ...experiment, id: 'child', kind: 'job', parentId: experiment.id, title: '正在读取论文', status: 'running' }], counts: { running: 1, queued: 0, completed: 1, failed: 0 }, currentNodeId: 'child' },
};
function mount() { const openFile = vi.fn(); const openNode = vi.fn(); render(<LabProjectSpace project={project} artifactId="report" artifactContent={<article>已有真实研究结果</article>} onSelectArtifact={vi.fn()} onOpenNode={openNode} onOpenGraph={vi.fn()} onOpenRuns={vi.fn()} onOpenApps={vi.fn()} onOpenChat={vi.fn()} onOpenFile={openFile} />); return { openFile, openNode }; }
describe('Lab result-first project surface', () => {
  it('shows the saved result first and all concurrent work on demand', () => {
    mount(); expect(screen.getByText('已有真实研究结果')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /1 项后台工作正在运行/ }));
    expect(screen.getByRole('region', { name: '全部后台工作' })).toBeVisible(); expect(screen.getByText('正在读取论文')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '改动、结果与来源' }));
    expect(screen.getByRole('region', { name: '所选节点详情' })).toBeVisible();
    expect(screen.getByText('无提升 · 沿用基线')).toBeVisible();
  });
  it('does not infer tested dimensions from an optimistic experiment title', () => {
    expect(testedDimensions(experiment)).toEqual(['Prompt']); mount();
    expect(screen.getByRole('button', { name: 'Skill 未记录测试' })).toBeDisabled();
    const prompt = screen.getByRole('button', { name: 'Prompt 1 次已有对照' });
    expect(prompt).toBeEnabled(); expect(prompt).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(prompt); expect(prompt).toHaveAttribute('aria-pressed', 'true');
  });
  it('opens the durable folder without starting a task', () => {
    const { openFile, openNode } = mount(); fireEvent.click(screen.getByRole('button', { name: '项目文件夹' }));
    expect(openFile).toHaveBeenCalledWith('/project folder'); expect(openNode).not.toHaveBeenCalled();
    expect(isProjectDirectory(project.directory)).toBe(true); expect(isProjectDirectory({ ...project.directory, files: [{ path: 1 }] })).toBe(false);
  });
});
