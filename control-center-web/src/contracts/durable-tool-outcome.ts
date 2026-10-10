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

/** Classic's native execution owner emits this flag only for an AbortError
 * thrown with that execution's aborted signal. Nested Tool output has no authority. */
export function hasAbortedToolOutcome(
  payload: Record<string, unknown>,
  owner: { sessionId: string; turnId: string },
): boolean {
  return hasAbortedDurableToolOutcome(payload, owner)
    || (Boolean(owner.sessionId && owner.turnId)
      && payload.cancelled === true && payload.isError === true
      && typeof payload.toolCallId === 'string' && Boolean(payload.toolCallId)
      && typeof payload.toolName === 'string' && Boolean(payload.toolName));
}
