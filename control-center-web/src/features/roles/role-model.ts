import type { AgentModelCatalogV1 } from '@/contracts/generated/agent-model-catalog.v1';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';

export type TimelineModel = 'luna' | 'terra' | 'sol';

export type RoleBookProposal = {
  text: string;
  confidence: number;
  sourceEvidenceIds: string[];
};

export type RoleBookDailyDraft = {
  draftId: string;
  createdAtMs: number;
  proposalStatus: string;
  traitProposals: RoleBookProposal[];
  capabilityProposals: RoleBookProposal[];
  lessonProposals: RoleBookProposal[];
  commitmentProposals: RoleBookProposal[];
  decision: { decision?: string } | null;
};

export type RoleBookActivationSelection = {
  roleId: string;
  roleVersion: string;
  revisionId: string;
  draftId: string;
  traitIndexes: number[];
  capabilityIndexes: number[];
  lessonIndexes: number[];
  commitmentIndexes: number[];
};

export type RoleBookDiffItem = {
  itemId: string;
  text: string;
  evidenceIds: string[];
};

export type RoleBookDiffSection = {
  section: string;
  label: string;
  added: RoleBookDiffItem[];
  removed: RoleBookDiffItem[];
  changed: Array<{
    itemId: string;
    before: RoleBookDiffItem;
    after: RoleBookDiffItem;
  }>;
};

export type RoleModel = AgentModelCatalogV1['providers'][number]['models'][number];
export type RoleModelCatalog = {
  providers: Array<{ id: string; displayName: string; models: RoleModel[] }>;
};

export type AgentDefaultCompanion = {
  revision: number;
  roleId: string;
  roleVersion: string;
};

export const modelRouteIds = [
  'primary',
  'traceDiagnostic',
  'toolAgent',
  'subagent',
  'roomCoordinator',
] as const;

export type ModelRouteId = typeof modelRouteIds[number];
export type ModelRouteThinkingLevel =
  | 'inherit'
  | 'off'
  | 'minimal'
  | 'low'
  | 'medium'
  | 'high'
  | 'xhigh'
  | 'max';

export type AgentModelRoute = {
  modelProfile: string;
  thinkingLevel: ModelRouteThinkingLevel;
};

export type AgentModelRouting = {
  revision: number;
  routes: Record<ModelRouteId, AgentModelRoute>;
};

export const timelineOptions: ReadonlyArray<{
  value: TimelineModel;
  label: string;
  caption: string;
}> = [
  { value: 'luna', label: '初识阶段', caption: '好奇、轻快，侧重认识与记录' },
  { value: 'terra', label: '此刻阶段', caption: '温暖、清晰，侧重回顾与整理' },
  { value: 'sol', label: '构筑阶段', caption: '沉稳、面向行动，侧重协作与推进' },
];

export function createdSessionId(value: unknown): string {
  const session = record(record(value).session);
  return typeof session.id === 'string' ? session.id : '';
}

export function agentDefaultCompanion(value: unknown): AgentDefaultCompanion | null {
  const snapshot = record(record(value).configuration);
  const configuration = record(snapshot.configuration);
  const defaults = record(configuration.sessionDefaults);
  const revision = numberValue(snapshot.revision);
  const roleId = textValue(defaults.roleId);
  const roleVersion = textValue(defaults.roleVersion);
  return revision > 0 && roleId && roleVersion ? { revision, roleId, roleVersion } : null;
}

export function agentModelRouting(value: unknown): AgentModelRouting | null {
  const snapshot = record(record(value).configuration);
  const configuration = record(snapshot.configuration);
  const routing = record(configuration.modelRouting);
  const revision = numberValue(snapshot.revision);
  if (revision <= 0) return null;
  const routes = Object.fromEntries(modelRouteIds.map((routeId) => {
    const route = record(routing[routeId]);
    const thinkingLevel = textValue(route.thinkingLevel) || 'inherit';
    return [routeId, {
      modelProfile: textValue(route.modelProfile) || 'inherit',
      thinkingLevel: isModelRouteThinkingLevel(thinkingLevel) ? thinkingLevel : 'inherit',
    }];
  })) as Record<ModelRouteId, AgentModelRoute>;
  return { revision, routes };
}

