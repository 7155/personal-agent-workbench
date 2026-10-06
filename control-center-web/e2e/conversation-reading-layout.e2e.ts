import {expect,test} from '@playwright/test';

for (const width of [768,1440]) test(`citation actions and conversation navigation stay clear at ${width}px`, async ({page}) => {
  await page.setViewportSize({width,height:900});
  await page.goto('/?controlTransport=mock#/agent?session=session-preview');
  const app=page.locator('.paw-window-shell[data-app="agent"]');
  await expect(app.getByRole('textbox',{name:'消息',exact:true})).toBeVisible({timeout:30_000});
  const citation=app.getByRole('region',{name:'引用：控制中心迁移记录'});
  const link=citation.locator('a.agent-citation');
  const save=app.getByRole('button',{name:'保存 src/features/agent/state/live-store.ts',exact:true});
  await expect(save).toBeVisible();
  const geometry=await app.evaluate(element=>{
    const card=element.querySelector('.paw-rich-citation')!.getBoundingClientRect();
    const link=element.querySelector('.agent-citation')!.getBoundingClientRect();
    const icon=element.querySelector('.agent-citation > svg')!.getBoundingClientRect();
    const file=element.querySelector('.agent-file-block')!.getBoundingClientRect();
    const fileTail=element.querySelector('.agent-file-block__open')!.getBoundingClientRect();
    const code=element.querySelector('.paw-rich-code')!.getBoundingClientRect();
    const nav=element.querySelector('.agent-conversation-nav')!.getBoundingClientRect();
    const composer=element.querySelector('.paw-unified-composer')!.getBoundingClientRect();
    return {cardWidth:card.width,linkWidth:link.width,citationInset:card.right-icon.right,fileInset:file.right-fileTail.right,navGap:nav.left-code.right,leftAlignment:card.left-composer.left};
  });
  expect(geometry.cardWidth-geometry.linkWidth).toBeLessThanOrEqual(2);
  expect(Math.abs(geometry.citationInset-geometry.fileInset)).toBeLessThanOrEqual(2);
  expect(geometry.navGap).toBeGreaterThanOrEqual(12);
  if (width === 768) expect(Math.abs(geometry.leftAlignment)).toBeLessThanOrEqual(4);
  expect(await save.evaluate(element=>{
    const rect=element.getBoundingClientRect();
    return element.contains(document.elementFromPoint(rect.right-2,rect.top+2));
  })).toBe(true);
  const download=page.waitForEvent('download');
  await save.click();
  expect((await download).suggestedFilename()).toBe('live-store.ts');
  const nav=app.getByRole('navigation',{name:'快速跳转对话'});
  const second=nav.getByRole('button',{name:'跳到第 2 轮'});
  const bounds=await second.boundingBox();
  expect(bounds!.width).toBeGreaterThanOrEqual(32);
  expect(bounds!.height).toBeGreaterThanOrEqual(32);
  await nav.getByRole('button',{name:'跳到第 1 轮'}).click();
  await expect(app.locator('.agent-turn').first().locator('.paw-user-message')).toBeInViewport();
  await second.click();
  await expect(app.locator('.agent-turn').last().locator('.paw-user-message')).toBeInViewport();
  await link.locator(':scope > svg').click();
  await expect(page).toHaveURL(/#\/planning/);
});
