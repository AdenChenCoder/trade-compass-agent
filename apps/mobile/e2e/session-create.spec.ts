import { test, expect, chromium } from '@playwright/test';

test('phone keeps creating real computer sessions while preserving existing conversations and drafts', async ({ page }, testInfo) => {
  const fixture = await (await page.request.get('/test/pwa')).json();
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [`--ignore-certificate-errors-spki-list=${fixture.spki}`] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const created: string[] = []; let deviceId: string | undefined;
  const desktopSessions = async () => (await (await page.request.get('/api/agent/sessions?limit=100')).json()).sessions;
  try {
    const phone = await context.newPage();
    await phone.goto('https://localhost:19746/mobile/');
    const invite = await (await page.request.post('/api/mobile/pairing/invitations')).json();
    const claim = await phone.evaluate(async invitation => (await fetch('/mobile/v1/browser/claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Compass-PWA': '1' },
      body: JSON.stringify({ invitation, name: '会话创建测试手机' }),
    })).json(), invite.invitation);
    deviceId = claim.device_id;
    const device = (await (await page.request.get('/api/mobile/devices')).json()).devices.find((row: any) => row.device_id === deviceId);
    await page.request.post(`/api/mobile/devices/${deviceId}/approve`, { data: { verification_code: device.verification_code } });
    await phone.addInitScript(() => {
      (window as any).sawStartupPairing = false;
      new MutationObserver(() => {
        if (document.querySelector('.app-shell.onboarding, .connect-view')) (window as any).sawStartupPairing = true;
      }).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
    });
    // Hold each startup response separately: the UI must not guess "unpaired"
    // while identity or authorization is still in flight, with or without history.
    for (const cachedHistory of [false, true]) {
      let releaseConnection!: () => void; let releasePairing!: () => void;
      const connectionGate = new Promise<void>(resolve => { releaseConnection = resolve; });
      const pairingGate = new Promise<void>(resolve => { releasePairing = resolve; });
      await phone.route('**/mobile/v1/browser/connection', async route => { await connectionGate; await route.continue(); });
      await phone.route('**/mobile/v1/pairing/status', async route => { await pairingGate; await route.continue(); });
      try {
        const connectionRequest = phone.waitForRequest('**/mobile/v1/browser/connection');
        const pairingRequest = phone.waitForRequest('**/mobile/v1/pairing/status');
        await phone.reload(); await connectionRequest;
        await expect(phone.getByRole('status', { name: '', exact: true })).toHaveText('正在打开交易罗盘…');
        await expect(phone.locator('.app-shell')).toHaveCount(0);
        releaseConnection(); await pairingRequest;
        if (!cachedHistory) await expect(phone.locator('.app-shell')).toHaveCount(0);
        releasePairing();
        await expect(phone.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
        expect(await phone.evaluate(() => (window as any).sawStartupPairing)).toBe(false);
      } finally {
        releaseConnection(); releasePairing();
        await phone.unroute('**/mobile/v1/browser/connection');
        await phone.unroute('**/mobile/v1/pairing/status');
      }
    }
    await page.goto('/agent');
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await phone.getByLabel('消息', { exact: true }).fill('原会话的草稿不能丢');
    const before = await desktopSessions();
    const create = async () => {
      const response = phone.waitForResponse(value => value.url().endsWith('/mobile/v1/sessions') && value.request().method() === 'POST');
      await phone.getByRole('button', { name: '新建会话', exact: true }).click();
      const result = await response; expect(result.ok()).toBe(true);
      const id = (await result.json()).session_id; created.push(id);
      await expect(phone.getByRole('heading', { name: '今天想研究什么？', exact: true })).toBeVisible();
      await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('');
      await expect(phone.getByRole('button', { name: '新建会话', exact: true })).toBeEnabled();
      return id;
    };
    const first = await create();
    await phone.screenshot({ path: testInfo.outputPath('new-conversation.png'), animations: 'disabled' });
    await phone.setViewportSize({ width: 320, height: 568 });
    expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(phone.getByLabel('消息', { exact: true })).toBeInViewport();
    await phone.screenshot({ path: testInfo.outputPath('new-conversation-narrow.png'), animations: 'disabled' });
    await phone.setViewportSize({ width: 390, height: 844 });
    await phone.getByLabel('消息', { exact: true }).fill('手机发起的新会话一');
    await phone.getByRole('button', { name: '发送', exact: true }).click();
    await expect(phone.getByText('已在电脑上的同一个会话收到：手机发起的新会话一')).toBeVisible();
    // An already-open desktop discovers and reads the same conversation through its normal UI.
    await page.getByRole('button', { name: /手机发起的新会话一/ }).click();
    await expect(page.getByText('已在电脑上的同一个会话收到：手机发起的新会话一', { exact: true })).toBeVisible();
    const second = await create();
    await phone.getByLabel('消息', { exact: true }).fill('第二段会话的草稿');
    await create();
    expect(new Set(created).size).toBe(3);
    expect((await desktopSessions()).length).toBe(before.length + 3);
    expect((await (await page.request.get(`/api/agent/sessions/${second}/messages`)).json()).messages).toEqual([]);
    await phone.getByRole('button', { name: '所有会话', exact: true }).click();
    await expect(phone.getByRole('button', { name: /手机发起的新会话一/ })).toBeVisible();
    await expect(phone.getByRole('button', { name: '新建会话', exact: true })).toBeEnabled();
    await phone.screenshot({ path: testInfo.outputPath('multiple-sessions.png'), animations: 'disabled' });
    await phone.setViewportSize({ width: 320, height: 568 });
    expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(phone.getByRole('button', { name: '新建会话', exact: true })).toBeInViewport();
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('原会话的草稿不能丢');
    // Failed creation must leave the original draft and selection intact; no automatic retry.
    await phone.route('**/mobile/v1/sessions', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{}' }));
    await phone.getByRole('button', { name: '新建会话', exact: true }).click();
    await expect(phone.getByRole('alert')).toBeVisible();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('原会话的草稿不能丢');
    expect((await desktopSessions()).length).toBe(before.length + 3);
    await phone.unroute('**/mobile/v1/sessions');
    await context.setOffline(true);
    await expect(phone.getByRole('button', { name: '新建会话', exact: true })).toBeDisabled();
    await context.setOffline(false); await phone.reload();
    await phone.getByRole('button', { name: /手机发起的新会话一/ }).click();
    await expect(phone.getByText('已在电脑上的同一个会话收到：手机发起的新会话一')).toBeVisible();
    expect((await (await page.request.get(`/api/agent/sessions/${first}/messages`)).json()).messages).toHaveLength(2);
    // Storage pressure must keep the input visible instead of silently navigating away.
    await phone.evaluate(() => {
      const original = Storage.prototype.setItem;
      (window as any).restoreStorage = () => { Storage.prototype.setItem = original; };
      Storage.prototype.setItem = function(key, value) {
        if (key.includes('.draft.')) throw new DOMException('full', 'QuotaExceededError');
        original.call(this, key, value);
      };
    });
    await phone.getByLabel('消息', { exact: true }).fill('需要保护的草稿');
    await phone.getByRole('button', { name: '所有会话', exact: true }).click();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('需要保护的草稿');
    await expect(phone.getByRole('alert')).toContainText('草稿暂时无法保存');
    await phone.screenshot({ path: testInfo.outputPath('unsaved-draft.png'), animations: 'disabled' });
    await phone.evaluate(() => (window as any).restoreStorage());
    await phone.getByRole('button', { name: '所有会话', exact: true }).click();
    // More than one page must remain discoverable through the phone UI.
    for (let index = 0; index < 100; index += 1) {
      const response = await page.request.post('/api/agent/sessions');
      expect(response.ok()).toBe(true); created.push((await response.json()).session_id);
    }
    await expect(phone.locator('.session-list > button')).toHaveCount(100);
    await expect(phone.getByRole('button', { name: /手机发起的新会话一/ })).toHaveCount(0);
    await phone.getByRole('button', { name: '查看更早会话', exact: true }).click();
    await expect(phone.getByRole('button', { name: /手机发起的新会话一/ })).toBeVisible();
    await phone.getByRole('button', { name: /手机发起的新会话一/ }).click();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('需要保护的草稿');
    // Deleting elsewhere leaves the device connected and provides a draft recovery action.
    expect((await page.request.delete(`/api/agent/sessions/${first}`)).ok()).toBe(true);
    await expect(phone.getByRole('heading', { name: '这段会话已删除', exact: true })).toBeVisible();
    await expect(phone.getByRole('status', { name: '已连接', exact: true })).toBeVisible();
    await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
    await phone.screenshot({ path: testInfo.outputPath('deleted-session.png'), animations: 'disabled' });
    const replacementResponse = phone.waitForResponse(value => value.url().endsWith('/mobile/v1/sessions') && value.request().method() === 'POST');
    await phone.getByRole('button', { name: '用草稿新建会话', exact: true }).click();
    created.push((await (await replacementResponse).json()).session_id);
    await expect(phone.getByRole('heading', { name: '今天想研究什么？', exact: true })).toBeVisible();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('需要保护的草稿');
  } finally {
    for (const id of created) await page.request.delete(`/api/agent/sessions/${id}`);
    if (deviceId) await page.request.delete(`/api/mobile/devices/${deviceId}`);
    await context.close(); await browser.close();
  }
});
