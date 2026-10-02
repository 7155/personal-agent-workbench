import { expect, test } from '@playwright/test';

test('strict CSP stays eval-free and still permits the React app to boot', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1440x900', 'one CSP execution gate is sufficient');
  const pageErrors: string[] = [];
  const policyErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error' && /Content Security Policy|invalid source/i.test(message.text())) {
      policyErrors.push(message.text());
    }
  });

  await page.goto('/#/overview');
  await expect(page.locator('.paw-workbench')).toBeVisible();

  const contentSecurityPolicy = await page
    .locator('meta[http-equiv="Content-Security-Policy"]')
    .getAttribute('content');
  expect(contentSecurityPolicy).toContain("script-src 'self'");
  expect(contentSecurityPolicy).toContain("frame-src 'self' blob:");
  // File/Browser previews need these explicit origins. Reject broad HTTP
  // wildcards and invalid host syntax instead of banning their supported path.
  const frameSources = contentSecurityPolicy?.match(/(?:^|;)\s*frame-src\s+([^;]+)/)?.[1]
    .trim().split(/\s+/);
  expect(frameSources).toEqual(["'self'", 'blob:', 'https:', 'http://127.0.0.1:*', 'http://localhost:*']);
  expect(contentSecurityPolicy).not.toContain("'unsafe-eval'");
  expect(pageErrors).toEqual([]);
  expect(policyErrors).toEqual([]);
});

test('browser preview does not expose the native message handler', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1440x900', 'one browser capability gate is sufficient');
  await page.goto('/#/overview');
  await expect(page.locator('.paw-workbench')).toBeVisible();

  const state = await page.evaluate(() => ({
    nativeHandler: Boolean(
      (window as Window & {
        webkit?: { messageHandlers?: { ragImeNativeBridge?: unknown } };
      }).webkit?.messageHandlers?.ragImeNativeBridge,
    ),
    transport: document.documentElement.dataset.controlTransport,
  }));
  expect(state.nativeHandler).toBe(false);
  expect(state.transport).toBe('mock');
});
