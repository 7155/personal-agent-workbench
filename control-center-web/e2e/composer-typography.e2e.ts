import { expect, test } from '@playwright/test';

test('home, conversation and Room keep the same Chinese input typography', async ({page}) => {
  const styles: unknown[] = [];
  for (const route of ['/agent', '/agent?session=session-preview', '/rooms?room=room-preview']) {
    await page.goto('/?controlTransport=mock#' + route);
    const composer = page.locator('[data-composer-design="workbench"]').first();
    const input = composer.locator('textarea');
    await expect(input).toBeVisible({timeout:30_000});
    const read = (element: HTMLElement, pseudo?: string) => {
      const style = getComputedStyle(element, pseudo);
      return {family:style.fontFamily, size:style.fontSize, weight:style.fontWeight, lineHeight:style.lineHeight};
    };
    const placeholderStyle = await input.evaluate(element => {
      const style = getComputedStyle(element, '::placeholder');
      return {family:style.fontFamily, size:style.fontSize, weight:style.fontWeight, lineHeight:style.lineHeight};
    });
    await input.fill('检查字体：持续对话，保留中文标点。PAW 123\n第二行：任务、资料、权限与模型。');
    const inputStyle = await input.evaluate(read);
    expect(placeholderStyle).toEqual(inputStyle);
    styles.push(inputStyle);
    await expect(input).toHaveValue('检查字体：持续对话，保留中文标点。PAW 123\n第二行：任务、资料、权限与模型。');
    const model = composer.getByRole('button', {name:/^模型与推理：/});
    if (await model.count() && await composer.evaluate(element=>element.clientWidth > 460)) {
      const parts = await model.evaluate(element => {
        const text = element.querySelector<HTMLElement>('.agent-composer__picker-text')!;
        const thinking = element.querySelector<HTMLElement>('.agent-composer__picker-thinking')!;
        const caret = element.querySelector<HTMLElement>('.caret')!;
        const label = element.querySelector<HTMLElement>('.ui-button__label')!;
        return {
          textWeight:getComputedStyle(text).fontWeight, thinkingWeight:getComputedStyle(thinking).fontWeight,
          textSize:getComputedStyle(text).fontSize, thinkingSize:getComputedStyle(thinking).fontSize,
          thinkingGap:thinking.getBoundingClientRect().left-text.getBoundingClientRect().right,
          caretGap:caret.getBoundingClientRect().left-thinking.getBoundingClientRect().right,
          caretRight:caret.getBoundingClientRect().right, labelRight:label.getBoundingClientRect().right,
        };
      });
      expect(parts.thinkingWeight).toBe(parts.textWeight);
      expect(parts.thinkingSize).toBe(parts.textSize);
      expect(parts.thinkingGap).toBeGreaterThanOrEqual(5);
      expect(parts.caretGap).toBeGreaterThanOrEqual(5);
      expect(parts.caretRight).toBeLessThanOrEqual(parts.labelRight+1);
    }
  }
  expect(styles[1]).toEqual(styles[0]);
  expect(styles[2]).toEqual(styles[0]);
});
