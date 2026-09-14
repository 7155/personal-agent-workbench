/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/agent-tool-call.v1.json
 */

export interface AgentToolCallV1 {
  schemaVersion: 'rag-ime.agent-tool-call.v1';
  sessionId: string;
  tool:
    | 'overview'
    | 'input'
    | 'voice'
    | 'planning'
    | 'agent_schedule'
    | 'memory'
    | 'agent_role_book'
    | 'knowledge'
    | 'models'
    | 'runtime'
    | 'configuration'
    | 'agents'
    | 'session_search'
    | 'trace_diagnostics'
    | 'room_partner'
    | 'structured_output'
    | 'browser'
    | 'todo'
    | 'agent_goal'
    | 'plugins'
    | 'sandbox'
    | 'work_documents'
    | 'lab_project'
    | 'connections'
    | 'desktop_semantic'
    | 'ls'
    | 'read'
    | 'grep'
    | 'find'
    | 'edit'
    | 'write'
    | 'bash'
    | 'workspace_list'
    | 'workspace_lsp'
    | 'workspace_read'
    | 'workspace_search'
    | 'workspace_patch'
    | 'workspace_edit'
    | 'workspace_write'
    | 'workspace_shell'
    | 'workspace_job';
  toolCallId: string;
  sourceLoopId?: string;
  args: {
    [k: string]: unknown;
  };
  roomCapability?: {
    [k: string]: unknown;
  };
  loadReceiptId?: string;
  runtimeContext?: {
    schemaVersion: 'rag-ime.agent-runtime-context.v1';
    /**
     * @minItems 1
     * @maxItems 2
     */
    forkSessions:
      | [
          {
            sessionId: string;
            sessionFile: string;
            parentSessionFile: string;
            parentLeafId: string;
            thinkingOverride?: 'off';
          },
        ]
      | [
          {
            sessionId: string;
            sessionFile: string;
            parentSessionFile: string;
            parentLeafId: string;
            thinkingOverride?: 'off';
          },
          {
            sessionId: string;
            sessionFile: string;
            parentSessionFile: string;
            parentLeafId: string;
            thinkingOverride?: 'off';
          },
        ];
  };
}
