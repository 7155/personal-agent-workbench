import { expect, test } from '@playwright/test';
import { expectNoHorizontalPageOverflow, productRoute, routeSurface, routes } from './helpers';

test('all product deep links render inside their single PAWOS owner', async ({ page }, info) => {
  test.setTimeout(180_000);
  test.skip(!['desktop-1440x900', 'mobile-390x844'].includes(info.project.name));
  const evidence = [];
  for (const route of routes) {
    await page.goto(productRoute(route.id));
    const surface = routeSurface(page, route.id);
    await expect(surface).toBeVisible();
    await expect(surface.locator('.paw-app-boot, .paw-app-loading')).toHaveCount(0);
    await expect.poll(() => surface.innerText()).not.toBe('');
    await expectNoHorizontalPageOverflow(page);
    await expect(page.locator('.control-shell')).toHaveCount(0);
    const bounds = await surface.boundingBox();
    expect(bounds!.width).toBeGreaterThan(300);
    expect(bounds!.height).toBeGreaterThan(120);
    const controls = surface.locator('button:visible, input:visible, textarea:visible, a:visible, summary:visible');
    expect(await controls.count()).toBeGreaterThan(0);
    evidence.push({ route: route.id, width: bounds!.width, height: bounds!.height, controls: await controls.count() });
  }
  await info.attach('single-owner-routes.json', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' });
});
