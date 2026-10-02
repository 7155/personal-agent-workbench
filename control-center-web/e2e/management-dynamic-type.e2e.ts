import { expect, test } from '@playwright/test';
import { productRoute, routeSurface, routes } from './helpers';

const auditedRoutes = routes.filter(({ id }) => !['agent', 'rooms'].includes(id));

test('management pages do not vertically clip text at 200% text size', async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  test.skip(
    !['desktop-1280x820', 'mobile-390x844'].includes(testInfo.project.name),
    'one desktop and one mobile Dynamic Type stress pass are sufficient',
  );

  const failures: Record<string, string[]> = {};
  for (const route of auditedRoutes) {
    await page.goto(productRoute(route.id));
    const main = routeSurface(page, route.id);
    await expect(main).toBeVisible();

    const clipped = await main.evaluate((root) => {
      const visible = (element: HTMLElement) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none'
          && style.visibility !== 'hidden'
          && rect.width > 2
          && rect.height > 2
          && !element.closest('[aria-hidden="true"]')
          && !String(element.className).includes('sr-only');
      };

      for (const element of root.querySelectorAll<HTMLElement>('*')) {
        if (!visible(element)) continue;
        const ownsText = [...element.childNodes].some(
          (node) => node.nodeType === Node.TEXT_NODE && Boolean(node.textContent?.trim()),
        );
        if (!ownsText) continue;
        const fontSize = Number.parseFloat(getComputedStyle(element).fontSize);
        if (fontSize > 0 && fontSize < 40) {
          element.style.fontSize = `${fontSize * 2}px`;
          element.dataset.dynamicTypeStress = 'true';
        }
      }

      return [...root.querySelectorAll<HTMLElement>('[data-dynamic-type-stress="true"]')]
        .filter(visible)
        .filter((element) => {
          const style = getComputedStyle(element);
          const clipsVertically = /(hidden|clip)/u.test(style.overflowY)
            || /(hidden|clip)/u.test(style.overflow);
          return clipsVertically && element.scrollHeight > element.clientHeight + 1;
        })
        .map((element) => {
          const label = element.getAttribute('aria-label')
            || element.getAttribute('title')
            || element.textContent
            || element.tagName;
          return `${element.tagName.toLowerCase()}.${element.className || '<none>'}: ${label.trim().slice(0, 80)}`;
        })
        .slice(0, 20);
    });

    if (clipped.length > 0) failures[route.id] = clipped;
  }

  expect(failures).toEqual({});
});
