import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function theme(page: Page, value: string) {
  await page.evaluate(value => { document.documentElement.dataset.theme = value; }, value);
}
async function contrast(page: Page, selector: string) {
  await page.evaluate(async () => {
    await Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {})));
  });
  const result = await new AxeBuilder({ page }).include(selector).withRules(['color-contrast']).analyze();
  expect(result.violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => ({ target: n.target, reason: n.failureSummary })) }))).toEqual([]);
}
async function noOverflow(page: Page) {
  expect(await page.locator('body').evaluate(el => el.scrollWidth <= innerWidth + 1)).toBe(true);
}

test('V5 tool tabs retain evidence and capability changes stay with their partner', async ({ page }, info) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/e2e/fixtures/pi-content-v5.html');
  await page.locator('.ccui-tool-head').click();
  await expect(page.getByRole('tab', { name: '返回内容' })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('tab', { name: '调用参数' }).click();
  await expect(page.getByRole('tabpanel')).toContainText('design/screenshots/');
  await page.keyboard.press('ArrowLeft');
  await expect(page.getByRole('tabpanel')).toContainText('3 张图片返回');
  await page.getByRole('button', { name: '切换工具执行状态' }).click();
  await expect(page.getByRole('tab', { name: '实时片段' })).toBeVisible();
  await page.getByRole('button', { name: '切换工具执行状态' }).click();
  for (const mode of ['light', 'dark']) {
    await theme(page, mode); await contrast(page, '.ccui-tool-card');
    await page.getByRole('button', { name: /对话功能：/ }).click();
    if (page.viewportSize()!.width >= 1024) expect((await page.locator('.pi-capabilities-popover').boundingBox())!.width).toBeGreaterThan(650);
    await page.getByRole('button', { name: /^写入文件/ }).click();
    await expect(page.locator('.pi-capabilities__detail')).toContainText('受权限限制');
    await expect(page.getByRole('button', { name: '加入消息' })).toBeDisabled();
    await contrast(page, '.pi-capabilities');
    await page.screenshot({ path: info.outputPath(`v5-capabilities-${mode}.png`), fullPage: true });
    await page.keyboard.press('Escape');
    await expect(page.getByRole('button', { name: /对话功能：/ })).toBeFocused();
  }
  await page.getByRole('combobox', { name: '选择伙伴' }).selectOption('fixture-mars');
  await page.getByRole('button', { name: /对话功能：/ }).click();
  await page.getByRole('button', { name: /^终端命令/ }).click();
  await expect(page.locator('.pi-capabilities__detail')).toContainText('受权限限制');
  await expect(page.getByRole('button', { name: '加入消息' })).toBeDisabled();
  expect(errors).toEqual([]); await noOverflow(page);
});

test('V6 image focus, zoom, comparison and independent canvas preserve reading state', async ({ page }, info) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/e2e/fixtures/delivery-studio-v6.html');
  await page.getByRole('button', { name: '聚焦下一张' }).click();
  const second = await page.locator('.paw-image-gallery__tile').getAttribute('data-image-id');
  await page.getByRole('button', { name: '总览', exact: true }).click();
  await page.getByRole('button', { name: '聚焦', exact: true }).click();
  await expect(page.locator('.paw-image-gallery__tile')).toHaveAttribute('data-image-id', second!);
  for (const mode of ['light', 'dark']) {
    await theme(page, mode); await contrast(page, '.paw-image-gallery');
    await page.locator('.paw-image-gallery__tile').click();
    const dialog = page.getByRole('dialog'); await expect(dialog).toHaveCount(1);
    await expect(dialog.getByRole('button', { name: '原尺寸', exact: true })).toBeEnabled();
    await dialog.getByRole('button', { name: '原尺寸', exact: true }).click();
    await expect(dialog.locator('output')).toHaveText('100%');
    await dialog.getByRole('button', { name: '放大图片' }).click();
    await expect(dialog.locator('output')).toHaveText('125%');
    for (const label of ['深色画布', '透明网格画布', '浅色画布']) {
      await dialog.getByRole('button', { name: label, exact: true }).click();
      await expect(page.locator('html')).toHaveAttribute('data-theme', mode);
    }
    await dialog.getByRole('button', { name: '对照', exact: true }).click();
    await expect(dialog.getByRole('combobox', { name: '对照图片 A' })).toBeVisible();
    await dialog.getByRole('button', { name: '查看', exact: true }).click();
    await contrast(page, '.paw-image-dialog');
    await page.screenshot({ path: info.outputPath(`v6-image-${mode}.png`), fullPage: true });
    // Explicit decode-failure injection; verifies error copy on each independent canvas.
    await dialog.locator('.paw-image-viewer__viewport img').dispatchEvent('error');
    for (const label of ['深色画布', '透明网格画布', '浅色画布']) {
      await dialog.getByRole('button', { name: label, exact: true }).click();
      await contrast(page, '.paw-image-dialog');
    }
    await dialog.getByRole('button', { name: '重新加载' }).click();
    await expect(dialog.getByRole('button', { name: '原尺寸', exact: true })).toBeEnabled();
    await page.keyboard.press('Escape'); await expect(page.locator('.paw-image-gallery__tile')).toBeFocused();
  }
  expect(errors).toEqual([]); await noOverflow(page);
});

