import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamApi, TeamApiError } from './team-api';
import { TeamProvider, useTeam } from './team-context';
import { TeamSharedResources } from './TeamSharedResources';
import { PAW_EXTENSION_INSTALLATION_CHANGED_EVENT } from '@/paw-os/extensions/installation';
import type {
  TeamPublishedResource,
  TeamResourceCatalogEntry,
  TeamResourceSelection,
  TeamSession,
  TeamSpace,
} from './types';

const projectA: TeamSpace = { id: 'project-a', kind: 'project', name: '官网', role: 'maintainer', revision: 4 };
const projectB: TeamSpace = { id: 'project-b', kind: 'project', name: '移动端', role: 'contributor', revision: 2 };
const resourceA1 = publishedResource('pub-a1', 'support-assistant', '1.1.0', '客服助手');
const resourceA2 = publishedResource('pub-a2', 'support-assistant', '1.2.0', '客服助手');
const resourceB1 = publishedResource('pub-b1', 'release-notes', '2.0.0', '发布说明');
const catalog = [catalogEntry(resourceA1), catalogEntry(resourceA2), catalogEntry(resourceB1)];

afterEach(() => cleanup());

describe('TeamSharedResources', () => {
  it('lets an administrator publish exact versions and a maintainer save a bounded space selection', async () => {
    const user = userEvent.setup();
    const installationChanged = vi.fn();
    window.addEventListener(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT, installationChanged);
    const api = fakeApi({
      listResources: vi.fn().mockResolvedValue([resourceA1, resourceA2, resourceB1]),
      listResourceCatalog: vi.fn().mockResolvedValue(catalog),
      getSpaceResourceSelection: vi.fn().mockResolvedValue(selectionFor(projectA, [resourceA1])),
      publishResource: vi.fn().mockResolvedValue(resourceA2),
      updateSpaceResourceSelection: vi.fn().mockResolvedValue(selectionFor(projectA, [resourceA2, resourceB1])),
    });
    renderResources(api, sessionFor('admin', projectA), false, 'catalog');

    expect(await screen.findByRole('heading', { name: '团队共享资源' })).toBeVisible();
    expect((await screen.findAllByText('客服助手')).length).toBeGreaterThan(0);
    expect(api.listResourceCatalog).toHaveBeenCalledOnce();
    expect(screen.getByText('官网 · 项目成员可见')).toBeVisible();

    await user.selectOptions(screen.getByRole('combobox', { name: '客服助手版本' }), 'pub-a2');
    await user.selectOptions(screen.getByRole('combobox', { name: '发布说明版本' }), 'pub-b1');
    await user.click(screen.getByRole('button', { name: '保存当前空间选择' }));
    await waitFor(() => expect(api.updateSpaceResourceSelection).toHaveBeenCalledWith(
      'project-a',
      { baseRevision: 4, publicationIds: ['pub-a2', 'pub-b1'] },
      'csrf-memory',
    ));
    expect(installationChanged).toHaveBeenCalledOnce();

    await user.click(screen.getByRole('button', { name: '发布客服助手 v1.2.0' }));
    await waitFor(() => expect(api.publishResource).toHaveBeenCalledWith('support-assistant', '1.2.0', 'csrf-memory'));
    window.removeEventListener(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT, installationChanged);
  });

  it.each(['contributor', 'viewer'] as const)('shows the saved versions to a %s without granting edit or publish controls', async (role) => {
    const space = { ...projectB, role };
    const api = fakeApi({
      listResources: vi.fn().mockResolvedValue([resourceA1, resourceA2, resourceB1]),
      listResourceCatalog: vi.fn(),
      getSpaceResourceSelection: vi.fn().mockResolvedValue(selectionFor(space, [resourceA1])),
    });
    renderResources(api, sessionFor('member', space));

    expect(await screen.findByRole('heading', { name: '团队共享资源' })).toBeVisible();
    expect(await screen.findByText('当前空间只读')).toBeVisible();
    const selected = within(await screen.findByRole('region', { name: '当前空间选择' }));
    expect(selected.getByText('客服助手')).toBeVisible();
    expect(selected.getByText('v1.1.0')).toBeVisible();
    expect(selected.queryByText('v1.2.0')).not.toBeInTheDocument();
    expect(selected.queryByText('发布说明')).not.toBeInTheDocument();
    expect(selected.getByText('版本记录 v2')).toBeVisible();
    expect(api.listResourceCatalog).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /发布/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存当前空间选择' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.getByText('当前空间只读')).toBeVisible();
  });

  it('explains an empty saved selection to a member even when other versions are published', async () => {
    const api = fakeApi({
      getSpaceResourceSelection: vi.fn().mockResolvedValue(selectionFor(projectB, [])),
    });
    renderResources(api, sessionFor('member', projectB));

    const selected = within(await screen.findByRole('region', { name: '当前空间选择' }));
    expect(selected.getByText('当前空间尚未选择共享版本。')).toBeVisible();
    expect(selected.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('preserves a selection draft after CAS conflict until the user loads the latest choice', async () => {
    const user = userEvent.setup();
    const latest = selectionFor(projectA, [resourceA1]);
    const api = fakeApi({
      listResources: vi.fn().mockResolvedValue([resourceA1, resourceA2, resourceB1]),
      getSpaceResourceSelection: vi.fn().mockResolvedValue(latest),
      updateSpaceResourceSelection: vi.fn().mockRejectedValue(new TeamApiError(
        'team.space-resources.update',
        409,
        { error: 'selection changed', selection: latest },
      )),
    });
    renderResources(api, sessionFor('member', projectA));

    await screen.findByRole('heading', { name: '团队共享资源' });
    await screen.findByRole('combobox', { name: '客服助手版本' });
    await user.selectOptions(screen.getByRole('combobox', { name: '客服助手版本' }), 'pub-a2');
    await user.click(screen.getByRole('button', { name: '保存当前空间选择' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('资源选择已变化');
    expect(screen.getByRole('combobox', { name: '客服助手版本' })).toHaveValue('pub-a2');
    await user.click(screen.getByRole('button', { name: '读取最新空间选择' }));
    await waitFor(() => expect(api.getSpaceResourceSelection).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('combobox', { name: '客服助手版本' })).toHaveValue('pub-a1');
  });

  it('drops late responses from the previous account-space scope', async () => {
    const user = userEvent.setup();
    const oldResources = deferred<TeamPublishedResource[]>();
    const oldSelection = deferred<TeamResourceSelection>();
    const api = fakeApi({
      listResources: vi.fn()
        .mockReturnValueOnce(oldResources.promise)
        .mockResolvedValue([resourceB1]),
      getSpaceResourceSelection: vi.fn()
        .mockReturnValueOnce(oldSelection.promise)
        .mockResolvedValue(selectionFor(projectB, [resourceB1])),
    });
    render(
      <TeamProvider api={api}>
        <SpaceSwitch />
        <TeamSharedResources />
      </TeamProvider>,
    );

    await waitFor(() => expect(api.listResources).toHaveBeenCalledOnce());
    await user.click(screen.getByRole('button', { name: '切换到移动端' }));
    expect(await screen.findByText('移动端 · 项目成员可见')).toBeVisible();
    const selected = within(await screen.findByRole('region', { name: '当前空间选择' }));
    expect(await selected.findByText('发布说明')).toBeVisible();
    await actResolve(async () => {
      oldResources.resolve([resourceA1]);
      oldSelection.resolve(selectionFor(projectA, [resourceA1]));
    });

    expect(screen.queryByText('客服助手')).not.toBeInTheDocument();
    expect(selected.getByText('发布说明')).toBeVisible();
  });

  it('keeps the current resource view usable through StrictMode effect replay', async () => {
    const api = fakeApi();
    renderResources(api, sessionFor('admin', projectA), true);

    expect(await screen.findByRole('heading', { name: '团队共享资源' })).toBeVisible();
    expect((await screen.findAllByText('客服助手')).length).toBeGreaterThan(0);
  });
});

function SpaceSwitch() {
  const team = useTeam();
  return <button onClick={() => team.selectSpace(projectB.id)} type="button">切换到移动端</button>;
}

function renderResources(api: TeamApi, session: TeamSession, strict = false, pageId = 'installed') {
  const scopedApi = { ...api, me: vi.fn().mockResolvedValue(session) } as unknown as TeamApi;
  const tree = <TeamProvider api={scopedApi}><TeamSharedResources pageId={pageId} /></TeamProvider>;
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

function sessionFor(role: 'admin' | 'member', activeSpace: TeamSpace): TeamSession {
  const spaces = [activeSpace, ...[projectA, projectB].filter((space) => space.id !== activeSpace.id)];
  return {
    user: { id: role === 'admin' ? 'user-admin' : 'user-member', username: role, displayName: role === 'admin' ? '管理员' : '成员', role, active: true },
    csrfToken: 'csrf-memory',
    spaces,
  };
}

function fakeApi(overrides: Partial<Record<keyof TeamApi, unknown>> = {}): TeamApi {
  return {
    status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
    login: vi.fn(),
    me: vi.fn().mockResolvedValue(sessionFor('admin', projectA)),
    logout: vi.fn().mockResolvedValue({ ok: true }),
    createProject: vi.fn(),
    listMembers: vi.fn().mockResolvedValue([]),
    listDirectory: vi.fn().mockResolvedValue([]),
    createMember: vi.fn(),
    setMemberStatus: vi.fn(),
    listProjectMembers: vi.fn().mockResolvedValue([]),
    addProjectMember: vi.fn(),
    removeProjectMember: vi.fn(),
    listResources: vi.fn().mockResolvedValue([resourceA1, resourceB1]),
    listResourceCatalog: vi.fn().mockResolvedValue(catalog),
    publishResource: vi.fn().mockResolvedValue(resourceA2),
    setPublishedResourceStatus: vi.fn(),
    getSpaceResourceSelection: vi.fn().mockResolvedValue(selectionFor(projectA, [resourceA1])),
    updateSpaceResourceSelection: vi.fn().mockResolvedValue(selectionFor(projectA, [resourceA1])),
    getSessionResourceSnapshot: vi.fn(),
    listProjectDrafts: vi.fn().mockResolvedValue([]),
    listProjectSessions: vi.fn().mockResolvedValue([]),
    getProjectDraftDiff: vi.fn(),
    publishProjectDraft: vi.fn(),
    integrateProjectDraft: vi.fn(),
    adoptProjectDraft: vi.fn(),
    ...overrides,
  } as unknown as TeamApi;
}

function publishedResource(publicationId: string, packageId: string, version: string, displayName: string, status: 'published' | 'withdrawn' = 'published'): TeamPublishedResource {
  return {
    publicationId,
    packageId,
    version,
    digest: `${publicationId}-digest`,
    status,
    metadata: {
      displayName,
      description: `${displayName}说明`,
      publisher: 'PAW',
      source: { kind: 'bundled', label: 'Product bundle' },
      permissions: ['workspace.read'],
      compatibility: {},
      security: { reviewed: true, networkAccess: 'none' },
      installable: true,
      distribution: 'team_staged_source',
      version,
    },
    publishedByUserId: 'user-admin',
    publishedAtMs: 100,
    updatedAtMs: 200,
  };
}

function catalogEntry(resource: TeamPublishedResource): TeamResourceCatalogEntry {
  return { ...resource.metadata, packageId: resource.packageId, version: resource.version };
}

function selectionFor(space: TeamSpace, items: TeamPublishedResource[]): TeamResourceSelection {
  return {
    spaceId: space.id,
    revision: space.revision,
    publicationIds: items.map((item) => item.publicationId),
    items,
    updatedByUserId: 'user-admin',
    updatedAtMs: 300,
  };
}

async function actResolve(callback: () => void): Promise<void> {
  await act(async () => {
    callback();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
}
