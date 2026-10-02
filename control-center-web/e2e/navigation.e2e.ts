import { expect, test } from '@playwright/test';
import { pawOsAppRegistry } from '../src/features/paw-os/model/app-registry';
import { expectNoHorizontalPageOverflow } from './helpers';

test('the default and historical frontend URLs open the same PAWOS desktop', async ({ page }) => {
  for (const search of ['', '?frontend=legacy', '?frontend=paw-os']) {
    await page.goto(`/${search}`);
    await expect(page.getByTestId('paw-os-product-root')).toBeVisible();
    await expect(page.locator('.paw-wayfinder')).toBeVisible();
    await expect(page.locator('.control-shell')).toHaveCount(0);
    await expectNoHorizontalPageOverflow(page);
  }
});

test('Launchpad opens every registered App through the product window host', async ({ page }, info) => {
  test.setTimeout(120_000);
  test.skip(!['desktop-1440x900', 'mobile-390x844'].includes(info.project.name));
  await page.goto('/?controlTransport=mock');
  for (const app of pawOsAppRegistry) {
    await page.getByRole('button', { name: '全部 App', exact: true }).click();
    const launcher = page.getByRole('dialog', { name: '全部 App' });
    await launcher.locator(`button[data-app="${app.id}"]`).click();
    await expect(launcher).toBeHidden();
    const shell = page.locator(`.paw-window-shell[data-app="${app.id}"]`);
    await expect(shell).toBeVisible();
    await expectNoHorizontalPageOverflow(page);
    await shell.getByRole('button', { name: '关闭窗口', exact: true }).click();
    await expect(shell).toBeHidden();
  }
});

test('Launchpad traps keyboard focus and returns it to the opener', async ({ page }) => {
  await page.goto('/?controlTransport=mock');
  const trigger = page.getByRole('button', { name: '全部 App', exact: true });
  await trigger.focus();
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: '全部 App' });
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Tab');
  expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});
