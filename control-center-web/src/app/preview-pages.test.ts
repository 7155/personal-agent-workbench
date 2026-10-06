import { describe, expect, it } from 'vitest';
import { createPreviewTransport } from './preview-control-transport';
import { parseProjectRead } from '@/features/eval-lab/projects/types';
import { parseGoldenRead } from '@/features/eval-lab/golden/types';
import { pawExtensionApps } from '@/paw-os/extensions/registry';
import { projectPawExtensionInstallation } from '@/paw-os/extensions/installation';

describe('preview page contracts', () => {
  it('opens an empty Lab, creates a project and reads the exact saved project', async () => {
    const transport = createPreviewTransport();
    expect(parseProjectRead(await transport.request({ pathId: 'agent.eval-lab.projects.get' })).items).toEqual([]);
    const created = await transport.request<{ project: { projectId: string } }>({ pathId: 'agent.eval-lab.projects.command', body: {
      action: 'create', expectedRevision: 0, clientRequestId: 'preview-create', input: { description: '演示项目：逐页检查' },
    } });
    const id = created.project.projectId;
    const read = parseProjectRead(await transport.request({ pathId: 'agent.eval-lab.projects.get', query: { projectId: id } }), id);
    expect(read.project?.description).toBe('演示项目：逐页检查');
    expect(read.project?.bindings).toEqual([]);
    expect(parseGoldenRead(await transport.request({ pathId: 'agent.eval-lab.golden.get' })).items).toEqual([]);
    await expect(transport.request({ pathId: 'agent.eval-lab.projects.command', body: {
      action: 'bind_execution', projectId: id, expectedRevision: 1, clientRequestId: 'no-execution', input: {},
    } })).rejects.toThrow('没有执行模型');
  });

  it('returns contract-valid empty Trace reports and session-free local Files', async () => {
    const transport = createPreviewTransport();
    expect(await transport.request({ pathId: 'observability.traceDiagnosticReports.list' })).toMatchObject({ items: [], total: 0, truncated: false });
    const listing = await transport.request<{ path: string; items: { path: string; name: string }[] }>({ pathId: 'files.list' });
    expect(listing.path.startsWith('/')).toBe(true);
    const path = listing.items.find(item => item.name === 'README.md')!.path;
    expect(await transport.request({ pathId: 'files.read', query: { path } })).toMatchObject({ ok: true, scope: 'local', requestedPath: path, path, content: expect.stringContaining('工作区文件预览') });
  });

  it('opens the installed extension using current manifest binding evidence', async () => {
    const transport = createPreviewTransport();
    const inventory = await transport.request({ pathId: 'agent.extensions.list' });
    const projection = projectPawExtensionInstallation(inventory);
    const app = pawExtensionApps.find(app => app.packageId === '@paw/zhanggui-wenshu')!;
    expect(projection.availableExtensionIds.has(app.id)).toBe(true);
    expect(projection.updateRequiredExtensionIds.has(app.id)).toBe(false);
  });

  it('round-trips a scene policy without resetting companion or Skill settings', async () => {
    const transport = createPreviewTransport();
    type Config = { configuration: { revision: number; configuration: Record<string, unknown> }; scenarioPolicyCatalog: { scenarios: { id: string; promptInstructions: string }[] } };
    const before = await transport.request<Config>({ pathId: 'agent.configuration.get' });
    expect(before.scenarioPolicyCatalog.scenarios.map(item => item.id)).toEqual(['ordinary', 'room', 'trace', 'agentLab']);
    const after = await transport.request<Config>({ pathId: 'agent.configuration.update', body: {
      expectedRevision: before.configuration.revision, changes: { 'scenarioPolicies.ordinary': { promptInstructions: '只核对当前项目', toolAllowlist: [] } }, updatedBy: 'preview-check',
    } });
    expect(after.scenarioPolicyCatalog.scenarios[0]?.promptInstructions).toBe('只核对当前项目');
    expect(after.configuration.configuration.skillRouting).toEqual(before.configuration.configuration.skillRouting);
    expect(after.configuration.configuration.sessionDefaults).toEqual(before.configuration.configuration.sessionDefaults);
  });
});
