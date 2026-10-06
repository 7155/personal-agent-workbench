import { expect, test } from '@playwright/test';

test('memory content classes remain clickable beside curation controls in narrow windows', async ({ page }, info) => {
  await page.goto('/?controlTransport=mock#/memory');
  const navigation = page.getByRole('region', { name: '记忆库分类与状态' });
  await expect(navigation).toBeVisible({ timeout: 30_000 });
  const layers = page.getByRole('list', { name: '记忆内容分类' });
  for (const name of [/^记忆 ·/, /^来源 ·/, /^主题 ·/]) {
    const button = layers.getByRole('button', { name });
    await button.click();
    await expect(button).toHaveAttribute('aria-current', 'page');
  }
  await page.getByRole('button', { name: '整理状态摘要', exact: true }).click();
  await expect(page.locator('#memory-library-status')).toBeVisible();
  await info.attach('memory-classes-and-curation.png', { body: await page.screenshot(), contentType: 'image/png' });
});
