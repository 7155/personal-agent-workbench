import { expect, test } from '@playwright/test';
import { productRoute } from './helpers';

test('Session changes reasoning and publishes one optimistic turn through the canonical workspace', async ({ page }) => {
  await page.goto(productRoute('agent'));
  const button = page.getByRole('button', { name: /^模型与推理：/ });
  await button.click();
  const picker = page.getByRole('dialog', { name: '选择模型与推理强度' });
  await picker.getByRole('radiogroup', { name: '推理强度' }).getByRole('radio', { name: '高', exact: true }).click();
  await expect(picker).toBeHidden();
  await expect(button).toHaveAccessibleName(/· 高$/);
  const probe = `Single Session send ${Date.now()}`;
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  await composer.fill(probe);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(composer).toHaveValue('');
  await expect(page.locator('.paw-user-message', { hasText: probe })).toHaveCount(1);
});

test('Room preserves its draft across another App and publishes it once', async ({ page }) => {
  await page.goto(productRoute('rooms'));
  const composer = page.getByRole('textbox', { name: '协作消息', exact: true });
  const probe = `Room draft survives ${Date.now()}`;
  await composer.fill(probe);
  await page.evaluate(() => { location.hash = '#/memory'; });
  await expect(page.locator('.paw-window-shell[data-app="memory"]')).toBeVisible();
  await page.evaluate(() => { location.hash = '#/rooms?room=room-preview'; });
  await expect(composer).toHaveValue(probe);
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(composer).toHaveValue('');
  await expect(page.locator('.paw-room-session-round__objective', { hasText: probe })).toHaveCount(1);
});
