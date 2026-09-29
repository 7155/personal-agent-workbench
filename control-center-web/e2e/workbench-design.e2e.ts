import { expect, test } from '@playwright/test';

test('Jev task rail is interactive in the React fixture at desktop and mobile sizes', async ({ page }) => {
  await page.goto('/e2e/fixtures/jev-execution.html?scene=parallel');
  const expand = page.getByRole('button', { name: '展开任务栏' });
  if (await expand.isVisible()) await expand.click();
  const tasks = page.getByRole('tab', { name: /任务/ });
  const files = page.getByRole('tab', { name: /成果/ });
  await expect(tasks).toHaveAttribute('aria-selected', 'true');
  await files.click();
  await expect(files).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('还没有交付文件')).toBeVisible();
  await tasks.click();
  await tasks.focus();
  await page.keyboard.press('ArrowRight');
  await expect(files).toBeFocused();
  await page.getByRole('button', { name: '深色主题' }).click();
  await expect(files).toBeVisible();
  expect(await page.locator('body').evaluate(el => el.scrollWidth <= window.innerWidth + 1)).toBe(true);
});

test('approval scene renders an actual plan without starting workers', async ({ page }) => {
  await page.goto('/e2e/fixtures/jev-execution.html?scene=approval');
  await expect(page.getByRole('region', { name: '整体执行方案' })).toBeVisible();
  await expect(page.getByRole('button', { name: '开始执行' })).toBeEnabled();
  await expect(page.getByRole('region', { name: '任务执行顺序' })).toContainText('可以先开始');
});
