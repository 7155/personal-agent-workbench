import { expect, test } from '@playwright/test';
import { expectNoHorizontalPageOverflow, productRoute, routeSurface } from './helpers';

test('Session and Room composers stay actionable through live desktop-to-mobile resizing', async ({ page }, info) => {
  test.skip(info.project.name !== 'desktop-1440x900');
  for (const target of ['agent', 'rooms']) {
    await page.goto(productRoute(target));
    const composer = page.getByRole('textbox', { name: target === 'agent' ? '消息' : '协作消息', exact: true });
    await expect(composer).toBeVisible();
    for (const width of [1280, 800, 390, 320, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await expectNoHorizontalPageOverflow(page);
      await composer.fill(`Draft at ${width}`);
      const hit = await composer.evaluate(element => {
        const box = element.getBoundingClientRect();
        return element.contains(document.elementFromPoint(box.left + box.width / 2, box.top + 16));
      });
      expect(hit).toBe(true);
      await expect(composer).toHaveValue(`Draft at ${width}`);
    }
  }
});

test('shared management content remains bounded inside its PAWOS App', async ({ page }) => {
  for (const route of ['plugins', 'voice', 'memory', 'history']) {
    await page.goto(productRoute(route));
    const body = routeSurface(page, route);
    await expect(body).toBeVisible();
    await expect(body.locator('.paw-app-loading, .paw-app-boot')).toHaveCount(0);
    await expectNoHorizontalPageOverflow(page);
  }
});
