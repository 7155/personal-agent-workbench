import type { ControlRequest } from '@/platform/transport';
import type { MockRouteHandler } from '@/test/mock-transport';
import type { ControlPathId } from '@/platform/routes';
import type { LabProject } from '@/features/eval-lab/projects/types';

/** Public, in-memory projects only. Preview never reads files or runs models. */
export function createPreviewLabProjectRoutes(): Partial<Record<ControlPathId, MockRouteHandler>> {
  let nextId = 1;
  const projects = new Map<string, LabProject>();
  const receipts = new Map<string, { signature: string; receipt: { ok: true; project: LabProject; clientRequestId: string; replayed: false } }>();
  const makeProject = (projectId: string, description: string): LabProject => ({
    schemaVersion: 'rag-ime.agent-lab-project.v1', projectId, revision: 1,
    title: description.slice(0, 32), description, briefVersion: 1,
    materialCount: 0, artifactCount: 0, guideSessionId: '',
    createdAtMs: Date.now(), updatedAtMs: Date.now(), materialSetId: '',
    materialSet: { materialSetId: '', version: 0, materials: [], createdAtMs: null }, materialVersions: [],
    intake: { state: 'needs_materials', requestedPath: '', resolvedPath: '', readCount: 0, readBytes: 0, skippedCount: 0, partial: false, issues: [], checkedAtMs: null },
    artifacts: [], bindings: [], workspace: { artifactOrder: [], primaryArtifactId: '', layout: 'split' }, workspaceBinding: null,
  });
  return {
    'agent.eval-lab.apps.get': { ok: true, items: [], app: null, versions: [], calls: [] },
    'agent.eval-lab.apps.command': () => { throw Object.assign(new Error('演示模式没有真实应用版本；请连接 Lab 服务后再执行。'), { status: 422 }); },
    'agent.eval-lab.golden.get': { ok: true, items: [], suite: null },
    'agent.eval-lab.golden.command': () => { throw Object.assign(new Error('演示模式不运行评测；请连接真实 Lab 服务后创建评测集。'), { status: 422 }); },
    'agent.eval-lab.projects.get': (request: ControlRequest) => {
      const id = String(request.query?.projectId ?? '');
      if (id && !projects.has(id)) throw Object.assign(new Error('演示项目不存在，请返回工作台新建项目。'), { status: 404 });
      return { ok: true, items: [...projects.values()], project: projects.get(id) ?? null,
        knowledge: { schemaVersion: 'paw.lab-knowledge-resource.v1', corpora: [], indexes: [], datasets: [], evaluations: [], jobs: [], embedding: { provider: 'none', model: '' } },
        supportedViews: ['markdown', 'table', 'form', 'code', 'html', 'json'], availableAdapters: [], historyCollections: [], historyUnavailable: true };
    },
    'agent.eval-lab.projects.command': (request: ControlRequest) => {
      const body = request.body as Record<string, unknown>;
      const clientRequestId = String(body.clientRequestId ?? '');
      if (!clientRequestId || clientRequestId.length > 240) throw Object.assign(new Error('请保留有效的原请求标识。'), { status: 422 });
      const signature = JSON.stringify(body, (_key, value) => value && typeof value === 'object' && !Array.isArray(value)
        ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) : value);
      const previous = receipts.get(clientRequestId);
      if (previous) {
        if (previous.signature !== signature) throw Object.assign(new Error('此请求标识已用于不同内容，请保留修改后重新操作。'), { status: 409 });
        return { ...structuredClone(previous.receipt), replayed: true };
      }
      const input = (body.input ?? {}) as Record<string, unknown>;
      let project = projects.get(String(body.projectId ?? ''));
      if (body.action === 'create') {
        const description = String(input.description ?? '').trim();
        if (!description) throw Object.assign(new Error('请描述项目目标。'), { status: 422 });
        if (input.path || Array.isArray(input.materials) && input.materials.length) throw Object.assign(new Error('演示模式不读取本机材料；请先创建无附件项目，或连接真实服务后导入。'), { status: 422 });
        project = makeProject(`preview-project-${nextId++}`, description);
      } else {
        if (!project || body.expectedRevision !== project.revision) throw Object.assign(new Error('演示项目已变化，请重新读取。'), { status: 409 });
        if (body.action === 'update_brief') {
          project = { ...project, title: String(input.title ?? project.title), description: String(input.description ?? project.description), briefVersion: project.briefVersion + 1 };
        } else if (body.action === 'set_workspace') {
          project = { ...project, workspace: { ...project.workspace, ...input } };
        } else {
          throw Object.assign(new Error('此操作需要真实 Lab 服务；演示模式没有执行模型、评测或文件写入。'), { status: 422 });
        }
        project = { ...project, revision: project.revision + 1, updatedAtMs: Date.now() };
      }
      projects.set(project.projectId, project);
      const receipt = { ok: true as const, project, clientRequestId, replayed: false as const };
      receipts.set(clientRequestId, { signature, receipt: structuredClone(receipt) });
      return receipt;
    },
  };
}
