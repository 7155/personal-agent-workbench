import { expect, test } from '@playwright/test';
import { expectNoHorizontalPageOverflow } from './helpers';

test('the canonical Wayfinder keeps its navigation reachable in compact viewports', async ({ page }, info) => {
  test.skip(info.project.name !== 'desktop-1440x900');
  for (const viewport of [{ width: 375, height: 812 }, { width: 667, height: 375 }, { width: 1280, height: 820 }]) {
    await page.setViewportSize(viewport);
    await page.goto('/?controlTransport=mock#/project-field');
    await expect(page.locator('.paw-wayfinder')).toBeVisible();
    const launch = page.getByRole('button', { name: '全部 App', exact: true });
    await expect(launch).toBeVisible();
    await launch.click();
    const dialog = page.getByRole('dialog', { name: '全部 App' });
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: /^Agent / }).first().click();
    await expect(page.locator('.paw-window-shell[data-app="agent"]')).toBeVisible();
    await expectNoHorizontalPageOverflow(page);
  }
});
