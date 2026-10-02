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
