import { expect, test } from '@playwright/test';

for (const frontend of ['paw-os']) {
  test(`Memory lifecycle labels remain readable in ${frontend}`, async ({ page }, testInfo) => {
    await page.goto(`/?frontend=${frontend}&controlTransport=mock#/memory?view=organize`);
    const section = page.locator('.mgmt-section').filter({
      has: page.getByRole('heading', { name: '记忆生命周期', exact: true }),
    });
    await expect(section).toBeVisible();
    await expect(section.locator('.mgmt-metric')).toHaveCount(4);

    const figures = await section.locator('.mgmt-metric').evaluateAll((metrics) => metrics.map((metric) => {
      const label = metric.querySelector('dt')!;
      const range = document.createRange();
      range.selectNodeContents(label);
      const text = range.getBoundingClientRect();
      const value = metric.querySelector('dd')!.getBoundingClientRect();
      const detail = metric.querySelector('.mgmt-metric__detail')!.getBoundingClientRect();
      return {
        label: label.textContent,
        labelHeight: text.height,
        lineHeight: Number.parseFloat(getComputedStyle(label).lineHeight),
        leftOffset: Math.abs(text.left - value.left),
        valueBelowLabel: value.top >= text.bottom - 1,
        detailBelowValue: detail.top >= value.bottom - 1,
      };
    }));
    for (const figure of figures) {
      expect(figure.labelHeight, `${figure.label} wraps into a narrow icon column`).toBeLessThanOrEqual(figure.lineHeight * 2);
      expect(figure.leftOffset, `${figure.label} aligns with its value`).toBeLessThanOrEqual(1);
      expect(figure.valueBelowLabel).toBe(true);
      expect(figure.detailBelowValue).toBe(true);
    }
    await testInfo.attach(`memory-lifecycle-${frontend}.png`, {
      body: await section.screenshot({ animations: 'disabled' }),
      contentType: 'image/png',
    });
  });
}

test('desktop empty-state guidance is visible when the surrounding pane first appears', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-1440x900', 'desktop split-pane contract');

  await page.goto('/?controlTransport=mock#/plugins');

  const detailPane = page.getByLabel('能力详情占位');
  const guidance = detailPane.getByText('选择一项能力查看详情');
  await expect(detailPane).toBeVisible();
  await expect(guidance).toBeVisible();

  const paneBox = await detailPane.boundingBox();
  const guidanceBox = await guidance.boundingBox();
  expect(paneBox).not.toBeNull();
  expect(guidanceBox).not.toBeNull();
  expect(guidanceBox!.y).toBeGreaterThanOrEqual(paneBox!.y);
  expect(guidanceBox!.y + guidanceBox!.height).toBeLessThanOrEqual(900);
});
