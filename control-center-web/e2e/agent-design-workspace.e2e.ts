import { expect, test } from '@playwright/test';
import { expectNoHorizontalPageOverflow } from './helpers';

test('conversation keeps project actions in files and returns keyboard focus from its tools', async ({ page }) => {
  await page.goto('/?controlTransport=mock#/agent?session=session-preview');
  const app = page.locator('.paw-window-shell[data-app="agent"]');
  const message = app.getByRole('textbox', { name: '消息', exact: true });
  await expect(message).toBeVisible({ timeout: 30_000 });
  await message.fill('保留这段尚未发送的文字');
  await expect(app.getByRole('region', { name: 'Session 对话' }).locator('.project-quick-actions')).toHaveCount(0);
  await app.getByRole('button', { name: '展开对话控件' }).click();
  await expect(page.getByRole('navigation', { name: '当前 Session 视图' })).toBeVisible();
  await expect(app.getByRole('button', { name: '打开对话文件' })).toHaveCount(0);
  const tools = app.getByRole('button', { name: '对话工具', exact: true });
  await tools.focus();
  await page.keyboard.press('ArrowDown');
  const menu = app.getByRole('menu', { name: '对话工具菜单' });
  await expect(menu.getByRole('menuitem', { name: '任务与状态' })).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(menu.getByRole('menuitem', { name: '文件', exact: true })).toBeFocused();
  await page.keyboard.press('Enter');
  const panel = app.getByLabel('对话工具侧栏', {exact:true});
  await expect(panel).toBeVisible();
  await expect(panel.getByRole('region', { name: '项目快速动作' })).toBeVisible();
  await expect(panel.getByRole('button', { name: '运行预览', exact: true })).toBeVisible();
  // Read both boxes in one frame while the drawer can still be entering.
  const geometry = await panel.evaluate(element => ({
    actions: element.querySelector('.project-quick-actions')!.getBoundingClientRect().toJSON(),
    files: element.querySelector('.agent-files-panel__body')!.getBoundingClientRect().toJSON(),
  }));
  expect(geometry.actions.height).toBeLessThan(160);
  expect(geometry.actions.y + geometry.actions.height).toBeLessThanOrEqual(geometry.files.y + 1);
  await panel.getByRole('button', { name: '运行预览', exact: true }).focus();
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
  await expect(tools).toBeFocused();
  await expect(message).toHaveValue('保留这段尚未发送的文字');
  await tools.click();
  await menu.getByRole('menuitem', { name: '文件', exact: true }).click();
  await panel.getByRole('button', { name: '收起文件目录' }).click();
  await expect(panel).toBeHidden();
  await expect(tools).toBeFocused();
  await expect(message).toHaveValue('保留这段尚未发送的文字');
  await expectNoHorizontalPageOverflow(page);
});


test('model and permission popovers track their trigger through viewport changes', async ({ page }) => {
  await page.goto('/?controlTransport=mock#/agent?session=session-preview');
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toBeVisible({timeout: 30_000});
  for (const name of [/^模型与推理：/, /^对话权限：/]) {
    const trigger = page.getByRole('button', { name });
    await trigger.click();
    const content = page.locator(name.source.includes('模型') ? '.agent-model-picker' : '.agent-picker-popover');
    for (const width of [768, 390, 1440]) {
      await page.setViewportSize({width, height: 900});
      await expect.poll(async () => {
        const anchor = await trigger.boundingBox(), popup = await content.boundingBox();
        return anchor && popup ? Math.abs(anchor.y - popup.y - popup.height - 6) : 999;
      }).toBeLessThan(2);
      const bounds = await content.boundingBox();
      expect(bounds!.x).toBeGreaterThanOrEqual(0);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    }
    await page.keyboard.press('Escape');
    await expect(trigger).toBeFocused();
  }
});


test('focused task criteria remains visible when the window becomes short', async ({ page }) => {
  await page.goto('/?controlTransport=mock#/agent');
  await expect(page.getByRole('button', {name: /打开对话/})).toBeEnabled({timeout:30_000});
  await page.getByRole('button', {name: '交给助手做', exact: true}).click();
  const criteria = page.getByRole('textbox', {name:'完成标准'});
  await criteria.fill('说明修改和验证结果');
  await page.setViewportSize({width:390, height:480});
  await expect.poll(() => criteria.evaluate(e => {
    const rect = e.getBoundingClientRect();
    return e.contains(document.elementFromPoint(rect.x + 20, rect.y + 10));
  })).toBe(true);
  await expect(criteria).toBeFocused();
  await expect(criteria).toHaveValue('说明修改和验证结果');
});


test('file browsing keeps focus visible and preserves its tree through resizing', async ({page}) => {
  await page.goto('/?controlTransport=mock#/agent?session=session-preview');
  const app = page.locator('.paw-window-shell[data-app="agent"]');
  const message = app.getByRole('textbox', {name:'消息', exact:true});
  await expect(message).toBeVisible({timeout:30_000});
  await message.fill('文件浏览期间保留的草稿');
  const tools = app.getByRole('button', {name:'对话工具', exact:true});
  await tools.click();
  await app.getByRole('menuitem', {name:'文件', exact:true}).click();
  const panel = app.getByLabel('对话工具侧栏', {exact:true});
  const close = panel.getByRole('button', {name:'收起文件目录'});
  await expect(close).toBeFocused();
  await page.setViewportSize({width:390,height:844});
  await expect(app.locator('.paw-session-workspace__primary')).toHaveAttribute('inert','');
  for(let i=0;i<12;i++){
    await page.keyboard.press('Tab');
    await expect.poll(()=>panel.evaluate(e=>e.contains(document.activeElement))).toBe(true);
  }
  const root = panel.getByRole('treeitem').first();
  if(await root.getAttribute('aria-expanded') === 'false') await root.click();
  await expect(panel.getByRole('treeitem', {name:'预览文件 README.md'})).toBeVisible();
  await page.setViewportSize({width:1440,height:900});
  await expect(app.locator('.paw-session-workspace__primary')).not.toHaveAttribute('inert','');
  await expect(panel.getByRole('treeitem', {name:'预览文件 README.md'})).toBeVisible();
  await message.focus();
  await expect(message).toBeFocused();
  await expect(message).toHaveValue('文件浏览期间保留的草稿');
  await close.click();
  await expect(tools).toBeFocused();
});
