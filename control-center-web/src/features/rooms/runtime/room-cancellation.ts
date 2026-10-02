/** A successful Stop request can find a Root that already finished normally.
 * Only an explicit termination receipt confirms that cancellation was applied. */
export function roomCancellationOutcome(
  receipt: Record<string, unknown>,
): 'terminated' | 'already_terminal' | 'pending' {
  if (receipt.ok === true && (receipt.status === 'terminated' || receipt.status === 'already_terminal')) {
    return receipt.status;
  }
  return 'pending';
}
