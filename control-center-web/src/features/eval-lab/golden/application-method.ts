export type ApplicationMethodSource = { kind: 'inline' } | { kind: 'project_artifact'; projectId: string; artifactId: string; artifactRevision: number };
export type ApplicationMethodIdentity = { kind: 'application_skill'; title: string; sha256: string; source: ApplicationMethodSource };
export type ApplicationMethod = ApplicationMethodIdentity & { body: string };
export type ApplicationMethodInput = { body: string; title?: string; sha256?: string } | { artifactId: string; artifactRevision: number };
export type ApplicationMethodComparison = { scope: 'application_skill_body'; changed: boolean; baseline: ApplicationMethodIdentity | null; candidate: ApplicationMethodIdentity | null; diff: string };
const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
export function isApplicationMethodIdentity(value: unknown): value is ApplicationMethodIdentity {
  const item = object(value); const source = object(item.source);
  return item.kind === 'application_skill' && typeof item.title === 'string' && typeof item.sha256 === 'string' && /^[a-f0-9]{64}$/i.test(item.sha256)
    && (source.kind === 'inline' || (source.kind === 'project_artifact' && typeof source.projectId === 'string' && typeof source.artifactId === 'string' && Number.isSafeInteger(source.artifactRevision) && Number(source.artifactRevision) > 0));
}
export function isApplicationMethod(value: unknown): value is ApplicationMethod { return isApplicationMethodIdentity(value) && typeof object(value).body === 'string'; }
export function isApplicationMethodComparison(value: unknown): value is ApplicationMethodComparison {
  const item = object(value); return item.scope === 'application_skill_body' && typeof item.changed === 'boolean' && typeof item.diff === 'string'
    && (item.baseline === null || isApplicationMethodIdentity(item.baseline)) && (item.candidate === null || isApplicationMethodIdentity(item.candidate));
}
