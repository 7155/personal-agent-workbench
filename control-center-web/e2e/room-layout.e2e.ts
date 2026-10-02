import { expect, test } from '@playwright/test';

test('Room keeps its conversation and composer usable with collapsed and expanded controls', async ({ page }, info) => {
  await page.goto('/?frontend=paw-os&controlTransport=mock#/rooms?room=room-preview');
  const workspace = page.locator('.paw-room-workspace');
  const composer = workspace.getByRole('textbox', { name: '协作消息' });
  await expect(composer).toBeVisible({ timeout: 30_000 });
  const states = [false, true, false];
  for (const [index, expanded] of states.entries()) {
    await expect(workspace).toHaveAttribute('data-controls-expanded', String(expanded));
    const geometry = await workspace.evaluate(element => {
      const body = element.querySelector('.paw-room-workspace__body')!;
      const editor = element.querySelector('textarea')!;
      const bodyRect = body.getBoundingClientRect();
      const editorRect = editor.getBoundingClientRect();
      return {
        bodyHeight: bodyRect.height,
        editorInsideBody: editorRect.top >= bodyRect.top && editorRect.bottom <= bodyRect.bottom,
        editorReceivesPointer: editor.contains(document.elementFromPoint(
          editorRect.left + editorRect.width / 2, editorRect.top + editorRect.height / 2,
        )),
      };
    });
    expect(geometry.bodyHeight).toBeGreaterThan(200);
    expect(geometry.editorInsideBody).toBe(true);
    expect(geometry.editorReceivesPointer).toBe(true);
    if (index < states.length - 1) {
      await page.getByRole('button', { name: expanded ? '收起 Room 控件' : '展开 Room 控件' }).click();
    }
  }
  // Click the actual send surface: a nonzero DOM box alone misses clipping.
  await composer.fill('Room layout acceptance');
  await workspace.locator('.room-composer__send').click();
  await expect(composer).toHaveValue('');
  await page.screenshot({ path: info.outputPath('room-composer.png') });
});

test('Room titlebar keeps every expanded view control reachable in a narrow window', async ({ page }) => {
  await page.goto('/?frontend=paw-os&controlTransport=mock#/rooms?room=room-preview');
  await expect(page.getByRole('textbox', { name: '协作消息' })).toBeVisible({ timeout: 30_000 });
  const expand = page.getByRole('button', { name: '展开 Room 控件' });
  if (await expand.isVisible()) await expand.click();
  const chrome = page.getByLabel('Room 窗口控制');
  const buttons = chrome.getByRole('button');
  for (const button of await buttons.all()) {
    await button.focus();
    const name = await button.getAttribute('aria-label');
    await expect.poll(() => button.evaluate(element => {
      const rect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      const header = element.closest('.paw-window-titlebar')!.getBoundingClientRect();
      return { widthOk: rect.width >= 24, heightOk: rect.height >= 24,
        reachable: hit === element || element.contains(hit),
        insideHeader: rect.top >= header.top && rect.bottom <= header.bottom };
    }), { message: `Titlebar action remains usable: ${name}` }).toEqual({
      widthOk: true, heightOk: true, reachable: true, insideHeader: true,
    });
  }
  await chrome.getByRole('button', { name: '完整记录', exact: true }).click();
  await expect(chrome.getByRole('button', { name: '完整记录', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await chrome.getByRole('button', { name: '对话与结果', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Room 行星任务表' })).toBeVisible();
});
