import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import type { SessionSummary } from '@/features/agent/types';
import { ProjectWorkspacePicker, type ProjectWorkspacePickerProps } from './ProjectWorkspacePicker';

afterEach(cleanup);

const agent = (id: string, path: string, title = id, updatedAtMs = 1) => ({ id, title, mode: 'coordinator', updatedAtMs, workspaceRoots: [path] } as SessionSummary);
const first = agent('agent-1', '/gis/shared', '地块分析', 3);
const second = agent('agent-2', '/gis/shared/', '影像分析', 2);
const other = agent('agent-3', '/archive/shared', '另一个项目', 1);

function show(overrides: Partial<ProjectWorkspacePickerProps> = {}) {
  const onSelectSession = vi.fn();
  const onCreateSession = vi.fn().mockResolvedValue(undefined);
  const props = { sessions: [first, second, other], session: first, onSelectSession, onCreateSession, ...overrides };
  function Harness() {
    const [open, setOpen] = useState(true);
    const [draftRoot, setDraftRoot] = useState(overrides.draftRoot ?? '/gis/shared');
    return <ProjectWorkspacePicker {...props} draftRoot={draftRoot} open={open} onOpenChange={setOpen} onDraftRootChange={setDraftRoot} />;
  }
  render(<Harness />);
  return { onSelectSession, onCreateSession };
}

it('groups Agents by directory and keeps the active binding visible until an explicit entry', async () => {
  const { onSelectSession, onCreateSession } = show();
  const projects = screen.getByRole('combobox', { name: '已有 GIS 项目' });
  expect(within(projects).getAllByRole('option')).toHaveLength(3);
  expect(within(projects).getByRole('option', { name: 'shared · 2 个 Agent · /gis/shared' })).toBeInTheDocument();
  expect(within(screen.getByRole('combobox', { name: '项目 Agent' })).getAllByRole('option')).toHaveLength(2);
  await userEvent.selectOptions(projects, '/archive/shared');
  expect(screen.getByLabelText('当前项目文件夹')).toHaveTextContent('/gis/shared');
  expect(screen.getByText('待打开的项目；当前 Agent 仍在原文件夹工作。')).toBeVisible();
  expect(within(screen.getByRole('combobox', { name: '项目 Agent' })).getAllByRole('option')).toHaveLength(1);
  expect(onSelectSession).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '进入所选 Agent' }));
  expect(onSelectSession).toHaveBeenCalledWith(other);
  expect(onCreateSession).not.toHaveBeenCalled();
});

it('enters another Agent in the same project without creating or moving either Session', async () => {
  const { onSelectSession, onCreateSession } = show();
  await userEvent.selectOptions(screen.getByRole('combobox', { name: '项目 Agent' }), second.id);
  expect(onSelectSession).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '进入所选 Agent' }));
  expect(onSelectSession).toHaveBeenCalledWith(second);
  expect(onCreateSession).not.toHaveBeenCalled();
});

it('creates an additional Agent only after the explicit same-project action', async () => {
  const { onSelectSession, onCreateSession } = show({ draftRoot: '/gis/shared/' });
  await userEvent.click(screen.getByRole('button', { name: '新建同项目 Agent' }));
  await waitFor(() => expect(onCreateSession).toHaveBeenCalledWith('/gis/shared'));
  expect(onCreateSession).toHaveBeenCalledTimes(1);
  expect(onSelectSession).not.toHaveBeenCalled();
  expect(screen.queryByRole('region', { name: '项目与 Agent 选择' })).not.toBeInTheDocument();
});

it('keeps folder browsing and cancellation separate from opening or creating a Session', async () => {
  const onPickDirectory = vi.fn().mockResolvedValueOnce(null).mockResolvedValueOnce('/gis/new-project');
  const { onSelectSession, onCreateSession } = show({ onPickDirectory });
  await userEvent.click(screen.getByRole('button', { name: '浏览…' }));
  expect(screen.getByRole('textbox', { name: '项目文件夹' })).toHaveValue('/gis/shared');
  await userEvent.click(screen.getByRole('button', { name: '浏览…' }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: '项目文件夹' })).toHaveValue('/gis/new-project'));
  expect(screen.getByLabelText('当前项目文件夹')).toHaveTextContent('/gis/shared');
  expect(screen.getByRole('button', { name: '新建项目 Agent' })).toBeEnabled();
  expect(onSelectSession).not.toHaveBeenCalled();
  expect(onCreateSession).not.toHaveBeenCalled();
});

it('keeps the selected directory and original Agent when creation fails, then permits retry', async () => {
  const onCreateSession = vi.fn().mockRejectedValueOnce(new Error('目录不可读取')).mockResolvedValueOnce(undefined);
  show({ draftRoot: '/gis/new-project', onCreateSession });
  await userEvent.click(screen.getByRole('button', { name: '新建项目 Agent' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('目录不可读取');
  expect(screen.getByRole('textbox', { name: '项目文件夹' })).toHaveValue('/gis/new-project');
  expect(screen.getByLabelText('当前项目文件夹')).toHaveTextContent('/gis/shared');
  await userEvent.click(screen.getByRole('button', { name: '新建项目 Agent' }));
  await waitFor(() => expect(onCreateSession).toHaveBeenCalledTimes(2));
});

it('keeps manual paths available without a native picker and returns focus on Escape', async () => {
  show({ draftRoot: 'relative/path' });
  expect(screen.queryByRole('button', { name: '浏览…' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '新建项目 Agent' })).toBeDisabled();
  const input = screen.getByRole('textbox', { name: '项目文件夹' });
  await userEvent.clear(input); await userEvent.type(input, '/gis/manual');
  expect(screen.getByRole('button', { name: '新建项目 Agent' })).toBeEnabled();
  await userEvent.keyboard('{Escape}');
  expect(screen.getByRole('button', { name: '选择 GIS 项目与 Agent' })).toHaveFocus();
  expect(screen.queryByRole('region', { name: '项目与 Agent 选择' })).not.toBeInTheDocument();
});
