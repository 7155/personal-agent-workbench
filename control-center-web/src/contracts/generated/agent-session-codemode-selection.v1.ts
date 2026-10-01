/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/agent-session-codemode-selection.v1.json
 */

export interface AgentSessionCodemodeSelectionV1 {
  schemaVersion: 'rag-ime.agent-session-codemode-selection.v1';
  ok: true;
  sessionId: string;
  codemodeMode: 'on' | 'only' | 'off';
  capability: {
    available: boolean;
    modes: ('off' | 'on' | 'only')[];
    defaultMode: 'off' | 'on' | 'only' | '';
  };
}
