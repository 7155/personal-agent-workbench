import { expect, test, type Page } from '@playwright/test';
import { openRoute } from './helpers';

test('system Reduce Motion clamps route and indefinite animation', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/#/overview');
  await expect(page.locator('html')).toHaveAttribute('data-reduce-motion', 'true');

  await openAgentRoute(page);
  const motion = await page.locator('main[data-route-id="agent"]').evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      animationDuration: durationToMilliseconds(style.animationDuration),
      animationIterations: style.animationIterationCount,
      transitionDuration: durationToMilliseconds(style.transitionDuration),
    };

    function durationToMilliseconds(value: string): number {
      return Math.max(...value.split(',').map((item) => {
        const duration = item.trim();
        return duration.endsWith('ms')
          ? Number.parseFloat(duration)
          : Number.parseFloat(duration) * 1_000;
      }));
    }
  });

  expect(motion.animationDuration).toBeLessThanOrEqual(1);
  // The comfort contract retains a brief 120 ms feedback transition while
  // removing repeated animation; it does not make every state change instant.
  expect(motion.transitionDuration).toBeLessThanOrEqual(120);
  expect(motion.animationIterations).not.toContain('infinite');

  const indefiniteAnimations = await page.evaluate(() =>
    [...document.querySelectorAll('*')].filter((element) => {
      const style = getComputedStyle(element);
      return style.animationName !== 'none' && style.animationIterationCount.includes('infinite');
    }).length,
  );
  expect(indefiniteAnimations).toBe(0);
});

test('explicit reduced preference wins over system full motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.addInitScript(() => {
    window.localStorage.setItem('rag-ime-control-motion', 'reduce');
  });
  await page.goto('/#/overview');

  await expect(page.locator('html')).toHaveAttribute('data-reduce-motion', 'true');
});

async function openAgentRoute(page: Page): Promise<void> {
  const mobileNavigation = page.getByRole('navigation', { name: '快捷导航' });
  if (await mobileNavigation.isVisible()) {
    await mobileNavigation.getByRole('link', { name: '对话', exact: true }).click();
    await expect(page.locator('main[data-route-id="agent"]')).toBeVisible();
    return;
  }
  await openRoute(page, 'agent');
}
