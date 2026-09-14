import { test, expect, chromium } from '@playwright/test';

test('task cards open complete, accessible details and return to the same list', async ({ page }, testInfo) => {
  const fixture = await (await page.request.get('/test/pwa')).json();
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [`--ignore-certificate-errors-spki-list=${fixture.spki}`] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, permissions: ['clipboard-read', 'clipboard-write'] });
  let deviceId: string | undefined;
  const failure = (await (await page.request.post('/test/task', { data: { status: 'failed' } })).json()).notice;
  expect(failure.severity).toBe('warning');
  expect(failure.task_status).toBe('failed');
  const original = { title: '观察清单整理完成', severity: 'info', message: '# 整理结果\n\n本次整理包含 **三项观察**，可以继续在电脑上核对。\n\n```text\n检查记录：仅用于界面验收\n状态：整理完成\n```\n\n'
    + Array.from({ length: 25 }, (_, index) => `第 ${index + 1} 项观察：保留完整内容，详情中可以逐项阅读。`).join('\n\n')
    + '\n\n| 项目 | 结果 |\n| --- | --- |\n| 长字段 | ' + 'a'.repeat(160) + ' |\n\n完整结果结束。' };
  let notices = [original, ...Array.from({ length: 12 }, (_, index) => index === 5 ? failure : ({ title: `历史任务 ${index + 1}`, message: '这条消息来自测试电脑。', severity: 'info' }))];
  try {
    const phone = await context.newPage();
    await phone.route('**/mobile/v1/notifications?limit=100', route => route.fulfill({ json: notices }));
    await phone.goto('https://localhost:19746/mobile/');
    const invite = await (await page.request.post('/api/mobile/pairing/invitations')).json();
    const claim = await phone.evaluate(async invitation => (await fetch('/mobile/v1/browser/claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Compass-PWA': '1' },
      body: JSON.stringify({ invitation, name: '详情测试手机' }),
    })).json(), invite.invitation);
    deviceId = claim.device_id;
    const device = (await (await page.request.get('/api/mobile/devices')).json()).devices.find((d: any) => d.device_id === deviceId);
    expect((await page.request.post(`/api/mobile/devices/${deviceId}/approve`, { data: { verification_code: device.verification_code } })).ok()).toBe(true);
    await phone.reload();
    await phone.getByRole('button', { name: '任务消息', exact: true }).click();
    const originalCard = phone.getByRole('button', { name: `查看任务详情：${original.title}`, exact: true });
    await originalCard.scrollIntoViewIfNeeded();
    const scrollBefore = await phone.evaluate(() => scrollY);
    expect(scrollBefore).toBeGreaterThan(0);
    await originalCard.click();
    const sheet = phone.getByRole('dialog', { name: original.title, exact: true });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole('button', { name: '关闭详情', exact: true })).toBeFocused();
    await expect(sheet.getByRole('heading', { name: '整理结果', exact: true })).toBeVisible();
    await expect(sheet.locator('pre')).toContainText('仅用于界面验收');
    await expect(sheet.getByRole('button', { name: /重新运行|RE-RUN/ })).toHaveCount(0);
    // New results must not switch the message being read or disturb the focused card.
    notices = [...notices, { title: '刚刚完成的新任务', message: '新消息不会替换打开的正文。', severity: 'info' }];
    await expect(phone.getByText('14 条消息')).toBeAttached();
    await expect(sheet).toContainText('完整结果结束。');
    await sheet.getByRole('button', { name: '复制内容', exact: true }).click();
    await expect(sheet.getByRole('status')).toHaveText('已复制完整内容');
    expect(await phone.evaluate(() => navigator.clipboard.readText())).toBe(`${original.title}\n\n${original.message}`);
    // Focus stays within the modal while the rest of the application is inert.
    await sheet.getByRole('button', { name: '返回列表', exact: true }).focus();
    await phone.keyboard.press('Tab');
    await expect(sheet.getByRole('button', { name: '关闭详情', exact: true })).toBeFocused();
    for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 420 }, { width: 390, height: 844 }]) {
      await phone.setViewportSize(viewport);
      await expect(sheet.getByRole('button', { name: '关闭详情', exact: true })).toBeInViewport();
      await expect(sheet.getByRole('button', { name: '复制内容', exact: true })).toBeInViewport();
      await sheet.getByText('完整结果结束。', { exact: true }).scrollIntoViewIfNeeded();
      await expect(sheet.getByText('完整结果结束。', { exact: true })).toBeInViewport();
      expect(await sheet.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    }
    // Read the actual rendered detail at the start, including its status and code block.
    await sheet.getByRole('heading', { name: '整理结果', exact: true }).scrollIntoViewIfNeeded();
    await phone.screenshot({ path: testInfo.outputPath('task-detail.png'), animations: 'disabled' });
    // Explicit return keeps the original card focused and the original scroll offset.
    await sheet.getByRole('button', { name: '返回列表', exact: true }).click();
    await expect(sheet).toHaveCount(0);
    await expect(originalCard).toBeFocused();
    expect(Math.abs(await phone.evaluate(() => scrollY) - scrollBefore)).toBeLessThan(2);
    await expect(phone).toHaveURL('https://localhost:19746/mobile/');
    await originalCard.press('Enter');
    await expect(sheet).toBeVisible();
    await phone.goBack();
    await expect(sheet).toHaveCount(0);
    await expect(originalCard).toBeFocused();
    // Escape and backdrop taps are both non-destructive exits.
    await originalCard.press('Space');
    await expect(sheet).toBeVisible();
    await phone.keyboard.press('Escape');
    await expect(sheet).toHaveCount(0);
    const failureCard = phone.getByRole('button', { name: `查看任务详情：${failure.title}`, exact: true });
    await expect(failureCard.locator('..').locator('.result-badge')).toHaveText('任务失败');
    await failureCard.click();
    const failureSheet = phone.getByRole('dialog', { name: failure.title, exact: true });
    await expect(failureSheet).toContainText('任务失败');
    await phone.screenshot({ path: testInfo.outputPath('task-failure-detail.png'), animations: 'disabled' });
    await phone.mouse.click(10, 10);
    await expect(failureSheet).toHaveCount(0);
    // Cached notification details remain readable without fetching additional data.
    await context.setOffline(true);
    await originalCard.click();
    await expect(sheet).toContainText('完整结果结束。');
    await sheet.getByRole('button', { name: '关闭详情', exact: true }).click();
    await expect(sheet).toHaveCount(0);
    await expect(originalCard).toBeFocused();
  } finally {
    if (deviceId) await page.request.delete(`/api/mobile/devices/${deviceId}`);
    await context.close(); await browser.close();
  }
});
