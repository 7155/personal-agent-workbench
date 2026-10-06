import { expect, test } from '@playwright/test';

test('extension start content and send target fit the actual app window in every mode', async ({ page }, info) => {
  test.setTimeout(90_000);
  await page.goto('/?controlTransport=mock#/extensions/zhanggui-wenshu');
  await expect(page.locator('.zhanggui-app__modes')).toBeVisible({ timeout: 30_000 });
  for (const name of ['问数', '对账', '解释']) {
    await page.getByRole('tab', { name, exact: true }).click();
    await expect(page.getByRole('tab', { name, exact: true })).toHaveAttribute('aria-selected', 'true');
    const geometry = await page.locator('.zhanggui-app').evaluate((root) => {
      const window = root.getBoundingClientRect();
      return {
        left: window.left, right: window.right,
        rows: [...root.querySelectorAll('.zhanggui-app__start > *, .zhanggui-app__start textarea, .zhanggui-app__start form > button')].map((element) => {
          const rect = element.getBoundingClientRect();
          return { left: rect.left, right: rect.right };
        }),
        startOverflow: root.querySelector('.zhanggui-app__start')!.scrollWidth - root.querySelector('.zhanggui-app__start')!.clientWidth,
      };
    });
    expect(geometry.startOverflow).toBeLessThanOrEqual(1);
    for (const rect of geometry.rows) {
      expect(rect.left).toBeGreaterThanOrEqual(geometry.left - 1);
      expect(rect.right).toBeLessThanOrEqual(geometry.right + 1);
    }
    const suggestion = page.locator('.zhanggui-app__suggestions button').first();
    const text = await suggestion.innerText();
    await suggestion.click();
    const input = page.getByRole('textbox', { name: `${name}问题` });
    await expect(input).toHaveValue(text);
    const send = page.getByRole('button', { name: '发送', exact: true });
    await send.scrollIntoViewIfNeeded();
    await send.focus();
    await expect(send).toBeFocused();
    await info.attach(`zhanggui-${name}-window.png`, { body: await page.screenshot(), contentType: 'image/png' });
    await input.fill('');
  }
});
