export function assistantLaunchIntent(argv) {
  const argument = (prefix) => argv.find((value) => typeof value === 'string' && value.startsWith(prefix))?.slice(prefix.length);
  if (argv.includes('--paw-capture')) {
    const raw = argument('--paw-source-app=') || '';
    return { kind: 'capture', sourceAppBundleId: /^[A-Za-z0-9.-]{1,200}$/.test(raw) ? raw : '' };
  }
  const sessionId = argument('--paw-session=');
  if (sessionId === undefined || sessionId.length > 200 || /[\x00-\x20\x7f]/.test(sessionId)) return null;
  return { kind: 'session', sessionId };
}

export function assistantSessionRoute(sessionId) {
  return sessionId ? `/agent?session=${encodeURIComponent(sessionId)}` : '/agent-controller';
}
