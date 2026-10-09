/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/agent-model-catalog.v1.json
 */

export interface AgentModelCatalogV1 {
  schemaVersion: 'rag-ime.agent-model-catalog.v1';
  ok: true;
  sessionId: string;
  runtimeEngine?: 'classic' | 'durable';
  engineCapabilities?: {
    gatewayTools: boolean;
    compaction: boolean;
    compactionRecovery?: boolean;
    resume: boolean;
    exactAbort: boolean;
    nativeMcp: boolean;
    codemode: boolean;
    managedPlugins: boolean;
    conversationFork: boolean;
    conversationRewrite: boolean;
    commandCatalog: boolean;
    images: boolean;
  };
  selected: {
    [k: string]: unknown;
  } | null;
  thinkingLevel: 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';
  providers: {
    id: string;
    displayName: string;
    models: Model[];
  }[];
}
export interface Model {
  provider: string;
  id: string;
  name: string;
  api: string;
  reasoning: boolean;
  /**
   * @minItems 1
   */
  thinkingLevels: [
    'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max',
    ...('off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max')[],
  ];
  supportsImages: boolean;
  contextWindow: number;
  maxTokens: number;
}
