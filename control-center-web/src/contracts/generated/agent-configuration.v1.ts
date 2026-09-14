/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/agent-configuration.v1.json
 */

/**
 * @maxItems 128
 */
export type SkillRoute = string[];

export interface AgentConfigurationV1 {
  schemaVersion: 'rag-ime.agent-configuration.v1';
  revision: number;
  revisionToken: string;
  configuration: {
    runtime: {
      enabled: boolean;
      startup: 'lazy';
      idleTimeoutSeconds: number;
      [k: string]: unknown;
    };
    sessionDefaults: {
      resumeLastSession: boolean;
      roleId: string;
      roleVersion: string;
      modelProfile: string;
      toolProfileVersion: string;
      capabilityDisclosurePreferences: {
        [k: string]: 'inherit' | 'enabled' | 'disabled';
      };
      [k: string]: unknown;
    };
    coordination: {
      enabled: boolean;
      [k: string]: unknown;
    };
    modelRouting: {
      primary: ModelRoute;
      traceDiagnostic: ModelRoute;
      toolAgent: ModelRoute;
      subagent: ModelRoute;
      roomCoordinator: ModelRoute;
    };
    prompts?: {
      systemInstructions: string;
      compactionInstructions: string;
    };
    skillRouting: {
      ordinary: SkillRoute;
      room: SkillRoute;
      trace: SkillRoute;
      agentLab: SkillRoute;
    };
    scenarioPolicies: {
      ordinary: ScenarioPolicy;
      room: ScenarioPolicy;
      trace: ScenarioPolicy;
      agentLab: ScenarioPolicy;
    };
    [k: string]: unknown;
  };
  sync: {
    state: 'synchronized' | 'pending' | 'failed';
    appliedRevision: number;
    error: string;
    [k: string]: unknown;
  };
  updatedAtMs: number;
  updatedBy: string;
  lastEventId: string;
  [k: string]: unknown;
}
export interface ModelRoute {
  modelProfile: string;
  thinkingLevel: 'inherit' | 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';
}
export interface ScenarioPolicy {
  promptInstructions: string;
  toolAllowlist: string[];
}
