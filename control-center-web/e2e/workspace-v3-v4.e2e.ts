import { expect, test } from '@playwright/test';

// Real React components and Dialog, deliberately synthetic snapshots; no model calls.
test('V3 reading controls navigate actual headings and preserve display preferences', async ({ page }, info) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/e2e/fixtures/conversation-workspace-v3.html');
  await page.getByRole('button', { name: '章节', exact: true }).click();
  const outline = page.getByRole('navigation', { name: '本段章节导航' });
  await expect(outline.getByRole('button')).toHaveCount(5);
  await outline.getByRole('button', { name: /让不同内容各得其所/ }).click();
  await expect(page.getByRole('heading', { name: '2. 让不同内容各得其所', exact: true })).toBeFocused();
  await page.getByRole('button', { name: '阅读', exact: true }).click();
  await page.getByRole('button', { name: /大字/ }).click();
  await page.getByRole('button', { name: '紧凑', exact: true }).click();
  await expect(page.locator('.paw-reading-surface').first()).toHaveAttribute('data-reading-size', 'large');
  await expect(page.locator('.paw-reading-surface').first()).toHaveAttribute('data-reading-spacing', 'compact');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: '阅读', exact: true })).toBeFocused();
  await page.reload();
  await expect(page.locator('.paw-reading-surface').first()).toHaveAttribute('data-reading-size', 'large');
  await page.getByRole('button', { name: '深色', exact: true }).click();
  await page.screenshot({ path: info.outputPath('v3-reading-dark.png'), fullPage: true });
  expect(errors).toEqual([]);
  expect(await page.locator('body').evaluate(el => el.scrollWidth <= innerWidth + 1)).toBe(true);
});

test('V4 relationships, role views and detail return retain selection', async ({ page }, info) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/e2e/fixtures/jev-collaboration-v4.html');
  await expect(page.locator('.jcv-node')).toHaveCount(5);
  const node = page.locator('.jcv-node').filter({ hasText: '联调恢复入口与重复打开' });
  await node.click();
  const summary = page.getByRole('complementary', { name: '选中任务的关系与摘要' });
  await expect(summary).toContainText('前置与参考');
  await page.getByRole('button', { name: '完整任务与操作' }).click();
  await expect(page.getByRole('heading', { name: '联调恢复入口与重复打开', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /返回协作全景/ }).click();
  await expect(node).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: '取消选中任务' }).click();
  await page.getByRole('tab', { name: '伙伴分工' }).click();
  await expect(page.getByRole('tabpanel')).toContainText('Venus');
  await page.getByRole('tab', { name: '交接与复核' }).click();
  await expect(page.getByRole('tabpanel')).toContainText('复核');
  await page.getByRole('tab', { name: '任务关系' }).click();
  await page.screenshot({ path: info.outputPath('v4-relations-light.png'), fullPage: true });
  expect(errors).toEqual([]);
  expect(await page.locator('body').evaluate(el => el.scrollWidth <= innerWidth + 1)).toBe(true);
});

test('V4 twelve scenes retain truthful terminal, offline and empty states', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto('/e2e/fixtures/jev-collaboration-v4.html');
  const selector = page.getByRole('combobox', { name: '切换协作样例' });
  const scenes = await selector.locator('option').evaluateAll(options => options.map(o => (o as HTMLOptionElement).value));
  expect(scenes).toHaveLength(12);
  for (const scene of scenes) {
    await selector.selectOption(scene);
    await expect(page.getByRole('tab', { name: '任务关系' })).toHaveAttribute('aria-selected', 'true');
    if (['history', 'disconnected', 'stopped', 'delivered'].includes(scene)) {
      await expect(page.locator('.jcv-node[data-running="true"]')).toHaveCount(0);
    }
    if (scene === 'empty') await expect(page.locator('.jcv-node')).toHaveCount(0);
    if (scene === 'plan') await expect(page.locator('.jcv-node').first()).toContainText('未执行');
    expect(await page.locator('body').evaluate(el => el.scrollWidth <= innerWidth + 1)).toBe(true);
  }
  expect(errors).toEqual([]);
});

test('production Room host reuses one Dialog for panorama and original task operations', async ({ page }) => {
  await page.goto('/e2e/fixtures/jev-execution.html?scene=parallel');
  const expand = page.getByRole('button', { name: '展开任务栏' });
  if (await expand.isVisible()) await expand.click();
  await page.getByRole('button', { name: '打开多 Agent 协作全景' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await page.getByRole('dialog').locator('.jcv-node').first().click();
  await page.getByRole('button', { name: '完整任务与操作' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await expect(page.getByRole('button', { name: /打开 .* Session/ }).last()).toBeVisible();
  await page.getByRole('button', { name: '返回协作全景', exact: true }).click();
  await expect(page.getByRole('dialog').locator('.jcv-node[aria-pressed="true"]')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '打开多 Agent 协作全景' })).toBeFocused();
});
