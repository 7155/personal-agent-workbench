import { expect, test, type Locator, type Page } from '@playwright/test';

test.beforeEach(({}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1440x900', 'resizable desktop App contract');
});

for (const reducedMotion of ['no-preference', 'reduce'] as const) {
  test(`plugin content fits a 732px window and toolbar remains clickable (${reducedMotion})`, async ({ page }, testInfo) => {
    await page.emulateMedia({ reducedMotion });
    for (const view of ['skills', 'capabilities']) {
      await page.goto(`/?controlTransport=mock#/plugins?view=${view}`);
      const shell = page.locator('.paw-window-shell[data-app="app-center"]');
      const list = shell.getByRole('group', { name: view === 'skills' ? 'Skill 列表' : '能力列表' });
      await expect(list).toBeVisible();
      await resizeWindow(page, shell, 732);

      const refresh = shell.getByRole('button', { name: '刷新', exact: true });
      await expect(refresh).toBeEnabled();
      await expect.poll(() => receivesPointer(refresh)).toBe(true);
      await refresh.click();
      await expect(list).toBeVisible();
      const trigger = list.getByRole('button').first();
      await trigger.click();
      const detail = shell.getByRole('complementary', { name: view === 'skills' ? 'Skill 详情' : '能力详情' });
      await expect(detail).toBeVisible();
      await expect(detail).toBeFocused();
      await expect(detail.locator('.plugins-detail__toolbar')).toBeInViewport({ ratio: 1 });
      await trigger.click();
      await expect(detail).toBeFocused();
      await expect(detail.locator('.plugins-detail__toolbar')).toBeInViewport({ ratio: 1 });
      await testInfo.attach('plugins-' + view + '-detail-arrival-' + reducedMotion + '.png', {
        body: await shell.screenshot(), contentType: 'image/png',
      });
      const browser = shell.locator('.plugins-browser');
      const geometry = await browser.evaluate((node) => {
        const rect = node.getBoundingClientRect();
        const listRect = node.querySelector('.plugins-list')!.getBoundingClientRect();
        const detailRect = node.querySelector('.plugins-detail')!.getBoundingClientRect();
        return {
          width: rect.width,
          clientWidth: node.clientWidth,
          scrollWidth: node.scrollWidth,
          listLeft: listRect.left,
          detailLeft: detailRect.left,
          detailRight: detailRect.right,
          right: rect.right,
        };
      });
      expect(geometry.width).toBeLessThan(550);
      expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
      expect(Math.abs(geometry.listLeft - geometry.detailLeft)).toBeLessThanOrEqual(1);
      expect(geometry.detailRight).toBeLessThanOrEqual(geometry.right + 1);

      const close = detail.getByRole('button', { name: view === 'skills' ? '关闭 Skill 详情' : '关闭能力详情' });
      await expect.poll(() => receivesPointer(close)).toBe(true);
      await close.focus();
      await page.keyboard.press('Escape');
      await expect(detail).toHaveCount(0);
      await expect(trigger).toBeFocused();
      await expect(shell.locator('.mgmt-page')).toHaveJSProperty('scrollLeft', 0);
      await testInfo.attach(`plugins-${view}-732-${reducedMotion}.png`, {
        body: await shell.screenshot(), contentType: 'image/png',
      });
    }
  });
}

test('settings section headings keep their natural height alongside the group rail', async ({ page }, testInfo) => {
  await page.goto('/?controlTransport=mock#/configuration');
  const shell = page.locator('.paw-window-shell[data-app="system-settings"]');
  const fields = shell.locator('.configuration-editor__fields');
  await expect(fields).toBeVisible();
  await resizeWindow(page, shell, 1100);
  const groups = shell.locator('.configuration-section-nav button');
  const count = await groups.count();
  expect(count).toBeGreaterThan(1);
  for (let index = 0; index < Math.min(count, 4); index += 1) {
    await groups.nth(index).click();
    const geometry = await fields.evaluate((node) => {
      const heading = node.querySelector('.configuration-editor__heading')!.getBoundingClientRect();
      const list = node.querySelector('.mgmt-list')!.getBoundingClientRect();
      return { headingHeight: heading.height, gap: list.top - heading.bottom };
    });
    expect(geometry.headingHeight).toBeLessThan(90);
    expect(geometry.gap).toBeGreaterThanOrEqual(0);
    expect(geometry.gap).toBeLessThanOrEqual(24);
  }
  const refresh = shell.getByRole('button', { name: '刷新', exact: true });
  await expect.poll(() => receivesPointer(refresh)).toBe(true);
  await refresh.click();
  await testInfo.attach('settings-natural-heading-height.png', {
    body: await shell.screenshot(), contentType: 'image/png',
  });
});

async function resizeWindow(page: Page, shell: Locator, width: number) {
  const handle = shell.locator('.paw-window-resize[data-handle="east"]');
  const current = await shell.boundingBox();
  const edge = await handle.boundingBox();
  expect(current).not.toBeNull();
  expect(edge).not.toBeNull();
  const x = edge!.x + edge!.width / 2;
  const y = edge!.y + edge!.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + width - current!.width, y, { steps: 8 });
  await page.mouse.up();
  await expect.poll(async () => Math.abs((await shell.boundingBox())!.width - width)).toBeLessThanOrEqual(2);
}

async function receivesPointer(target: Locator) {
  return target.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return Boolean(hit && (hit === node || node.contains(hit)));
  });
}
