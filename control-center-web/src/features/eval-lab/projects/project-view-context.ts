import type { WorkspaceComposerContext } from '@/paw-os/apps/workspace-draft';
import type { ArtifactSummary, LabArtifact, LabProject } from './types';
import type { LabWorkflowNode } from './project-workflow-types';
import type { ProjectPage } from './views';

const excerpt = (value: unknown, limit: number) => {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  return { text: text.slice(0, limit), truncated: text.length > limit };
};

/** Capture the same saved revision the user sees; never substitute another cached artifact. */
export function projectViewContext(project: LabProject, page: ProjectPage, selected?: ArtifactSummary, artifact?: LabArtifact, node?: LabWorkflowNode): Omit<WorkspaceComposerContext, 'onClear'> {
  const showsArtifact = (page === 'workspace' || page === 'artifact') && !node && selected;
  const matched = showsArtifact && artifact?.artifactId === selected.artifactId && artifact.revision === selected.revision;
  const current = node ? {
    kind: 'workflow_record', ...excerpt(node, 10000),
  } : showsArtifact ? {
    kind: 'artifact', artifactId: selected.artifactId, revision: selected.revision,
    title: selected.title, summary: selected.summary,
    contentState: matched ? 'loaded' : 'not_loaded',
    ...(matched ? { content: excerpt(artifact.content, 12000) } : {}),
    read: { op: 'read', artifactId: selected.artifactId, artifactRevision: selected.revision },
  } : { kind: 'project_page', page };
  return {
    kind: 'project', label: node?.title ?? (showsArtifact ? selected.title : project.title),
    detail: `${showsArtifact ? `v${selected.revision} · ${matched ? '正文已附带' : '正文未就绪，附带读取引用'}` : node ? '实验结果与来源已附带' : '项目概况已附带'} · 随下一条消息发送`,
    text: JSON.stringify({ projectId: project.projectId, projectRevision: project.revision, page,
      current, counts: project.workflow?.counts,
      artifacts: project.artifacts.slice(0, 30).map(({ artifactId, revision, title }) => ({ artifactId, revision, title })),
      artifactCount: project.artifacts.length,
    }),
  };
}
