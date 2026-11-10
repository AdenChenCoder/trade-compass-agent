import { test, expect, chromium, type Route } from '@playwright/test';

test('mobile layouts preserve drafts and connection across themes, narrow screens and task reading', async ({ page }, testInfo) => {
  test.setTimeout(90000);
  const fixture = await (await page.request.get('/test/pwa')).json();
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [`--ignore-certificate-errors-spki-list=${fixture.spki}`] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
  let deviceId: string | undefined;
  try {
    const phone = await context.newPage();
    const capture = async (name: string) => {
      await phone.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await phone.screenshot({ path: testInfo.outputPath(name), animations: 'disabled' });
    };
    const errors: string[] = [];
    phone.on('pageerror', error => errors.push(error.message));
    const writes: string[] = [];
    phone.on('request', request => { if (request.method() === 'POST') writes.push(new URL(request.url()).pathname); });
    const notices = [
      { title: '盘前观察清单', message: '## 关注方向\n\n对照昨日研究假设，核实成交量与行业变化。\n\n- 关注新增证据\n- 保留尚未确认的判断', severity: 'info' },
      { title: '收盘复盘未完成', message: '部分行情暂不可用，本次结果不完整。请核实数据后再作判断。', severity: 'warning', task_status: 'degraded' },
    ];
    await phone.route('**/mobile/v1/notifications?limit=100', route => route.fulfill({ json: notices }));
    const invite = await (await page.request.post('/api/mobile/pairing/invitations')).json();
    await phone.goto(`https://localhost:19746/mobile/#pair=${encodeURIComponent(JSON.stringify(invite))}`);
    await expect(phone.getByLabel('手机名称')).toBeVisible();
    await phone.getByText('朱砂橙', { exact: true }).click();
    await phone.locator('main').evaluate(element => { element.scrollTop = 0; });
    await capture('onboarding.png');
    // Claim through the actual browser API; authorization is supplied by the isolated test computer.
    const claim = await phone.evaluate(async invitation => (await fetch('/mobile/v1/browser/claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Compass-PWA': '1' },
      body: JSON.stringify({ invitation, name: '设计验证手机' }),
    })).json(), invite.invitation);
    deviceId = claim.device_id;
    const device = (await (await page.request.get('/api/mobile/devices')).json()).devices.find((row: { device_id: string }) => row.device_id === deviceId);
    await phone.reload();
    await expect(phone.getByLabel('电脑上的六位配对码')).toBeVisible();
    await capture('pairing.png');
    let sessionRequest: Route | undefined;
    await phone.route('**/mobile/v1/sessions?limit=100', route => { sessionRequest = route; });
    await phone.getByLabel('电脑上的六位配对码').fill(device.verification_code);
    await expect(phone.getByText('正在读取会话…', { exact: true })).toBeVisible();
    await expect(phone.getByText('0 段对话')).toHaveCount(0);
    await capture('loading-sessions.png');
    await expect.poll(() => !!sessionRequest).toBe(true);
    await sessionRequest!.continue();
    await phone.unroute('**/mobile/v1/sessions?limit=100');
    await expect(phone.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await phone.getByRole('textbox', { name: '消息', exact: true }).fill('先核实证据，这段草稿暂不发送。');
    const savedConnection = await phone.evaluate(() => localStorage.getItem('compass.browser.connection.v1'));
    const writesBeforeThemes = writes.length;
    for (const [theme, name] of [['ember', '朱砂橙'], ['glacier', '冰川青'], ['berry', '莓果红'], ['volt', '电光蓝']]) {
      await phone.getByRole('button', { name: '设置', exact: true }).click();
      await expect(phone.getByRole('heading', { name: '设置', exact: true })).toBeVisible();
      await expect(phone.getByRole('button', { name: '连接', exact: true })).toHaveCount(0);
      await phone.getByText(name, { exact: true }).click();
      await expect(phone.locator('html')).toHaveAttribute('data-theme', theme);
      await phone.locator('main').evaluate(element => { element.scrollTop = 0; });
      await capture(`${theme}-connection.png`);
      await phone.getByRole('switch', { name: '手机通知', exact: true }).scrollIntoViewIfNeeded();
      await capture(`${theme}-settings-notifications.png`);
      await phone.getByRole('button', { name: '会话', exact: true }).click();
      await expect(phone.getByRole('button', { name: '会话', exact: true })).toHaveAttribute('aria-current', 'page');
      await expect(phone.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('先核实证据，这段草稿暂不发送。');
      await capture(`${theme}-conversation.png`);
      await phone.getByRole('button', { name: '所有会话' }).click();
      await expect(phone.getByRole('button', { name: '新建会话', exact: true })).toHaveClass('session-create');
      await capture(`${theme}-sessions.png`);
      await phone.getByRole('button', { name: '任务消息', exact: true }).click();
      await expect(phone.getByRole('button', { name: '任务消息', exact: true })).toHaveAttribute('aria-current', 'page');
      await expect(phone.getByText('结果不完整', { exact: true })).toBeVisible();
      await expect(phone.getByRole('button', { name: '查看任务详情：收盘复盘未完成', exact: true })).toHaveAccessibleDescription('结果不完整');
      await capture(`${theme}-notices.png`);
      await phone.getByRole('button', { name: '查看任务详情：盘前观察清单', exact: true }).click();
      await expect(phone.getByRole('dialog')).toContainText('保留尚未确认的判断');
      await capture(`${theme}-detail.png`);
      await phone.keyboard.press('Escape');
      await expect(phone.getByRole('dialog')).toHaveCount(0);
      await phone.getByRole('button', { name: '会话', exact: true }).click();
      await expect(phone.getByRole('button', { name: '会话', exact: true })).toHaveAttribute('aria-current', 'page');
      await phone.getByRole('button', { name: /消费行业观察/ }).click();
    }
    expect(writes).toHaveLength(writesBeforeThemes);
    expect(await phone.evaluate(() => localStorage.getItem('compass.browser.connection.v1'))).toBe(savedConnection);
    for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 420 }, { width: 430, height: 932 }, { width: 844, height: 390 }]) {
      await phone.setViewportSize(viewport);
      await expect(phone.getByRole('textbox', { name: '消息', exact: true })).toBeInViewport();
      await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeInViewport();
      await expect(phone.getByRole('button', { name: '设置', exact: true })).toBeInViewport();
      expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const main = await phone.locator('main').boundingBox();
      const nav = await phone.getByRole('navigation', { name: '主导航' }).boundingBox();
      expect(main!.y + main!.height).toBeLessThanOrEqual(nav!.y + 1);
      await phone.getByRole('button', { name: '设置', exact: true }).click();
      await phone.getByRole('radio', { name: '电光蓝' }).scrollIntoViewIfNeeded();
      expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await phone.getByRole('button', { name: '会话', exact: true }).click();
      await expect(phone.getByRole('button', { name: '会话', exact: true })).toHaveAttribute('aria-current', 'page');
    }
    await phone.getByRole('button', { name: '新建会话', exact: true }).click();
    await expect(phone.getByRole('heading', { name: '今天想研究什么？' })).toBeVisible();
    await phone.setViewportSize({ width: 320, height: 568 });
    await expect(phone.getByRole('textbox', { name: '消息', exact: true })).toBeInViewport();
    await expect(phone.getByRole('button', { name: '设置', exact: true })).toBeInViewport();
    await expect(phone.locator('.conversation-welcome > .brand-mark')).toBeInViewport({ ratio: 1 });
    await expect(phone.getByRole('button', { name: '今天市场有哪些变化？', exact: true })).toBeInViewport({ ratio: 1 });
    await capture('new-conversation-narrow.png');
    const beforeSuggestion = writes.length;
    await phone.getByRole('button', { name: '今天市场有哪些变化？', exact: true }).click();
    await expect(phone.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('今天市场有哪些变化？');
    await expect(phone.getByRole('textbox', { name: '消息', exact: true })).toBeFocused();
    expect(writes).toHaveLength(beforeSuggestion);
    await phone.setViewportSize({ width: 390, height: 844 });
    await phone.getByRole('button', { name: '设置', exact: true }).click();
    await phone.getByRole('radio', { name: '电光蓝' }).focus();
    await phone.keyboard.press('ArrowLeft');
    await expect(phone.getByRole('radio', { name: '莓果红' })).toBeChecked();
    await phone.getByText('朱砂橙', { exact: true }).click();
    await phone.reload();
    await expect(phone.locator('html')).toHaveAttribute('data-theme', 'ember');
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await expect(phone.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('先核实证据，这段草稿暂不发送。');
    const longDeviceName = '研究用手机与我的个人设备'.repeat(6);
    await phone.route('**/mobile/v1/pairing/status', route => route.fulfill({ json: { status: 'approved', name: longDeviceName } }));
    await phone.getByRole('button', { name: '设置', exact: true }).click();
    await phone.getByRole('button', { name: '刷新连接状态' }).click();
    await expect(phone.locator('.device-link')).toContainText(longDeviceName);
    await phone.setViewportSize({ width: 320, height: 568 });
    await phone.locator('main').evaluate(element => { element.scrollTop = 0; });
    await expect(phone.getByRole('button', { name: '刷新连接状态' })).toBeInViewport();
    expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await capture('settings-long-name.png');
    await phone.getByText('连接管理', { exact: true }).click();
    await expect(phone.getByRole('definition').filter({ hasText: longDeviceName })).toBeVisible();
    await phone.unroute('**/mobile/v1/pairing/status');
    await phone.setViewportSize({ width: 390, height: 844 });
    await phone.getByRole('button', { name: '会话', exact: true }).click();
    await expect(phone.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('先核实证据，这段草稿暂不发送。');
    await context.setOffline(true);
    await phone.evaluate(() => window.dispatchEvent(new Event('offline')));
    await expect(phone.getByRole('alert')).toContainText(/网络已断开|暂时无法连接电脑/);
    await expect(phone.getByRole('alert')).toBeInViewport();
    await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
    await capture('offline-draft.png');
    expect(errors).toEqual([]);
  } finally {
    if (deviceId) await page.request.delete(`/api/mobile/devices/${deviceId}`);
    await context.close(); await browser.close();
  }
});
