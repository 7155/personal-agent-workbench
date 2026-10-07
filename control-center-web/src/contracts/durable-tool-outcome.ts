import { validateContract } from './validators';

/** The server has already checked native lineage; the UI also binds its exact owner.
 * Tool output, error prose and an aborted parent turn never create this receipt. */
export function hasAbortedDurableToolOutcome(
  payload: Record<string, unknown>,
  owner: { sessionId: string; turnId: string },
): boolean {
  const parsed = validateContract('pi-durable-tool-outcome.v1', payload.durableToolOutcome);
  if (!parsed.ok) return false;
  const outcome = parsed.value;
  return outcome.sessionId === owner.sessionId
    && outcome.turnId === owner.turnId
    && outcome.toolCallId === payload.toolCallId
    && outcome.toolName === payload.toolName;
}
