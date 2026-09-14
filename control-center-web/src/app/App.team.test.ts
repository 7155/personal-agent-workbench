import { afterEach, describe, expect, it, vi } from 'vitest';
import { isTeamDeployment } from './App';

afterEach(() => {
  document.querySelector('meta[name="paw-deployment"]')?.remove();
  vi.unstubAllEnvs();
});

describe('isTeamDeployment', () => {
  it('only enables Team mode when TeamGateway marks the deployment', () => {
    expect(isTeamDeployment()).toBe(false);

    const meta = document.createElement('meta');
    meta.name = 'paw-deployment';
    meta.content = 'team';
    document.head.append(meta);

    expect(isTeamDeployment()).toBe(true);
  });

  it('supports a development-only deployment override', () => {
    vi.stubEnv('VITE_PAW_DEPLOYMENT', 'team');
    expect(isTeamDeployment()).toBe(true);
  });
});
