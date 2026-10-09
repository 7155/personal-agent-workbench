/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/pi-durable-tool-outcome.v1.json
 */

/**
 * Exact Pi-owned logical ToolTask aborted outcome. This does not assert physical drain, Session idle, or parent acceptance. Native adapter verifies original ToolResult, ToolTask, assistant GenerationTask and request lineage before publication.
 */
export interface PiDurableToolOutcomeV1 {
  schemaVersion: 'rag-ime.pi-durable-tool-outcome.v1';
  sessionId: string;
  runtimeSessionId: string;
  turnId: string;
  clientMessageId: string;
  toolCallId: string;
  toolName: string;
  entryId: string;
  taskId: string;
  assistantEntryId: string;
  generationTaskId: string;
  status: 'aborted';
}
