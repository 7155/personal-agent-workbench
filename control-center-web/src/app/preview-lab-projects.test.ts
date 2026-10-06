import { describe, expect, it } from 'vitest';
import { commandLabProject, readLabProject } from '@/features/eval-lab/projects/api';
import type { ProjectCommand } from '@/features/eval-lab/projects/types';
import { createPreviewTransport } from './preview-control-transport';

describe('public preview Lab command recovery', () => {
  it('replays the original create, brief and workspace receipts without duplicating mutations', async () => {
    const transport = createPreviewTransport();
    const create: ProjectCommand = { action: 'create', expectedRevision: 0, clientRequestId: 'lab-project:create', input: { description: '公开模拟恢复检查' } };
    const created = await commandLabProject(transport, create);
    expect(await commandLabProject(transport, create)).toEqual({ ...created, replayed: true });
    for (const [action, input] of [['update_brief', { title: '公开修订' }], ['set_workspace', { layout: 'focus' }]] as const) {
      const current = (await readLabProject(transport, created.project.projectId)).project!;
      const command: ProjectCommand = { action, projectId: current.projectId, expectedRevision: current.revision, clientRequestId: `lab-project:${action}`, input };
      const receipt = await commandLabProject(transport, command);
      expect(await commandLabProject(transport, command)).toEqual({ ...receipt, replayed: true });
      expect((await readLabProject(transport, current.projectId)).project?.revision).toBe(current.revision + 1);
      await expect(commandLabProject(transport, { ...command, input: { title: '不同请求' } })).rejects.toMatchObject({ status: 409 });
    }
    const projects = await readLabProject(transport);
    expect(projects.items).toHaveLength(1);
    expect(await commandLabProject(transport, create)).toEqual({ ...created, replayed: true });
    await expect(commandLabProject(transport, { ...create, input: { description: '另一个项目' } })).rejects.toMatchObject({ status: 409 });
    expect((await readLabProject(transport)).items).toHaveLength(1);
  });

  it('rejects a missing request ID before creating a preview project', async () => {
    const transport = createPreviewTransport();
    await expect(transport.request({ pathId: 'agent.eval-lab.projects.command', body: { action: 'create', expectedRevision: 0, input: { description: '不能创建' } } })).rejects.toThrow('clientRequestId');
    expect((await readLabProject(transport)).items).toEqual([]);
  });
});
