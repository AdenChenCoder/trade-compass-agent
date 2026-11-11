import { test, expect, chromium } from '@playwright/test';

test('phone acknowledges submission, preserves the next draft and keeps long replies readable', async ({ page }, testInfo) => {
  const fixture = await (await page.request.get('/test/pwa')).json();
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [`--ignore-certificate-errors-spki-list=${fixture.spki}`] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, ignoreHTTPSErrors: true,
    ...(process.env.COMPASS_RECORD_VIDEO ? { recordVideo: { dir: testInfo.outputPath('video'), size: { width: 390, height: 844 } } } : {}) });
  let deviceId: string | undefined; let sessionId: string | undefined;
  let accept!: () => void;
  const acceptance = new Promise<void>(resolve => { accept = resolve; });
  try {
    const phone = await context.newPage();
    const errors: string[] = [];
    phone.on('pageerror', error => errors.push(error.message));
    await phone.goto('https://localhost:19746/mobile/');
    const invite = await (await page.request.post('/api/mobile/pairing/invitations')).json();
    const claim = await phone.evaluate(async invitation => (await fetch('/mobile/v1/browser/claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Compass-PWA': '1' },
      body: JSON.stringify({ invitation, name: '手机消息交互验证' }),
    })).json(), invite.invitation);
    deviceId = claim.device_id;
    const device = (await (await page.request.get('/api/mobile/devices')).json()).devices.find((row: any) => row.device_id === deviceId);
    await page.request.post(`/api/mobile/devices/${deviceId}/approve`, { data: { verification_code: device.verification_code } });
    await phone.reload();
    await phone.getByRole('button', { name: '设置', exact: true }).click();
    await phone.getByText('朱砂橙', { exact: true }).click();
    await phone.getByRole('button', { name: '会话', exact: true }).click();
    const created = phone.waitForResponse(response => response.url().endsWith('/mobile/v1/sessions') && response.request().method() === 'POST');
    await phone.getByRole('button', { name: '新建会话', exact: true }).click();
    sessionId = (await (await created).json()).session_id;
    let baseline = (await (await page.request.get(`/api/agent/sessions/${sessionId}/messages`)).json());
    let frozen = true; let confirmed = false;
    await phone.route(`**/mobile/v1/sessions/${sessionId}/messages?*`, route => frozen ? route.fulfill({ json: baseline }) : route.continue());
    await phone.route('**/mobile/v1/turns/*', route => confirmed ? route.continue() : route.fulfill({ status: 404, json: {} }));
    const submitted: string[] = [];
    await phone.route('**/mobile/v1/turns', async route => {
      submitted.push(route.request().postDataJSON().request_id);
      const response = await route.fetch();
      await acceptance;
      await route.fulfill({ response });
    });
    const composer = phone.getByRole('textbox', { name: '消息', exact: true });
    await composer.fill('请整理我的研究清单');
    await phone.getByRole('button', { name: '发送', exact: true }).click();
    await expect(phone.locator('.message-pending')).toContainText('请整理我的研究清单');
    await expect(phone.getByRole('heading', { name: '今天想研究什么？' })).toHaveCount(0);
    await expect(composer).toHaveValue('');
    await expect(composer).not.toBeFocused();
    await expect(phone.getByRole('region', { name: '回复状态' })).toContainText('正在发送');
    await phone.screenshot({ path: testInfo.outputPath('sending.png') });
    confirmed = true; accept();
    await expect(composer).toBeEnabled();
    await expect(phone.getByRole('region', { name: '回复状态' })).toContainText('正在回复');
    await composer.fill('接下来再核实数据');
    frozen = false;
    await expect(phone.locator('.message-pending')).toHaveCount(0);
    await expect(phone.locator('.message.user')).toHaveCount(1);
    await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
    await phone.screenshot({ path: testInfo.outputPath('waiting.png') });
    await phone.setViewportSize({ width: 390, height: 420 });
    await expect(composer).toBeInViewport();
    await expect(phone.getByRole('button', { name: '设置', exact: true })).toBeInViewport();
    await phone.screenshot({ path: testInfo.outputPath('keyboard-waiting.png') });
    await phone.setViewportSize({ width: 390, height: 844 });
    await phone.locator('main').evaluate(element => { element.scrollTop = element.scrollHeight; });
    await page.request.post('/test/reply');
    const answer = phone.getByRole('heading', { name: '先把问题分成三组', exact: true });
    await expect(answer).toBeInViewport();
    await expect(composer).toHaveValue('接下来再核实数据');
    await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeEnabled();
    await expect(phone.getByRole('region', { name: '回复状态' })).toHaveCount(0);
    await phone.screenshot({ path: testInfo.outputPath('long-answer.png') });
    await phone.getByRole('button', { name: '发送', exact: true }).click();
    await phone.locator('main').evaluate(element => { element.scrollTop = 0; });
    await expect(phone.getByRole('button', { name: '查看新回复', exact: true })).toBeInViewport();
    expect(await phone.locator('main').evaluate(element => element.scrollTop)).toBeLessThan(10);
    await phone.screenshot({ path: testInfo.outputPath('reading-history.png') });
    await phone.getByRole('button', { name: '查看新回复', exact: true }).click();
    await expect(phone.getByText('已在电脑上的同一个会话收到：接下来再核实数据', { exact: true })).toBeInViewport();
    await expect(composer).toHaveValue('');
    expect(new Set(submitted).size).toBe(2); expect(submitted).toHaveLength(2);
    for (const [theme, label] of [['glacier', '冰川青'], ['berry', '莓果红'], ['volt', '电光蓝']] as const) {
      await phone.getByRole('button', { name: '设置', exact: true }).click();
      await phone.getByText(label, { exact: true }).click();
      await phone.getByRole('button', { name: '会话', exact: true }).click();
      await phone.setViewportSize({ width: 320, height: 568 });
      await phone.locator('main').evaluate(element => { element.scrollTop = 0; });
      await expect(phone.getByRole('button', { name: '回到最新', exact: true })).toBeInViewport();
      expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await phone.screenshot({ path: testInfo.outputPath(`${theme}-reading.png`), animations: 'disabled' });
    }
    await phone.emulateMedia({ reducedMotion: 'reduce' });
    await phone.getByRole('button', { name: '回到最新', exact: true }).click();
    await expect(phone.getByText('已在电脑上的同一个会话收到：接下来再核实数据', { exact: true })).toBeInViewport();
    // Lose an accepted HTTP response. Recovery remains an explicit retry of
    // the original request, even after going offline and returning online.
    baseline = (await (await page.request.get(`/api/agent/sessions/${sessionId}/messages`)).json());
    frozen = true; confirmed = false;
    await phone.unroute('**/mobile/v1/turns');
    let loseResponse = true;
    await phone.route('**/mobile/v1/turns', async route => {
      submitted.push(route.request().postDataJSON().request_id);
      const response = await route.fetch();
      if (loseResponse) { loseResponse = false; await route.abort('failed'); }
      else { frozen = false; confirmed = true; await route.fulfill({ response }); }
    });
    await composer.fill('请保留这条问题');
    await phone.getByRole('button', { name: '发送', exact: true }).click();
    await expect(phone.getByRole('button', { name: '确认并重试原消息' })).toBeInViewport();
    await context.setOffline(true);
    await phone.evaluate(() => window.dispatchEvent(new Event('offline')));
    await expect(phone.getByRole('region', { name: '回复状态' })).toContainText('连接中断');
    await expect(phone.locator('.reply-pulse')).toHaveCount(0);
    await expect(phone.getByRole('button', { name: '确认并重试原消息' })).toBeDisabled();
    await expect(phone.getByRole('button', { name: '确认并重试原消息' })).toBeInViewport();
    await phone.screenshot({ path: testInfo.outputPath('offline-pending.png') });
    await context.setOffline(false);
    await phone.evaluate(() => window.dispatchEvent(new Event('pageshow')));
    await expect(phone.getByRole('region', { name: '回复状态' })).toContainText('发送结果待确认');
    await phone.screenshot({ path: testInfo.outputPath('retry-original.png') });
    expect(submitted).toHaveLength(3);
    await phone.getByRole('button', { name: '确认并重试原消息' }).click();
    await expect(phone.getByText('已在电脑上的同一个会话收到：请保留这条问题', { exact: true })).toBeInViewport();
    expect(submitted).toHaveLength(4); expect(submitted[2]).toBe(submitted[3]);
    const stored = (await (await page.request.get(`/api/agent/sessions/${sessionId}/messages`)).json()).messages;
    expect(stored.filter((message: any) => message.role === 'user').map((message: any) => message.content)).toEqual(['请整理我的研究清单', '接下来再核实数据', '请保留这条问题']);
    expect(errors).toEqual([]);
  } finally {
    accept(); await page.request.post('/test/reply');
    await context.close(); await browser.close();
    if (sessionId) await page.request.delete(`/api/agent/sessions/${sessionId}`);
    if (deviceId) await page.request.delete(`/api/mobile/devices/${deviceId}`);
  }
});
