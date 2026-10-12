import { expect, type Page } from '@playwright/test';
import { pawOsAppRegistry } from '../src/features/paw-os/model/app-registry';
import { routeRegistry } from '../src/app/route-registry';

// E2E route assertions consume the same registry as the shell so product copy
// changes cannot leave a second, stale navigation contract behind.
export const routes = routeRegistry.filter(route => route.surface !== 'standalone');

export function isMobileViewport(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) <= 760;
}

export function productRoute(routeId: string): string {
  const target = routeId === 'agent' ? '/agent?session=session-preview'
    : routeId === 'rooms' ? '/rooms?room=room-preview' : `/${routeId}`;
  return `/?controlTransport=mock#${target}`;
}

export function routeSurface(page: Page, routeId: string) {
  if (routeId === 'project-field') return page.locator('.paw-desktop-viewport');
  const app = pawOsAppRegistry.find(app => app.routeIds.some(id => id === routeId));
  if (!app) throw new Error(`No PAWOS owner for ${routeId}`);
  return page.locator(`.paw-window-shell[data-app="${app.id}"] .paw-window-body`);
}

export async function openRoute(page: Page, routeId: string): Promise<number> {
  const started = Date.now();
  await page.evaluate(hash => { location.hash = hash; }, productRoute(routeId).split('#')[1]);
  await expect(routeSurface(page, routeId)).toBeVisible();
  return Date.now() - started;
}

export async function expectNoHorizontalPageOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
}

export async function settleAgentTimeline(page: Page): Promise<void> {
  const scroller = page.locator('.paw-session-workspace [data-testid="virtuoso-scroller"]');
  await expect(scroller).toBeVisible();
  await scroller.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect.poll(() => scroller.evaluate(
    element => element.scrollHeight - element.clientHeight - element.scrollTop,
  )).toBeLessThanOrEqual(1);
  await page.waitForTimeout(50);
}

export function percentile(values: readonly number[], ratio: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * ratio) - 1)] ?? 0;
}

export async function coordinatorControls(page: Page) {
  const app=page.locator('.paw-window-shell[data-app="agent-controller"]');
  if (await app.getAttribute('data-active') !== 'true') {
    await page.getByRole('button', {name:'打开全部 App',exact:true}).click();
    await page.getByRole('dialog', {name:'全部 App'}).locator('button[data-app="agent-controller"]').click();
    await expect(app).toHaveAttribute('data-active', 'true');
  }
  await expect(app.getByRole('textbox',{name:'消息',exact:true})).toBeVisible();
  const rail=app.getByRole('complementary',{name:'Agent 控制的 Session 与 Room'});
  if(!await rail.isVisible()) await app.getByRole('button',{name:/^控制 /}).click();
  await expect(rail).toBeVisible();
  return rail;
}

/** Explicit current StarMate UI admission; it never sends the new task. */
export async function createControlledSession(page: Page, task: string) {
  const rail=await coordinatorControls(page);
  await rail.getByRole('button',{name:'Session',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'创建 Session'});
  await expect(dialog.getByRole('button',{name:'创建',exact:true})).toBeDisabled();
  await dialog.getByRole('textbox',{name:'新控制对象的任务'}).fill(task);
  await expect(dialog.getByText('创建后由你或 Agent 显式开始工作。',{exact:true})).toBeVisible();
  await dialog.getByRole('button',{name:'创建',exact:true}).click();
  await expect(dialog).toBeHidden();
  const target=rail.getByRole('button',{name:`打开 session ${task}`,exact:true});
  await expect(target).toBeVisible();
  return target;
}
