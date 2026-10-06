import { expect, test } from '@playwright/test';

test('project chat and results switch without covering keyboard or pointer targets', async ({ page }, info) => {
  test.setTimeout(90_000);
  await page.goto('/?controlTransport=mock#/eval-lab');
  await page.getByRole('button', { name: '新建项目', exact: true }).click();
  await page.getByRole('textbox', { name: '描述你的项目' }).fill('公开演示：检查项目导航');
  await page.getByRole('button', { name: '创建并开始', exact: true }).click();
  const body = page.locator('.lab-project-body');
  await expect(body).toBeVisible({ timeout: 30_000 });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.lab-project-results')).toBeHidden();
  await page.getByRole('button', { name: '收起项目 Agent', exact: true }).click();
  const view = page.getByRole('combobox', { name: '当前项目视图' });
  for (const value of ['materials', 'runs', 'artifact', 'apps', 'lifecycle', 'brief', 'workspace']) {
    await view.selectOption(value);
    await expect(view).toHaveValue(value);
  }
  await view.selectOption('knowledge');
  for (const name of ['资料', '索引与检索', '评测']) {
    await page.locator('nav[aria-label="知识库实验步骤"]').getByRole('button', { name, exact: true }).click();
    await expect(page.getByText('知识库实验数据未完整返回，请重新读取。')).toHaveCount(0);
  }
  await view.selectOption('materials');
  await page.getByRole('button', { name: '添加材料', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '添加项目材料' });
  await dialog.getByLabel('粘贴材料标题', { exact: true }).fill('公开演示材料');
  await dialog.getByRole('textbox', { name: '材料正文', exact: true }).fill('这次演示不写入本机文件。');
  await dialog.getByRole('button', { name: '保存文本材料', exact: true }).click();
  await expect(dialog.getByText('此操作需要真实 Lab 服务；演示模式没有执行模型、评测或文件写入。')).toBeVisible();
  await expect(dialog.getByRole('alert')).toBeInViewport();
  await expect(page.getByRole('button', { name: '核对原操作', exact: true })).toHaveCount(0);
  await expect(dialog.getByRole('textbox', { name: '材料正文', exact: true })).toHaveValue('这次演示不写入本机文件。');
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(dialog).toBeHidden();
  await view.selectOption('workflow');
  await expect(page.locator('.lab-flow__canvas-tools')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '工作流记录尚未返回' })).toBeVisible();
  const geometry = await page.locator('.lab-project-header').evaluate(el => {
    const heading = el.querySelector('h1')!.getBoundingClientRect();
    const actions = el.querySelector('.lab-project-header__actions')!.getBoundingClientRect();
    return { headingWidth: heading.width, headingRight: heading.right, actionsLeft: actions.left, actionsRight: actions.right, width: innerWidth };
  });
  expect(geometry.headingWidth).toBeGreaterThan(64);
  expect(geometry.headingRight).toBeLessThanOrEqual(geometry.actionsLeft + 1);
  expect(geometry.actionsRight).toBeLessThanOrEqual(geometry.width);
  await info.attach('lab-workflow-without-unused-tools.png', { body: await page.screenshot(), contentType: 'image/png' });
  await page.getByRole('button', { name: '展开项目 Agent', exact: true }).click();
  await expect(page.getByRole('region', { name: '项目 Agent', exact: true })).toBeVisible();
  await expect(page.locator('.lab-project-results')).toBeHidden();
  await info.attach('lab-chat-without-covered-results.png', { body: await page.screenshot(), contentType: 'image/png' });
});
