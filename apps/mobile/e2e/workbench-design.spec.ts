import { test, expect } from '@playwright/test';

test('workbench themes, narrow navigation and failed route loads preserve user work', async ({ page, context }, testInfo) => {
  test.setTimeout(120000);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1360, height: 920 });
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const writes: string[] = [];
  page.on('request', request => { if (request.method() === 'POST') writes.push(new URL(request.url()).pathname); });
  await page.goto('/agent');
  await page.getByRole('button', { name: /看市场/ }).click();
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('今天大盘资金情况');
  await expect(page.locator('.welcome-prompts')).toHaveCount(0);
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('这段草稿和附件先保留，不要发送。');
  await page.locator('input[type=file]').setInputFiles({ name: 'research.txt', mimeType: 'text/plain', buffer: Buffer.from('尚未发送的研究附件') });
  await expect(page.getByText('文本文件', { exact: true })).toBeVisible();
  const settings = await context.newPage();
  await settings.emulateMedia({ reducedMotion: 'reduce' });
  await settings.goto('/settings');
  for (const [theme, label] of [['ember', '朱砂橙'], ['glacier', '冰川青'], ['berry', '莓果红'], ['volt', '电光蓝']]) {
    await settings.getByRole('radio', { name: new RegExp(label) }).check();
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    await expect(page.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('这段草稿和附件先保留，不要发送。');
    await expect(page.getByText('文本文件', { exact: true })).toBeVisible();
    await settings.screenshot({ path: testInfo.outputPath(`${theme}-settings.png`), animations: 'disabled' });
  }
  // A stale lazy chunk must leave the active conversation mounted, without a reload.
  await page.route('**/assets/SkillsPage-*.js', route => route.abort());
  await page.getByRole('link', { name: '技能', exact: true }).click();
  await expect(page.getByText('页面暂时无法打开', { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/agent$/);
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('这段草稿和附件先保留，不要发送。');
  await expect(page.getByText('文本文件', { exact: true })).toBeVisible();
  expect(writes.filter(path => path.endsWith('/turn'))).toEqual([]);

  const inspection = await context.newPage();
  await inspection.emulateMedia({ reducedMotion: 'reduce' });
  inspection.on('pageerror', error => errors.push(error.message));
  const routes = ['agent', 'portfolio', 'research', 'memory', 'rules', 'skills', 'jobs', 'audit', 'settings'];
  for (const width of [1360, 390, 320]) {
    await inspection.setViewportSize({ width, height: width === 320 ? 568 : 920 });
    for (const route of routes) {
      await inspection.goto(`/${route}`);
      await expect(inspection.locator('main#workspace')).toBeVisible();
      await expect(inspection.locator('.workspace-recovery')).toHaveCount(0);
      await expect(inspection.locator('main#workspace').locator('h1').first()).toBeVisible();
      expect(await inspection.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
      if (route === 'research') {
        await expect(inspection.getByRole('form', { name: '策略假设' })).toBeVisible();
        await expect(inspection.getByRole('form', { name: '运行回测' })).toBeVisible();
      }
      if (['jobs', 'skills', 'agent'].includes(route)) await inspection.screenshot({ path: testInfo.outputPath(`${route}-${width}.png`), animations: 'disabled' });
    }
    if (width < 768) {
      await inspection.getByRole('button', { name: '展开导航' }).click();
      for (const route of routes) await expect(inspection.locator(`nav a[href="/${route}"]`)).toBeVisible();
      await inspection.getByRole('link', { name: '定时任务', exact: true }).click();
      await expect(inspection.getByRole('button', { name: '展开导航' })).toHaveAttribute('aria-expanded', 'false');
      await inspection.getByRole('tab', { name: '日程设置' }).click();
      await inspection.getByLabel('晨间计划', { exact: true }).fill('09:21');
      await inspection.getByRole('tab', { name: '任务', exact: true }).click();
      await inspection.getByRole('tab', { name: '日程设置' }).click();
      await expect(inspection.getByLabel('晨间计划', { exact: true })).toHaveValue('09:21');
    }
  }
  expect(errors.filter(message => !message.includes('Failed to fetch dynamically imported module'))).toEqual([]);
  await settings.close(); await inspection.close();
});