function isModelRouteThinkingLevel(value: string): value is ModelRouteThinkingLevel {
  return ['inherit', 'off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].includes(value);
}

export function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function textValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

export function ensureControlOk(value: unknown): Record<string, unknown> {
  const response = record(value);
  if (response.ok === false) throw new Error(textValue(response.error) || '操作未完成。');
  return response;
}

export function toggleIndex(values: number[], index: number): number[] {
  return values.includes(index)
    ? values.filter((value) => value !== index)
    : [...values, index].sort((left, right) => left - right);
}

export function roleBookProposals(value: unknown): RoleBookProposal[] {
  return arrayValue(value).flatMap((item) => {
    const proposal = record(item);
    const text = textValue(proposal.text);
    if (!text) return [];
    return [{
      text,
      confidence: Math.max(0, Math.min(1, numberValue(proposal.confidence))),
      sourceEvidenceIds: arrayValue(proposal.sourceEvidenceIds).map(textValue).filter(Boolean),
    }];
  });
}

export function roleBookDailyDrafts(value: unknown): RoleBookDailyDraft[] {
  return arrayValue(record(value).dailyDrafts).flatMap((item) => {
    const draft = record(item);
    const draftId = textValue(draft.draftId);
    if (!draftId) return [];
    const decision = record(draft.decision);
    const diagnostics = record(draft.proposalDiagnostics);
    return [{
      draftId,
      createdAtMs: numberValue(draft.createdAtMs),
      proposalStatus: textValue(diagnostics.status),
      traitProposals: roleBookProposals(draft.traitProposals),
      capabilityProposals: roleBookProposals(draft.capabilityProposals),
      lessonProposals: roleBookProposals(draft.lessonProposals),
      commitmentProposals: roleBookProposals(draft.commitmentProposals),
      decision: Object.keys(decision).length ? { decision: textValue(decision.decision) } : null,
    }];
  });
}

export function roleBookHistory(value: unknown): Array<{
  revisionId: string;
  revisionNumber: number;
  status: string;
  changeSummary: string;
}> {
  return arrayValue(record(value).history).flatMap((item) => {
    const revision = record(item);
    const revisionId = textValue(revision.revisionId);
    if (!revisionId) return [];
    return [{
      revisionId,
      revisionNumber: numberValue(revision.revisionNumber),
      status: textValue(revision.status),
      changeSummary: textValue(revision.changeSummary),
    }];
  });
}

function roleBookDiffItem(value: unknown): RoleBookDiffItem | null {
  const item = record(value);
  const itemId = textValue(item.itemId);
  const text = textValue(item.text);
  if (!itemId || !text) return null;
  return {
    itemId,
    text,
    evidenceIds: arrayValue(item.evidenceIds).map(textValue).filter(Boolean),
  };
}

export function roleBookDiffSections(value: unknown): RoleBookDiffSection[] {
  return arrayValue(record(value).sections).flatMap((sectionValue) => {
    const section = record(sectionValue);
    const sectionId = textValue(section.section);
    const label = textValue(section.label);
    if (!sectionId || !label) return [];
    const added = arrayValue(section.added).flatMap((item) => {
      const parsed = roleBookDiffItem(item);
      return parsed ? [parsed] : [];
    });
    const removed = arrayValue(section.removed).flatMap((item) => {
      const parsed = roleBookDiffItem(item);
      return parsed ? [parsed] : [];
    });
    const changed = arrayValue(section.changed).flatMap((changeValue) => {
      const change = record(changeValue);
      const before = roleBookDiffItem(change.before);
      const after = roleBookDiffItem(change.after);
      const itemId = textValue(change.itemId);
      return before && after && itemId ? [{ itemId, before, after }] : [];
    });
    return [{ section: sectionId, label, added, removed, changed }];
  });
}

export function roleModelCatalog(value: unknown): RoleModelCatalog {
  const source = record(value);
  const providers = Array.isArray(source.providers) ? source.providers : [];
  return {
    providers: providers.flatMap((providerValue) => {
      const provider = record(providerValue);
      if (typeof provider.id !== 'string' || !Array.isArray(provider.models)) return [];
      const models = provider.models.filter((model): model is RoleModel => {
        const item = record(model);
        return typeof item.provider === 'string'
          && typeof item.id === 'string'
          && typeof item.name === 'string';
      });
      return [{
        id: provider.id,
        displayName: typeof provider.displayName === 'string' ? provider.displayName : provider.id,
        models,
      }];
    }),
  };
}

export function thinkingLabel(level: string): string {
  return ({
    off: '不启用推理', minimal: '最小', low: '低', medium: '中', high: '高', xhigh: '极高', max: 'Max',
  } as Record<string, string>)[level] ?? level;
}

export function personaPhase(persona: AgentPersonaV1): {
  id: TimelineModel | 'flash' | '';
  label: string;
} {
  const assetId = persona.visualProfile.avatarAssetId.toLowerCase();
  const policy = persona.defaults.modelPolicy.toLowerCase();
  if (assetId.includes('present') || policy.includes('terra')) return { id: 'terra', label: '此刻阶段' };
  if (assetId.includes('past') || policy.includes('luna')) return { id: 'luna', label: '初识阶段' };
  if (assetId.includes('future') || policy.includes('sol')) return { id: 'sol', label: '构筑阶段' };
  if (assetId.includes('flash')) return { id: 'flash', label: '闪念阶段' };
  return { id: '', label: '自定义阶段' };
}

export function modelDisplayName(profile?: string): string {
  const model = String(profile ?? '').split('/').at(-1) ?? '';
  return (({
    'gpt-6.1-sol': 'GPT-6.1 Sol',
    'gpt-5.6-sol': 'GPT-5.6 Sol',
    'gpt-5.6-terra': 'GPT-5.6 Terra',
    'gpt-5.6-luna': 'GPT-5.6 Luna',
    'deepseek-v4-flash': 'DeepSeek V4 Flash',
  } as Record<string, string>)[model] ?? model) || '跟随对话';
}

export function personaExpressionTraits(persona: AgentPersonaV1): string[] {
  return persona.traits.filter((trait) => {
    const normalized = trait.trim().toLowerCase();
    return !/^(?:5\.6\s+)?(?:terra|luna|sol)$/.test(normalized);
  });
}

export function normalizeTrait(value: string): string {
  return value.trim().replace(/\s+/gu, ' ').slice(0, 24);
}

export function normalizedTraits(traits: readonly string[], pending = ''): string[] {
  const result: string[] = [];
  for (const candidate of [...traits, pending]) {
    const value = normalizeTrait(candidate);
    if (!value || result.some((item) => item.toLocaleLowerCase() === value.toLocaleLowerCase())) continue;
    result.push(value);
    if (result.length === 5) break;
  }
  return result;
}