test('V6 delivery desk preserves pins, selection, list order and one-dialog task return', async ({ page }, info) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/e2e/fixtures/delivery-studio-v6.html');
  for (const mode of ['light', 'dark']) {
    await theme(page, mode); await page.getByRole('button', { name: /打开成果桌/ }).click();
    const cards = page.locator('[data-delivery-select]');
    const before = await cards.evaluateAll(nodes => nodes.map(n => n.getAttribute('data-delivery-select')));
    const pin = page.locator('.jdd-pin').first();
    if (await pin.getAttribute('aria-pressed') !== 'true') await pin.click();
    expect(await cards.evaluateAll(nodes => nodes.map(n => n.getAttribute('data-delivery-select')))).toEqual(before);
    await cards.first().click();
    await expect(page.locator('.jdd-detail h3')).toBeFocused();
    await expect(page.getByRole('button', { name: '完整任务与证据' })).toBeInViewport();
    await contrast(page, '.jdd');
    await page.screenshot({ path: info.outputPath(`v6-delivery-${mode}.png`), fullPage: true });
    await page.getByRole('button', { name: '完整任务与证据' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(1);
    await page.getByRole('button', { name: '返回成果桌', exact: true }).click();
    await expect(page.locator('.jdd-detail h3')).toBeFocused();
    await page.getByRole('button', { name: '返回成果集合' }).click();
    await expect(cards.first()).toBeFocused();
    await page.getByRole('button', { name: '重点 1', exact: true }).click(); await expect(cards).toHaveCount(1);
    await page.getByRole('button', { name: '本轮', exact: true }).click();
    await page.getByRole('button', { name: '列表浏览' }).click(); await contrast(page, '.jdd');
    await page.getByRole('button', { name: '卡片浏览' }).click();
    await page.keyboard.press('Escape'); await expect(page.getByRole('button', { name: /打开成果桌/ })).toBeFocused();
  }
  expect(errors).toEqual([]); await noOverflow(page);
});

test('V5 local draft viewer never uploads and releases blob URLs on removal', async ({ page }) => {
  await page.addInitScript(() => {
    const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
    (window as any).blobAudit = { made: [], revoked: [] };
    URL.createObjectURL = blob => { const url = create(blob); (window as any).blobAudit.made.push(url); return url; };
    URL.revokeObjectURL = url => { (window as any).blobAudit.revoked.push(url); revoke(url); };
  });
  await page.goto('/e2e/fixtures/pi-content-v5.html');
  const writes: string[] = []; page.on('request', req => { if (req.method() !== 'GET') writes.push(req.url()); });
  await page.locator('input[type=file]').setInputFiles({ name: 'draft.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jSf8AAAAASUVORK5CYII=', 'base64') });
  await page.getByRole('button', { name: '查看图片 draft.png' }).click();
  await expect(page.getByRole('dialog')).toContainText('尚未发送');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: /移除.*draft.png/ }).click();
  expect(await page.evaluate(() => (window as any).blobAudit.revoked.length)).toBe(1);
  expect(writes).toEqual([]);
});

test('production Room delivery desk returns to original task controls in one Dialog', async ({ page }) => {
  await page.goto('/e2e/fixtures/jev-execution.html?scene=final');
  const expand = page.getByRole('button', { name: '展开任务栏' }); if (await expand.isVisible()) await expand.click();
  await page.getByRole('button', { name: /打开成果桌/ }).first().click();
  await page.locator('[data-delivery-select]').first().click();
  await page.getByRole('button', { name: '完整任务与证据' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await expect(page.getByRole('button', { name: /打开 .* Session/ }).last()).toBeVisible();
  await page.getByRole('button', { name: '返回成果桌', exact: true }).click();
  await expect(page.locator('.jdd-detail h3')).toBeFocused();
  await page.keyboard.press('Escape'); await expect(page.getByRole('button', { name: /打开成果桌/ }).first()).toBeFocused();
});
