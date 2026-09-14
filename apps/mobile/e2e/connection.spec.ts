import { test, expect } from '@playwright/test';
import https from 'node:https';
import tls from 'node:tls';
import { createHash, randomBytes } from 'node:crypto';

// A test transport stands in for the system bridge. It verifies the actual TLS
// socket before handing it to HTTP. Native Java/Swift still require device tests.
function pinned(endpoint: string, fingerprint: string, path: string, method: string, body?: string, secret?: string): Promise<any> {
  const url = new URL(endpoint);
  const agent = new https.Agent();
  agent.createConnection = (_options: any, callback: any): any => {
    const socket = tls.connect({ host: url.hostname, port: Number(url.port), rejectUnauthorized: false }, () => {
      const actual = createHash('sha256').update(socket.getPeerCertificate().raw).digest('hex');
      if (actual !== fingerprint) { socket.destroy(); callback(new Error('Wrong computer identity')); }
      else callback(null, socket);
    });
    socket.once('error', callback);
  };
  return new Promise((resolve, reject) => {
    const request = https.request({ hostname: url.hostname, port: url.port, path, method, agent,
      headers: { 'Content-Type': 'application/json', ...(secret ? { Authorization: `Bearer ${secret}` } : {}) } }, response => {
      let text = ''; response.on('data', chunk => text += chunk); response.on('end', () => {
        agent.destroy(); try { resolve({ status: response.statusCode, data: JSON.parse(text) }); } catch (error) { reject(error); }
      });
    });
    request.on('error', reject); request.end(body);
  });
}

