import { expect, test } from '@playwright/test';

test('Session model catalog remains clickable when opened from the bottom composer', async ({ page }, info) => {
  await page.goto('/?frontend=paw-os&controlTransport=mock#/agent?session=session-preview');
  await page.getByRole('button', { name: /^模型与推理：/ }).click();
  const picker = page.getByRole('dialog', { name: '选择模型与推理强度' });
  await picker.getByRole('button', { name: /^更换模型/ }).click();
  await expect(picker.getByRole('listbox', { name: '可用模型' })).toBeVisible();
  const listHeight = await picker.getByRole('listbox').evaluate(element => element.getBoundingClientRect().height);
  expect(listHeight).toBeGreaterThan(100);
  const option = picker.getByRole('option', { selected: false }).first();
  const name = await option.getAttribute('aria-label');
  await option.click();
  await expect(picker).toBeHidden();
  await expect(page.getByRole('button', { name: /^模型与推理：/ })).toContainText(name!.replace('选择模型 ', ''));
  await page.screenshot({ path: info.outputPath('session-model-picker.png') });
});
