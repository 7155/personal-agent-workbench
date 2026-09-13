import type { LabEvaluationSelection } from './apps';
export type ProjectPage = 'workspace' | 'workflow' | 'artifact' | 'materials' | 'runs' | 'brief' | 'apps' | 'knowledge' | 'lifecycle';
export type ProjectView = { page: ProjectPage; guideOpen: boolean; artifactId?: string; bindingId?: string; jobId?: string; appId?: string; appVersion?: number; appCallId?: string; evaluationSelection?: LabEvaluationSelection };
export const defaultProjectView: ProjectView = { page:'workspace', guideOpen:true };
const key = (connection: string) => `paw.lab.project-views.v1:${connection}`;
export function readProjectViews(connection: string): Record<string, ProjectView> {
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(key(connection)) ?? '{}');
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
    return Object.fromEntries(Object.entries(raw).filter(([,value]) => value && typeof value === 'object'
      && ['workspace','workflow','artifact','materials','runs','brief','apps','knowledge','lifecycle'].includes(value.page) && typeof value.guideOpen === 'boolean'
      && (value.artifactId === undefined || typeof value.artifactId === 'string') && (value.bindingId === undefined || typeof value.bindingId === 'string') && (value.jobId === undefined || typeof value.jobId === 'string')
      && (value.appId === undefined || typeof value.appId === 'string') && (value.appCallId === undefined || typeof value.appCallId === 'string') && (value.appVersion === undefined || (Number.isSafeInteger(value.appVersion) && value.appVersion > 0))
      && (value.evaluationSelection === undefined || (typeof value.evaluationSelection?.suiteId === 'string' && typeof value.evaluationSelection?.jobId === 'string' && ['baseline', 'candidate'].includes(value.evaluationSelection?.variant)))).slice(-100));
  } catch { return {}; }
}
export function writeProjectViews(connection: string, views: Record<string, ProjectView>): boolean {
  try { sessionStorage.setItem(key(connection), JSON.stringify(Object.fromEntries(Object.entries(views).slice(-100)))); return true; }
  catch { return false; }
}
