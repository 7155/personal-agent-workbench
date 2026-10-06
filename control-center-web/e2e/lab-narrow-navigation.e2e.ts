import { expect, test } from '@playwright/test';

test('project chat and results switch without covering keyboard or pointer targets', async ({ page }, info) => {
  test.setTimeout(90_000);
  await page.goto('/?controlTransport=mock#/eval-lab');
  await page.getByRole('button', { name: '新建项目', exact: true }).click();
  await page.getByRole('textbox', { name: '描述你的项目' }).fill('公开演示：检查项目导航');
  await page.getByRole('button', { name: '创建并开始', exact: true }).click();
  const body = page.locator('.lab-project-body');
  await expect(body).toBeVisible({ timeout: 30_000 });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.lab-project-results')).toBeHidden();
  await page.getByRole('button', { name: '收起项目 Agent', exact: true }).click();
  for (const name of ['材料 0', '运行', '成果 0', '应用交付', '优化对比']) {
    await page.getByRole('button', { name, exact: true }).click();
    await expect(page.getByRole('button', { name, exact: true })).toHaveAttribute('aria-current', 'page');
  }
  await page.getByRole('button', { name: '材料 0', exact: true }).click();
  await page.getByRole('button', { name: '知识库实验', exact: true }).click();
  for (const name of ['资料', '索引与检索', '评测']) {
    await page.locator('nav[aria-label="知识库实验步骤"]').getByRole('button', { name, exact: true }).click();
    await expect(page.getByText('知识库实验数据未完整返回，请重新读取。')).toHaveCount(0);
  }
  await page.getByRole('combobox', { name: '更多项目视图' }).selectOption('workflow');
  const tools = page.locator('.lab-flow__canvas-tools');
  await tools.scrollIntoViewIfNeeded();
  const geometry = await page.evaluate(() => {
    const bar = document.querySelector('.lab-flow__canvas-tools')!.getBoundingClientRect();
    const empty = document.querySelector('.lab-flow__empty')!.getBoundingClientRect();
    return { bar: { left: bar.left, right: bar.right, top: bar.top }, emptyBottom: empty.bottom, width: innerWidth };
  });
  expect(geometry.bar.left).toBeGreaterThanOrEqual(0);
  expect(geometry.bar.right).toBeLessThanOrEqual(geometry.width);
  expect(geometry.bar.top).toBeGreaterThanOrEqual(geometry.emptyBottom);
  for (const name of ['适应窗口', '回到起点', '恢复默认缩放', '缩小画布', '放大画布']) {
    await page.getByRole('button', { name, exact: true }).click();
  }
  await info.attach('lab-workflow-controls-without-covered-content.png', { body: await page.screenshot(), contentType: 'image/png' });
  await page.getByRole('button', { name: '展开项目 Agent', exact: true }).click();
  await expect(page.getByRole('region', { name: '项目 Agent', exact: true })).toBeVisible();
  await expect(page.locator('.lab-project-results')).toBeHidden();
  await info.attach('lab-chat-without-covered-results.png', { body: await page.screenshot(), contentType: 'image/png' });
});