test('pair in desktop UI, continue the same session on phone, view tasks and revoke', async ({ browser, page }, testInfo) => {
  await page.goto('/settings');
  await page.getByRole('button', { name: '开启手机连接', exact: true }).click();
  await page.getByText('原生开发测试入口', { exact: true }).click();
  await page.getByLabel('手机可访问的电脑地址').fill('https://127.0.0.1:19745');
  await page.getByRole('button', { name: '生成二维码' }).click();
  await expect(page.getByAltText('用交易罗盘手机测试版扫描此配对二维码')).toBeVisible();
  let clipboard = '';
  await page.exposeFunction('captureClipboard', (text: string) => { clipboard = text; });
  await page.evaluate(() => { Object.defineProperty(navigator, 'clipboard', { value: { writeText: (text: string) => (window as any).captureClipboard(text) } }); });
  await page.getByRole('button', { name: '复制连接信息', exact: true }).click();
  await expect.poll(() => clipboard.length).toBeGreaterThan(100);
  const invitation = JSON.parse(clipboard);
  const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  let state: any = null;
  let uncertain = false;
  let uncertainId = "";
  await phone.exposeFunction('nativeCall', async (_plugin: string, method: string, options: any) => {
    if (method === 'connection') return { connected: !!state, endpoint: state?.endpoint, computer_id: state?.computer_id };
    if (method === 'forget') { state = null; return; }
    if (method === 'pair') {
      const invite = JSON.parse(options.invitation);
      state = { ...invite, device_secret: randomBytes(32).toString('base64url') };
      return pinned(state.endpoint, state.certificate_sha256, '/mobile/v1/pairing/claim', 'POST', JSON.stringify({ invitation: state.invitation, name: options.name, device_secret: state.device_secret }));
    }
    if (uncertain && options.path === '/mobile/v1/turns') {
      uncertainId = JSON.parse(options.body).request_id;
      return { status: 202, data: { request_id: uncertainId, session_id: 'shared-demo', status: 'running' } };
    }
    if (uncertain && options.path === `/mobile/v1/turns/${uncertainId}`) return { status: 200, data: { request_id: uncertainId, session_id: 'shared-demo', status: 'unknown' } };
    return pinned(state.endpoint, state.certificate_sha256, options.path, options.method, options.body, state.device_secret);
  });
  await phone.addInitScript(() => {
    (window as any).CapacitorCustomPlatform = { name: 'ios' };
    (window as any).Capacitor = { PluginHeaders: [{ name: 'Compass', methods: ['connection','pair','request','forget'].map(name => ({ name, rtype: 'promise' })) }], nativePromise: (...args: any[]) => (window as any).nativeCall(...args) };
  });
  await phone.goto('/phone/');
  await phone.getByText('使用复制的连接信息').click();
  await phone.getByLabel('电脑连接信息').fill(JSON.stringify(invitation));
  await phone.getByRole('button', { name: '申请连接', exact: true }).click();
  await expect(phone.getByText('核对两端数字一致')).toBeVisible();
  const code = await phone.locator('.pair-code strong').textContent();
  await expect(page.getByText(code!, { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('desktop-pairing.png'), fullPage: true });
  await phone.screenshot({ path: testInfo.outputPath('phone-pairing.png'), fullPage: true });
  await page.getByRole('button', { name: '数字一致，批准' }).click();
  await phone.getByRole('button', { name: /消费行业观察/ }).click();
  await expect(phone.getByText('我们可以从需求变化、企业盈利和估值三个方面继续讨论。')).toBeVisible();
  const desktopChat = await page.context().newPage();
  await desktopChat.addInitScript(() => localStorage.setItem('trade-compass-session-id', 'shared-demo'));
  await desktopChat.goto('/agent');
  await expect(desktopChat.getByText('我们可以从需求变化、企业盈利和估值三个方面继续讨论。')).toBeVisible();
  await phone.getByLabel('消息', { exact: true }).fill('接着分析需求变化');
  await phone.getByRole('button', { name: '发送', exact: true }).click();
  await expect(phone.getByText('已在电脑上的同一个会话收到：接着分析需求变化')).toBeVisible();
  await expect(desktopChat.getByText('已在电脑上的同一个会话收到：接着分析需求变化')).toBeVisible();
  await desktopChat.close();
  await phone.screenshot({ path: testInfo.outputPath('phone-session.png'), fullPage: true });
  const desktopHistory = await page.request.get('/api/agent/sessions/shared-demo/messages');
  expect((await desktopHistory.json()).messages.map((m: any) => m.content)).toContain('已在电脑上的同一个会话收到：接着分析需求变化');
  uncertain = true;
  await expect(phone.getByLabel('消息', { exact: true })).toBeEnabled();
  await phone.getByLabel('消息', { exact: true }).fill('执行结果不确定时，保留这段文字');
  await phone.getByRole('button', { name: '发送', exact: true }).click();
  await expect(phone.getByText('电脑曾中断，无法确定这条请求是否完成。请先检查历史和执行结果，再决定是否发起新请求。')).toBeVisible();
  await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('执行结果不确定时，保留这段文字');
  await phone.getByRole('button', { name: '任务消息', exact: true }).click();
  await expect(phone.getByText('收盘复盘已完成')).toBeVisible();
  await phone.screenshot({ path: testInfo.outputPath('phone-notices.png'), fullPage: true });
  await page.getByRole('button', { name: '移除', exact: true }).click();
  await expect(phone.getByRole('alert')).toContainText('连接授权已失效');
  await phone.close();
});

test('changing desktop view does not stop the accepted turn or inject its reply into a new chat', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('trade-compass-session-id', 'shared-demo'));
  await page.goto('/agent');
  await expect(page.getByText('我们可以从需求变化、企业盈利和估值三个方面继续讨论。')).toBeVisible();
  const controls: string[] = [];
  page.on('request', request => { if (request.url().endsWith('/api/agent/control')) controls.push(request.postData() || ''); });
  await page.getByPlaceholder('输入问题，可粘贴链接或添加附件…').fill('slow-test');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect.poll(async () => {
    const response = await page.request.get('/api/agent/sessions/shared-demo/messages');
    return (await response.json()).has_active_turn;
  }).toBe(true);
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await expect.poll(async () => {
    const response = await page.request.get('/api/agent/sessions/shared-demo/messages');
    return (await response.json()).messages.some((message: any) => message.content === '已在电脑上的同一个会话收到：slow-test');
  }).toBe(true);
  expect(controls).toEqual([]);
  await expect(page.getByText('已在电脑上的同一个会话收到：slow-test', { exact: true })).toHaveCount(0);
});
