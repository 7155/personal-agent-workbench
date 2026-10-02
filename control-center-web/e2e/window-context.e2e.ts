import { expect, test, type Locator, type Page } from '@playwright/test';

async function expectWindowContext(shell: Locator, editor: Locator, page: Page) {
  const title = shell.locator('.paw-window-title > strong');
  await expect(title).toBeVisible();
  const name = (await title.textContent())?.trim();
  expect(name).toBeTruthy();
  await expect(title).toHaveAttribute('title', name!);
  expect(await shell.getAttribute('aria-label')).toContain(name);
  const header = shell.locator('.paw-window-titlebar');
  for (const button of await header.getByRole('button').all()) {
    await button.focus();
    const actionName = await button.getAttribute('aria-label') || await button.textContent();
    await expect.poll(() => button.evaluate(element => {
      const rect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      const headerRect = element.closest('.paw-window-titlebar')!.getBoundingClientRect();
      return { minimumSize: rect.width >= 24 && rect.height >= 24,
        insideHeader: rect.top >= headerRect.top && rect.bottom <= headerRect.bottom,
        pointerReachable: Boolean(hit === element || element.contains(hit)) };
    }), { message: `${actionName}: header action geometry and hit target` }).toEqual({ minimumSize: true, insideHeader: true, pointerReachable: true });
  }
  await expect(editor).toBeVisible();
  await editor.fill('窗口上下文可读性草稿');
  await expect(editor).toHaveValue('窗口上下文可读性草稿');
  expect(await editor.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const body = element.closest('.paw-window-body')!.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return rect.top >= body.top && rect.bottom <= body.bottom && Boolean(hit === element || element.contains(hit));
  })).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  return shell.evaluate(element => {
    const rect = (node: Element) => {
      const box = node.getBoundingClientRect();
      return { x: box.x, y: box.y, width: box.width, height: box.height };
    };
    const header = element.querySelector('.paw-window-titlebar')!;
    const caption = header.querySelector('.paw-window-title > strong')!;
    return { target: element.getAttribute('data-window-target'), viewport: { width: innerWidth, height: innerHeight },
      title: caption.textContent, titleRect: rect(caption), headerRect: rect(header),
      bodyInset: getComputedStyle(element.querySelector('.paw-window-body')!).paddingTop,
      buttons: Array.from(header.querySelectorAll('button')).filter(button => button.getBoundingClientRect().width > 0)
        .map(button => ({ name: button.getAttribute('aria-label') || button.textContent?.trim(), ...rect(button) })) };
  });
}

test('Room and Session retain visible context names without sacrificing actions or composers', async ({ page }, info) => {
  await page.goto('/?frontend=paw-os&controlTransport=mock#/rooms?room=room-preview');
  const room = page.locator('.paw-window-shell[data-window-target="room"]');
  const composer = room.getByRole('textbox', { name: '协作消息' });
  await expect(composer).toBeVisible({ timeout: 30_000 });
  const roomCollapsed = await expectWindowContext(room, composer, page);
  await page.screenshot({ path: info.outputPath('room-context.png') });
  const expand = room.getByRole('button', { name: '展开 Room 控件' });
  if (await expand.isVisible()) await expand.click();
  const roomExpanded = await expectWindowContext(room, composer, page);
  const mars = room.getByRole('region', { name: 'Mars 伙伴结果' });
  await mars.getByText('查看结果', { exact: true }).click();
  await mars.getByRole('button', { name: '打开 Mars Session', exact: true }).click();
  const session = page.locator('.paw-window-shell[data-window-target="session"]');
  const sessionEditor = session.getByRole('textbox', { name: '消息', exact: true });
  await expect(sessionEditor).toBeVisible({ timeout: 30_000 });
  const sessionCollapsed = await expectWindowContext(session, sessionEditor, page);
  await page.screenshot({ path: info.outputPath('session-context.png') });
  await session.getByRole('button', { name: '展开对话控件' }).click();
  const sessionExpanded = await expectWindowContext(session, sessionEditor, page);
  await info.attach('window-context-metrics', { body: JSON.stringify({ roomCollapsed, roomExpanded, sessionCollapsed, sessionExpanded }, null, 2), contentType: 'application/json' });
});
