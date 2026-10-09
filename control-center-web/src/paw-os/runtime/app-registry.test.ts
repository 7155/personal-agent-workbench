import { describe, expect, it } from 'vitest';
import { pawExtensionApp } from '../extensions/registry';
import { pawApp, pawAppForPath, pawApps } from './app-registry';

describe('PAWOS App registry', () => {
  it('maps legacy feature URLs onto native PAWOS Apps', () => {
    expect(pawAppForPath('/agent-controller')?.id).toBe('agent-controller');
    expect(pawAppForPath('/planning')?.id).toBe('project-workbench');
    expect(pawAppForPath('/rooms')?.id).toBe('agent');
    expect(pawAppForPath('/agent-capsule')?.id).toBe('agent-controller');
    expect(pawAppForPath('/voice')?.id).toBe('input-studio');
    expect(pawAppForPath('/context-debug')?.id).toBe('system-monitor');
    expect(pawAppForPath('/trace-agent?view=knowledge')?.id).toBe('trace-agent');
    expect(pawAppForPath('/trace-agent?reportId=trace-report%3A1')?.id).toBe('trace-agent');
    expect(pawAppForPath('/evolution-report')).toBeNull();
    expect(pawAppForPath('/appearance')?.id).toBe('system-settings');
  });

  it('keeps Project Field as the OS home instead of registering it as an App', () => {
    expect(pawAppForPath('/project-field')).toBeNull();
    expect(pawApps.some((app) => app.id === ('project-field' as never))).toBe(false);
  });

  it('does not silently turn an unknown hash into a project window', () => {
    expect(pawAppForPath('/not-a-paw-app')).toBeNull();
  });

  it('opens Memory on its topic library and preserves explicit timeline links', () => {
    expect(pawApp('memory').route).toBe('/memory');
    expect(pawAppForPath('/memory?view=timeline')?.id).toBe('memory');
  });

  it('discovers source-isolated Extension Apps without adding business ids to the core registry', () => {
    const extension = pawApps.find((app) => app.id === 'extension:zhanggui-wenshu');
    expect(extension).toMatchObject({
      label: '掌柜问数',
      route: '/extensions/zhanggui-wenshu',
      kind: 'agent',
    });
    expect(pawExtensionApp('extension:zhanggui-wenshu').sandbox).toEqual({
      default: 'optional',
      connectorPackageId: 'vertical-agent-sandbox',
      policyId: 'vertical-readonly-v1',
    });
    expect(pawAppForPath('/extensions/zhanggui-wenshu')?.id).toBe('extension:zhanggui-wenshu');
  });
});
