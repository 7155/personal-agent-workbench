import { expect, test } from '@playwright/test';
import { expectNoHorizontalPageOverflow } from './helpers';

test('typed Rich Blocks stay safe, compact, and readable at 320px', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await page.goto('/e2e/fixtures/rich-blocks.html');

  await expect(page.getByRole('region', { name: 'Room 重构验收' })).toContainText('结构化内容与文字分开持久化');
  await expect(page.getByRole('link', { name: /证据/ })).toHaveAttribute('href', 'https://example.com/evidence');
  await expect(page.locator('#root img[src="x"], #root script')).toHaveCount(0);
  await expect(page.getByText('原文永久保留').locator('xpath=ancestor::a')).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Context Cleaner' })).toContainText('digest');
  await expect(page.getByText(/暂不支持的内容 · future_chart/)).toBeVisible();

  const checklist = page.locator('details.agent-rich-checklist');
  const code = page.locator('figure.paw-rich-code');
  await expect(checklist).not.toHaveAttribute('open');
  const expandCode = code.getByRole('button', { name: /展开阅读区/ });
  await expect(expandCode).toHaveAttribute('aria-expanded', 'false');
  await expandCode.click();
  await expect(code).toHaveAttribute('data-expanded', 'true');
  await code.getByRole('button', { name: /收起阅读区/ }).click();
  await expect(code.getByRole('button', { name: /展开阅读区/ })).toHaveAttribute('aria-expanded', 'false');
  await checklist.locator('summary').click();
  await expect(checklist).toHaveAttribute('open', '');
  await expect(page.getByRole('table')).toContainText('Room Post');
  await expectNoHorizontalPageOverflow(page);

  await page.screenshot({ path: test.info().outputPath('rich-blocks-320.png'), fullPage: true });
});
