/** TeamGateway is opt-in so local PAWOS never probes team authentication. */
export function isTeamDeployment(): boolean {
  const metaValue = typeof document === 'undefined'
    ? ''
    : document.querySelector('meta[name="paw-deployment"]')?.getAttribute('content')?.trim().toLowerCase() ?? '';
  const developmentOverride = import.meta.env.DEV
    ? import.meta.env.VITE_PAW_DEPLOYMENT?.trim().toLowerCase()
    : undefined;
  return metaValue === 'team' || developmentOverride === 'team';
}
