import { expect, test } from '@playwright/test';
import { expectNoHorizontalPageOverflow } from './helpers';

for (const width of [390,1440]) {
  test(`goal cancellation keeps the desktop viewport and window fixed at ${width}px`, async ({page}) => {
    await page.setViewportSize({width,height:900});
    await page.goto('/?controlTransport=mock#/agent?session=session-preview');
    await expect(page.getByRole('textbox', {name:'消息',exact:true})).toBeVisible({timeout:30_000});
    const window = page.locator('.paw-window-shell[data-app="agent"]');
    const before = await window.boundingBox();
    await page.getByRole('navigation', {name:'当前工作内容'}).getByRole('button', {name:'任务',exact:true}).click();
    const panel = page.getByLabel('对话工具侧栏', {exact:true});
    const goal = panel.getByRole('region', {name:'长期目标',exact:true});
    await goal.getByRole('button', {name:'取消目标',exact:true}).click();
    const reason = goal.getByRole('textbox', {name:'目标取消原因'});
    await reason.fill('保留已完成的记录');
    const confirm = goal.getByRole('button', {name:'确认取消目标',exact:true});
    const editor = await reason.boundingBox(), button = await confirm.boundingBox();
    expect(editor!.y).toBeGreaterThanOrEqual(0);
    expect(button!.y + button!.height).toBeLessThanOrEqual(900);
    await confirm.click();
    await expect(goal.getByText('取消记录', {exact:true})).toBeVisible();
    const after = await window.boundingBox();
    expect(after!.x).toBeCloseTo(before!.x, 1);
    expect(after!.y).toBeCloseTo(before!.y, 1);
    await expect.poll(() => page.locator('.paw-desktop-viewport').evaluate(e=>[e.scrollLeft,e.scrollTop])).toEqual([0,0]);
  });
}

test('finished demo task exposes missing completion evidence and keeps criteria reachable', async ({page}) => {
  await page.goto('/?controlTransport=mock#/agent');
  await expect(page.getByRole('button', {name:/进入对话/})).toBeEnabled({timeout:30_000});
  await page.getByRole('button', {name:'交给助手做',exact:true}).click();
  await page.getByRole('textbox', {name:'和我的助手聊聊'}).fill('检查完成依据的显示');
  await page.getByRole('textbox', {name:'完成标准'}).fill('说明真实验证的范围\n保留未完成部分');
  await page.getByRole('textbox', {name:'本次工作目录'}).fill('/work/demo');
  await page.getByRole('checkbox').check();
  await page.getByRole('button', {name:'授权并开始任务',exact:true}).click();
  await expect(page.locator('p').filter({hasText:/这是演示任务的结果：/})).toBeVisible();
  const trigger = page.getByRole('navigation', {name:'当前工作内容'}).getByRole('button', {name:'任务',exact:true});
  await trigger.click();
  const panel = page.getByLabel('对话工具侧栏', {exact:true});
  const goal = panel.getByRole('region', {name:'长期目标',exact:true});
  await expect(goal.getByText('尚无完成依据', {exact:true})).toBeVisible();
  await expect(goal.getByText('说明真实验证的范围\n保留未完成部分', {exact:true})).toHaveCount(0);
  const details = goal.locator('summary').filter({hasText:'完成标准与预算'});
  await details.focus();
  await page.keyboard.press('Enter');
  await expect(goal.getByText('说明真实验证的范围\n保留未完成部分', {exact:true})).toBeVisible();
  await expect(panel.getByText('目标已结束', {exact:true})).toBeVisible();
  await expect(panel.getByText('执行条件未满足', {exact:true})).toHaveCount(0);
  await panel.getByRole('button', {name:'收起任务中心',exact:true}).click();
  await expect(trigger).toBeFocused();
  await expectNoHorizontalPageOverflow(page);
});

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
  await page.keyboard.press('Escape');
  const tools = app.getByRole('navigation', { name: '当前工作内容' }).getByRole('button', { name: '文件', exact: true });
  await tools.focus();
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
  await expect(page.getByRole('button', {name: /进入对话/})).toBeEnabled({timeout:30_000});
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
  const tools = app.getByRole('navigation', {name:'当前工作内容'}).getByRole('button', {name:'文件', exact:true});
  await tools.click();
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
  const readme = panel.getByRole('treeitem', {name:'预览文件 README.md'});
  await expect(readme).toBeVisible();
  await readme.click();
  const preview = page.getByRole('dialog').filter({has: page.getByRole('heading', {name:'README.md',exact:true})});
  await expect(preview.getByRole('heading', {name:'README.md',exact:true})).toBeFocused();
  await expect(page.getByRole('tooltip', {name:'在 Finder 中显示'})).toHaveCount(0);
  await expect(preview.getByText('这是工作区文件预览。', {exact:true})).toBeVisible();
  await preview.getByRole('button', {name:'关闭',exact:true}).click();
  await expect(readme).toBeFocused();
  await page.keyboard.press('Tab');
  await expect.poll(()=>panel.evaluate(e=>e.contains(document.activeElement))).toBe(true);
  await page.setViewportSize({width:1440,height:900});
  await expect(app.locator('.paw-session-workspace__primary')).not.toHaveAttribute('inert','');
  await expect(panel.getByRole('treeitem', {name:'预览文件 README.md'})).toBeVisible();
  await message.focus();
  await expect(message).toBeFocused();
  await expect(message).toHaveValue('文件浏览期间保留的草稿');
  await close.click();
  await expect(tools).toBeFocused();
});
