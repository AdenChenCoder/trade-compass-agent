import { test, expect, chromium } from '@playwright/test';

test('experimental peer bundle connects using real RTC, preserves same session through reconnect, then revokes', async ({ page }) => {
  await page.goto('/settings');
  await page.getByText('实验性 WebRTC 连接', { exact: true }).click();
  await expect(page.getByRole('link', { name: '打开移动端界面' })).toHaveAttribute('href', '/mobile/');
  expect((await page.request.get('/mobile/')).status()).toBe(200);
  const fixture = await (await page.request.get('/test/pwa')).json();
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [`--ignore-certificate-errors-spki-list=${fixture.spki}`] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, permissions: ['notifications'] });
  await context.addInitScript(subscription => {
    let subscribed: any = null;
    PushManager.prototype.subscribe = async function(options: any) {
      subscribed = { ...subscription, options, toJSON: () => subscription, unsubscribe: async () => { subscribed = null; return true; } };
      return subscribed;
    };
    PushManager.prototype.getSubscription = async () => subscribed;
  }, fixture.subscription);
  const phone = await context.newPage();
  try {
  const cdp = await context.newCDPSession(phone);
  let registrationId = '';
  cdp.on('ServiceWorker.workerRegistrationUpdated', (event: any) => {
    for (const registration of event.registrations) if (registration.scopeURL === 'https://localhost:19748/mobile/') registrationId = registration.registrationId;
  });
  cdp.on('ServiceWorker.workerErrorReported', event => console.error('Worker error', event));
  await cdp.send('ServiceWorker.enable');
  const businessHTTP: string[] = [];
  context.on('request', request => { if (/\/mobile\/v1|\/api\//.test(request.url())) businessHTTP.push(request.url()); });
  await phone.goto('https://localhost:19748/mobile/');
  await expect.poll(() => phone.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
  async function connect() {
    await page.getByRole('button', { name: '生成直连信息', exact: true }).click();
    const offer = await page.getByLabel('电脑直连信息（5 分钟内使用）').inputValue();
    await phone.getByLabel('电脑直连信息', { exact: true }).fill(offer);
    await phone.getByRole('button', { name: '生成手机返回信息' }).click();
    await expect(phone.getByLabel('手机返回信息', { exact: true })).toBeVisible({ timeout: 18000 }).catch(async error => { throw new Error(`${error}\n手机页面：${await phone.locator('body').innerText()}`); });
    const answer = await phone.getByLabel('手机返回信息', { exact: true }).inputValue();
    expect(JSON.parse(answer).sdp).toContain('a=candidate:');
    await page.getByLabel('手机返回信息', { exact: true }).fill(answer);
    const submitted = page.waitForResponse(response => response.url().endsWith('/api/mobile/peer/answer'));
    await page.getByRole('button', { name: '建立直连', exact: true }).click();
    expect((await submitted).status()).toBe(200);
    await phone.getByRole('button', { name: '电脑已导入，继续连接' }).click();
  }
  await connect();
  await expect(phone.locator('.pair-code strong')).toBeVisible();
  expect(await phone.getByRole('button', { name: /消费行业观察/ }).count()).toBe(0);
  const code = await phone.locator('.pair-code strong').textContent();
  await expect(page.getByText(code!, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '数字一致，批准' }).click();
  await phone.getByRole('button', { name: /消费行业观察/ }).click();
  await phone.getByLabel('消息', { exact: true }).fill('静态网页通过直连继续同一会话');
  await phone.getByRole('button', { name: '发送', exact: true }).click();
  await expect(phone.getByText('已在电脑上的同一个会话收到：静态网页通过直连继续同一会话')).toBeVisible();
  const messages = await (await page.request.get('/api/agent/sessions/shared-demo/messages')).json();
  expect(messages.messages.filter((m: any) => m.content === '静态网页通过直连继续同一会话')).toHaveLength(1);
  await phone.getByRole('button', { name: '任务消息', exact: true }).click();
  await expect(phone.getByText('收盘复盘已完成')).toBeVisible();
  await phone.getByRole('button', { name: '连接', exact: true }).click();
  await phone.getByRole('button', { name: '开启测试通知' }).click();
  await expect(phone.getByText('已保存通知订阅，等待真机测试。')).toBeVisible();
  await phone.getByRole('button', { name: '任务消息', exact: true }).click();
  await page.getByRole('button', { name: '发送测试通知' }).click();
  await expect(page.getByText('推送服务已接受，等待手机回传')).toBeVisible();
  const sent = await (await page.request.get('/test/pwa')).json();
  expect(sent.payloads).toHaveLength(1);
  await cdp.send('ServiceWorker.deliverPushMessage', { origin: 'https://localhost:19748', registrationId, data: JSON.stringify(sent.payloads[0]) });
  await expect.poll(() => phone.evaluate(async () => (await (await navigator.serviceWorker.ready).getNotifications()).length), { timeout: 12000 }).toBe(1);
  await expect(page.getByText('推送服务已接受，等待手机回传')).toBeVisible();
  await context.setOffline(true);
  await phone.reload();
  await phone.getByRole('button', { name: /消费行业观察/ }).click();
  await expect(phone.getByText('已在电脑上的同一个会话收到：静态网页通过直连继续同一会话')).toBeVisible();
  await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  await context.setOffline(false);
  await expect.poll(() => phone.evaluate(() => navigator.onLine)).toBe(true);
  await phone.getByRole('button', { name: '连接', exact: true }).click();
  await connect();
  await expect(phone.getByRole('status', { name: '已连接', exact: true })).toBeVisible({ timeout: 20000 }).catch(async error => { throw new Error(`${error}\nRTC状态：\n手机页面：${await phone.locator('body').innerText()}`); });
  await expect(page.getByText('手机已处理通知并回传；请确认锁屏是否可见')).toBeVisible({ timeout: 12000 }).catch(async error => { throw new Error(`${error}\n手机页面：${await phone.locator('body').innerText()}`); });
  // Approval is a durable device property, not an RTC connection property.
  await expect(page.getByRole('button', { name: '数字一致，批准' })).toHaveCount(0);
  await phone.getByRole('button', { name: '会话', exact: true }).click();
  await expect(phone.getByText('已在电脑上的同一个会话收到：静态网页通过直连继续同一会话')).toBeVisible();
  expect(businessHTTP).toEqual([]);
  await page.getByRole('button', { name: '移除', exact: true }).click();
  await expect(phone.getByRole('alert')).toContainText('直连');
  await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  } finally { await context.close(); await browser.close(); }
});
