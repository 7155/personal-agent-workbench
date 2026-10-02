import { expect, test } from '@playwright/test';
import { expectNoHorizontalPageOverflow, percentile, productRoute, settleAgentTimeline } from './helpers';

test('the product Session keeps rich content and typing responsive inside its PAWOS window', async ({ page }, info) => {
  await page.goto(productRoute('agent'));
  await expect(page.locator('.agent-turn').first()).toBeVisible();
  await settleAgentTimeline(page);
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  await page.evaluate(() => {
    const samples: number[] = [];
    Reflect.set(window, '__PAW_TYPING_PAINT__', samples);
    document.querySelector('textarea')!.addEventListener('input', () => {
      const started = performance.now();
      requestAnimationFrame(() => samples.push(performance.now() - started));
    });
  });
  await composer.pressSequentially('The canonical Session remains responsive while reading rich content.', { delay: 8 });
  await page.waitForTimeout(50);
  const samples = await page.evaluate(() => Reflect.get(window, '__PAW_TYPING_PAINT__') as number[]);
  expect(samples.length).toBeGreaterThan(20);
  expect(percentile(samples, .95)).toBeLessThan(80);
  await expectNoHorizontalPageOverflow(page);
  await info.attach('product-session.png', { body: await page.screenshot(), contentType: 'image/png' });
  await info.attach('product-typing.json', { body: JSON.stringify({ samples, p95: percentile(samples, .95) }), contentType: 'application/json' });
});

test('the product Room retains ordered conversation and independent partner controls', async ({ page }, info) => {
  await page.goto(productRoute('rooms'));
  const room = page.locator('.paw-room-workspace');
  await expect(room).toBeVisible();
  await expect(room.getByRole('textbox', { name: '协作消息' })).toBeVisible();
  await expect(room.locator('.paw-room-session-round').first()).toBeVisible();
  await expect(page.getByRole('button', { name: '展开 Room 控件' })).toBeVisible();
  await expectNoHorizontalPageOverflow(page);
  await info.attach('product-room.png', { body: await page.screenshot(), contentType: 'image/png' });
});
