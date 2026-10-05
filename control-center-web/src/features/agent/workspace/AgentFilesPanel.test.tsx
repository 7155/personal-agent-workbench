import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { AgentFilesPanel } from './AgentFilesPanel';

afterEach(cleanup);

describe('AgentFilesPanel', () => {
  it.each([{ roots: [] }, { roots: ['/workspace/paw'] }])('explains a fixed workspace without offering a rejected directory change ($roots)', async ({ roots }) => {
    const onManageRoots = vi.fn();
    const reason = '工作区已在开始时确定。要使用其他目录，请返回入口新建工作。';
    const transport = new MockControlTransport({ routes: { 'agent.session.workspace.list': workspaceListing('/workspace/paw', []) } });
    render(<ControlTransportProvider transport={transport}><TooltipProvider><AgentFilesPanel sessionId="session-files"
      workspaceRoots={roots} open onClose={vi.fn()} onManageRoots={onManageRoots} rootsLockedReason={reason} /></TooltipProvider></ControlTransportProvider>);
    const manage = screen.getByRole('button', { name: '管理工作区目录' });
    expect(manage).toBeDisabled();
    expect(manage).toHaveAccessibleDescription(reason);
    expect(screen.getByText(reason)).toBeVisible();
    await userEvent.setup().click(manage);
    expect(onManageRoots).not.toHaveBeenCalled();
    if (!roots.length) expect(screen.getByRole('button', { name: '选择目录' })).toBeDisabled();
  });

  it('uses a roving treeitem focus model with levels and left/right navigation', async () => {
    const root = '/workspace/paw';
    const transport = new MockControlTransport({
      routes: {
        'agent.session.workspace.list': (request: ControlRequest) => {
          const path = String(request.query?.path ?? '');
          return workspaceListing(path, path === root
            ? [{ path: `${root}/docs`, name: 'docs', kind: 'directory' }]
            : [{ path: `${root}/docs/guide.md`, name: 'guide.md', kind: 'file', byteSize: 12 }]);
        },
      },
    });
    const user = userEvent.setup();

    renderPanel(transport, [root]);

    const tree = await screen.findByRole('tree');
    expect(tree.closest('nav')).toHaveAttribute('aria-label', '工作区文件');
    const rootItem = tree.querySelector<HTMLButtonElement>('button[aria-label="收起工作区 paw"]');
    expect(rootItem).not.toBeNull();
    if (!rootItem) return;
    expect(rootItem).toHaveAttribute('role', 'treeitem');
    expect(rootItem).toHaveAttribute('aria-level', '1');
    expect(rootItem).toHaveAttribute('tabindex', '0');

    rootItem.focus();
    await user.keyboard('{ArrowRight}');
    const docs = within(tree).getByRole('treeitem', { name: '展开目录 docs' });
    expect(docs).toHaveFocus();
    expect(docs).toHaveAttribute('aria-level', '2');

    await user.keyboard('{ArrowRight}');
    expect(docs).toHaveFocus();
    expect(docs).toHaveAttribute('aria-expanded', 'true');
    const guide = await within(tree).findByRole('treeitem', { name: '预览文件 guide.md' });
    expect(guide).toHaveAttribute('role', 'treeitem');
    expect(guide).toHaveAttribute('aria-level', '3');

    await user.keyboard('{ArrowLeft}');
    expect(docs).toHaveFocus();
    expect(docs).toHaveAttribute('aria-expanded', 'false');
    await user.keyboard('{ArrowLeft}');
    expect(rootItem).toHaveFocus();
  });

  it('aborts an in-flight refresh and ignores the stale response', async () => {
    const root = '/workspace/paw';
    const pending: Array<{ resolve: (value: unknown) => void }> = [];
    const transport = new MockControlTransport({
      routes: {
        'agent.session.workspace.list': () => new Promise((resolve) => {
          pending.push({ resolve });
        }),
      },
    });
    const user = userEvent.setup();

    renderPanel(transport, [root]);
    await waitFor(() => expect(transport.requests).toHaveLength(1));

    await user.click(screen.getByRole('button', { name: '刷新文件目录' }));
    await waitFor(() => expect(transport.requests).toHaveLength(2));
    expect(transport.requests[0]?.request.signal?.aborted).toBe(true);

    pending[1]!.resolve(workspaceListing(root, [
      { path: `${root}/fresh.md`, name: 'fresh.md', kind: 'file' },
    ]));
    expect(await screen.findByRole('treeitem', { name: '预览文件 fresh.md' })).toBeVisible();

    pending[0]!.resolve(workspaceListing(root, [
      { path: `${root}/stale.md`, name: 'stale.md', kind: 'file' },
    ]));
    await waitFor(() => expect(screen.queryByRole('treeitem', { name: '预览文件 stale.md' })).not.toBeInTheDocument());
  });
});

function renderPanel(transport: MockControlTransport, workspaceRoots: string[]): void {
  render(
    <ControlTransportProvider transport={transport}>
      <TooltipProvider>
        <AgentFilesPanel
          onClose={() => undefined}
          onManageRoots={() => undefined}
          open
          sessionId="session-files"
          workspaceRoots={workspaceRoots}
        />
      </TooltipProvider>
    </ControlTransportProvider>,
  );
}

function workspaceListing(path: string, items: Array<Record<string, unknown>>): Record<string, unknown> {
  return {
    schemaVersion: 'rag-ime.agent-workspace-list.v1',
    ok: true,
    sessionId: 'session-files',
    root: '/workspace/paw',
    path,
    items,
    truncated: false,
  };
}
