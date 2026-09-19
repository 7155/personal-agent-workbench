/** Pi Package receipts are appended to the Session. Keep this UI's polling
 * commands and mutations ordered so one invocation cannot read another's receipt. */
export function createGISCommandQueue() {
  const tails = new Map<string, Promise<unknown>>();
  return async function run<T>(sessionId: string, operation: () => Promise<T>): Promise<T> {
    const pending = (tails.get(sessionId) ?? Promise.resolve()).then(operation, operation);
    tails.set(sessionId, pending);
    try { return await pending; }
    finally { if (tails.get(sessionId) === pending) tails.delete(sessionId); }
  };
}
