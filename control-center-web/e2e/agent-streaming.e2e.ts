import { expect, test } from '@playwright/test';
import { expectNoHorizontalPageOverflow, percentile } from './helpers';

test.beforeEach(async ({ page }) => {
  await page.goto('/e2e/fixtures/session-load.html');
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toBeVisible();
});

test('the actual Session workspace virtualizes 1000 messages and keeps long Markdown readable', async ({ page }, info) => {
  await expect(page.getByRole('heading', { name: '长 Markdown', exact: true })).toBeVisible();
  const turns = page.locator('.agent-turn');
  expect(await turns.count()).toBeGreaterThan(0);
  expect(await turns.count()).toBeLessThan(80);
  await expectNoHorizontalPageOverflow(page);
  await info.attach('real-session-history.png', { body: await page.screenshot(), contentType: 'image/png' });
});

test('real Session deltas batch without blocking typing or creating duplicate subscriptions', async ({ page }, info) => {
  await expect(page.getByRole('heading', { name: '长 Markdown', exact: true })).toBeVisible();
  await page.evaluate(() => Reflect.get(window, '__PAW_SESSION_LOAD__').startStream());
  await page.getByRole('textbox', { name: '消息', exact: true }).pressSequentially('streaming keeps the real production composer responsive', { delay: 8 });
  await page.waitForFunction(() => Reflect.get(window, '__PAW_SESSION_LOAD__').metrics.complete);
  const metrics = await page.evaluate(() => Reflect.get(window, '__PAW_SESSION_LOAD__').metrics as {
    events: number; commits: number; longTasks: number[]; typing: number[];
  });
  await info.attach('real-session-streaming.json', { body: JSON.stringify(metrics, null, 2), contentType: 'application/json' });
  await info.attach('real-session-streaming.png', { body: await page.screenshot(), contentType: 'image/png' });
  expect(metrics.events).toBe(200);
  expect(metrics.commits).toBeGreaterThan(1);
  expect(metrics.commits).toBeLessThan(150);
  expect(metrics.typing.length).toBeGreaterThan(20);
  expect(percentile(metrics.typing, .95)).toBeLessThan(80);
  await expect(page.locator('.agent-turn', { hasText: 'Δ200' })).toBeVisible();
  expect(await page.evaluate(() => Reflect.get(window, '__PAW_SESSION_LOAD__').subscriptions())).toBe(1);
});
