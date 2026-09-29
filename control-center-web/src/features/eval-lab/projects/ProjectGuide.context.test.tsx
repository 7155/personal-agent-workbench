import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { WorkspaceComposerContext } from '@/paw-os/apps/workspace-draft';
import { ProjectGuide } from './ProjectGuide';
import type { LabProject } from './types';

vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({ data: { id: 'guide' } }) }));
vi.mock('@/app/control-transport', () => ({ useControlTransport: () => ({}) }));
vi.mock('./LabGuideWorkflow', () => ({ LabGuideWorkflow: () => null }));
vi.mock('@/paw-os/apps/PawSessionWorkspace', () => ({
  sessionWorkspaceProjectionSlice: vi.fn(),
  PawSessionWorkspace: ({ composerContext }: { composerContext?: WorkspaceComposerContext }) => composerContext
    ? <button onClick={composerContext.onClear}>移除 {composerContext.label}</button>
    : <p>没有附带上下文</p>,
}));

describe('ProjectGuide context dismissal', () => {
  it('keeps a dismissed artifact removed across progress updates and attaches a newly selected revision', () => {
    const project = { projectId: 'project', guideSessionId: 'guide' } as LabProject;
    const context = { contextId: 'artifact-a:v1', kind: 'project' as const, label: '原报告', detail: 'v1', text: 'running: 1' };
    const props = { project, viewContext: context, onNewProject: vi.fn(), onProjectActivity: vi.fn(), onEnsure: vi.fn() };
    const view = render(<ProjectGuide {...props} />);
    fireEvent.click(screen.getByRole('button', { name: '移除 原报告' }));
    view.rerender(<ProjectGuide {...props} viewContext={{ ...context, text: 'running: 0; completed: 1' }} />);
    expect(screen.getByText('没有附带上下文')).toBeVisible();
    view.rerender(<ProjectGuide {...props} viewContext={{ ...context, contextId: 'artifact-a:v2', detail: 'v2' }} />);
    expect(screen.getByRole('button', { name: '移除 原报告' })).toBeVisible();
  });
});
