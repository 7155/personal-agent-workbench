export const EVOLUTION_REPORT_PATH = '/evolution-report';

export type StandaloneSurfaceId = 'evolution-report' | 'screen-assistant' | 'agent-capsule' | 'desktop-pet';

export function standaloneSurfaceForPath(pathname: string, search = ''): StandaloneSurfaceId | null {
  const normalized = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  if (normalized === '/desktop-pet') return 'desktop-pet';
  if (normalized === '/screen-assistant') return 'screen-assistant';
  // The managed Agent Capsule owns both its PAWOS launcher and the compact
  // capture conversation. Only the explicit capture surface escapes the
  // desktop shell; ordinary /agent-capsule navigation remains a PAWOS App.
  if (normalized === '/agent-capsule' && new URLSearchParams(search).get('surface') === 'capture') return 'agent-capsule';
  return normalized === EVOLUTION_REPORT_PATH ? 'evolution-report' : null;
}
