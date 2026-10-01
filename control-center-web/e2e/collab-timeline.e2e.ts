import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

for (const view of ['room', 'session']) {
  test(`${view}: themed timeline, bounded scrolling and keyboard-safe shared dialog`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`/e2e/fixtures/collab-timeline.html?view=${view}`);
    const stage = page.getByRole('region', { name: '多 Agent 协作时间线' });
    await expect(stage).toBeVisible();
    await expect(stage.getByRole('slider', { name: '回放进度' })).toHaveValue('1000');
    for (const theme of ['light', 'dark']) {
      if (theme === 'dark') await page.getByRole('button', { name: '切换明暗主题' }).click();
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      const audit = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
      expect(audit.violations.map(item => ({ id: item.id, nodes: item.nodes.map(node => node.target) }))).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`${view}-${theme}.png`), fullPage: true });
    }
    const trigger = page.getByRole('button', { name: view === 'session' ? '打开卫星协作时间线' : '打开协作全景' });
    await trigger.click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('slider', { name: '回放进度' })).toHaveValue('1000');
    await dialog.getByRole('slider', { name: '回放进度' }).fill('450');
    await expect(dialog.getByRole('slider', { name: '回放进度' })).toHaveValue('450');
    await dialog.getByRole('button', { name: '关闭', exact: true }).focus();
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(errors).toEqual([]);
  });
}

test('Agent app opens from PAWOS and keeps its searchable work-record entry usable', async ({ page }, testInfo) => {
  await page.goto('/?frontend=paw-os&controlTransport=mock#/project-field');
  await page.getByRole('button', { name: '全部 App', exact: true }).click();
  await page.getByRole('dialog', { name: '全部 App' }).locator('button[data-app="agent"]').click();
  const app = page.getByRole('region', { name: 'Agent 工作台', exact: true });
  await expect(app).toBeVisible();
  // Window chrome is portalled beside the App body by PAWOS.
  const openRecords = page.locator('.paw-window-shell[data-app="agent"]').getByRole('button', { name: '打开工作记录', exact: true });
  if (await openRecords.isVisible()) await openRecords.click();
  const search = app.getByRole('textbox', { name: '搜索 Session 与 Room' });
  await expect(search).toBeVisible();
  await search.fill('timeline-check-no-match');
  await expect(search).toHaveValue('timeline-check-no-match');
  await search.fill('');
  await expect(app.locator('.paw-agent-recents')).not.toHaveAttribute('aria-busy', 'true');
  await expect(app.getByRole('button', { name: '新建工作', exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('agent-app.png') });
});

test('Room playback keeps its layout stable and paused handoffs do not run a second clock', async ({ page }) => {
  await page.goto('/e2e/fixtures/collab-timeline.html');
  const slider = page.getByRole('slider', { name: '回放进度' });
  await slider.fill('15');
  const packet = page.locator('.ctl-handoff__packet-core').first();
  await expect(packet).toBeAttached();
  const before = await packet.boundingBox();
  await page.waitForTimeout(800);
  const after = await packet.boundingBox();
  expect(after!.x).toBeCloseTo(before!.x, 1);
  expect(after!.y).toBeCloseTo(before!.y, 1);
  await slider.fill('1000');
  await page.getByRole('button', { name: '从头回放', exact: true }).click();
  const observed = await page.evaluate(async () => {
    const selectors = ['.ctl', '.ctl-stage', '.ctl-controls'];
    const initial = selectors.map(s => document.querySelector(s)!.getBoundingClientRect());
    let layoutShift = 0;
    let feedRebound = 0;
    const end = performance.now() + 17_000;
    while (performance.now() < end) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      selectors.forEach((s, index) => {
        const rect = document.querySelector(s)!.getBoundingClientRect();
        layoutShift = Math.max(layoutShift, Math.abs(rect.y - initial[index].y), Math.abs(rect.height - initial[index].height));
      });
      document.querySelectorAll('.ctl-feed li').forEach(item => {
        const transform = getComputedStyle(item).transform;
        if (transform !== 'none') feedRebound = Math.max(feedRebound, Math.abs(new DOMMatrixReadOnly(transform).m42));
      });
    }
    return { layoutShift, feedRebound };
  });
  expect(observed.layoutShift).toBeLessThan(1);
  expect(observed.feedRebound).toBeLessThan(1);
  await expect(slider).toHaveValue('1000');
});

test('live pulse changes emphasis without moving the timeline geometry', async ({ page }) => {
  await page.goto('/e2e/fixtures/collab-timeline.html?view=session');
  const marker = page.locator('.ctl-peek__now');
  await expect(marker).toBeAttached();
  const movement = await marker.evaluate(async node => {
    const positions: number[] = [];
    for (let frame = 0; frame < 100; frame += 1) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      positions.push(node.getBoundingClientRect().x);
    }
    return Math.max(...positions) - Math.min(...positions);
  });
  expect(movement, 'a live pulse must not scale the SVG coordinate system').toBeLessThan(1);
});

test('reduced motion disables autoplay but keeps scrubbing and truthful live status', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/e2e/fixtures/collab-timeline.html?cut=35');
  const stage = page.getByRole('region', { name: '多 Agent 协作时间线' });
  await expect(stage).toHaveAttribute('data-motion', 'off');
  await expect(stage.getByRole('button', { name: '从头回放' })).toBeDisabled();
  await expect(stage).toContainText('实时 · 跟随最新回执');
  await stage.getByRole('slider', { name: '回放进度' }).fill('500');
  await expect(stage.getByRole('slider', { name: '回放进度' })).toHaveValue('500');
  await expect(stage).toContainText('回放 ·');
  expect(await page.locator('.ctl-avatar__ring').first().evaluate(node => getComputedStyle(node).animationName)).toBe('none');
});
