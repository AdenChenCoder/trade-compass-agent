import { test, expect, chromium, type Worker } from '@playwright/test';

async function clickTaskNotification(worker: Worker, id: string, existingWindow: boolean) {
  return worker.evaluate(async ({ id, existingWindow }) => {
    const sw = self as any;
    const notification = (await sw.registration.getNotifications()).find((n: Notification) => n.tag === id);
    if (!notification) throw new Error('Task notification was not displayed');
    const originalMatch = sw.clients.matchAll;
    const originalOpen = sw.clients.openWindow;
    let opened: string | null = null;
    const work: Promise<unknown>[] = [];
    // Execute the registered notificationclick handler, using real displayed notifications,
    // worker storage and postMessage. Only OS window activation is simulated.
    sw.clients.matchAll = async (options: unknown) => existingWindow
      ? (await originalMatch.call(sw.clients, options)).map((client: any) => ({
          url: client.url, focused: true, visibilityState: 'visible',
          postMessage: client.postMessage.bind(client), focus: async () => client,
        })) : [];
    sw.clients.openWindow = async (url: string) => { opened = url; return null; };
    try {
      const event = new Event('notificationclick');
      Object.defineProperty(event, 'notification', { value: notification });
      Object.defineProperty(event, 'waitUntil', { value: (promise: Promise<unknown>) => work.push(promise) });
      sw.dispatchEvent(event);
      await Promise.all(work);
      return opened;
    } finally { sw.clients.matchAll = originalMatch; sw.clients.openWindow = originalOpen; }
  }, { id, existingWindow });
}

test('browser pairs over HTTPS, shares history, receives a test push, survives offline and is revoked', async ({ page }, testInfo) => {
  const fixture = await (await page.request.get('/test/pwa')).json();
  // Narrow test-only trust override, without modifying the machine trust store.
  // This is not evidence that an iPhone trusts a production certificate.
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [`--ignore-certificate-errors-spki-list=${fixture.spki}`] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, permissions: ['notifications'] });
  try {
    // Only vendor subscription creation is simulated. All pairing/API requests use real browser fetch.
    await context.addInitScript(subscription => {
      // A real browser subscription survives reloads; persist the synthetic
      // subscription too, so UI permission checks exercise the same lifecycle.
      const wrap = (options: any) => ({ ...subscription, options, toJSON: () => subscription,
        unsubscribe: async () => { localStorage.removeItem('test.push.key'); return true; } });
      PushManager.prototype.subscribe = async function(options: any) {
        localStorage.setItem('test.push.key', JSON.stringify(Array.from(new Uint8Array(options.applicationServerKey))));
        return wrap(options);
      };
      PushManager.prototype.getSubscription = async () => {
        const stored = localStorage.getItem('test.push.key');
        return stored ? wrap({ applicationServerKey: new Uint8Array(JSON.parse(stored)).buffer }) : null;
      };
    }, fixture.subscription);
    await page.goto('/settings');
    await page.getByText('连接遇到问题？', { exact: true }).click();
    const mobileLink = page.getByRole('link', { name: '打开移动端入口' });
    await expect(mobileLink).toHaveAttribute('href', 'https://localhost:19746/mobile/');
    const mobileURL = await mobileLink.getAttribute('href');
    await expect(page.getByRole('button', { name: '生成直连信息', exact: true })).not.toBeVisible();
    await page.getByRole('button', { name: '生成配对二维码', exact: true }).click();
    await expect(page.getByAltText('用手机相机扫描，打开交易罗盘并申请配对')).toBeVisible();
    let clipboard = '';
    await page.exposeFunction('captureClipboard', (text: string) => { clipboard = text; });
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: { writeText: (text: string) => (window as any).captureClipboard(text) } }));
    await page.getByRole('button', { name: '复制配对链接', exact: true }).click();
    await expect.poll(() => clipboard.length).toBeGreaterThan(100);
    await page.getByText('连接遇到问题？', { exact: true }).click();
    await page.screenshot({ path: testInfo.outputPath('desktop-qr.png'), fullPage: true, animations: 'disabled' });
    const phone = await context.newPage();
    const cdp = await context.newCDPSession(phone);
    let registrationId = '';
    cdp.on('ServiceWorker.workerRegistrationUpdated', (event: any) => {
      for (const registration of event.registrations) if (registration.scopeURL === `${mobileURL}`) registrationId = registration.registrationId;
    });
    await cdp.send('ServiceWorker.enable');
    await phone.goto(clipboard);
    await expect(phone.getByRole('button', { name: '生成手机返回信息' })).toHaveCount(0);
    await expect.poll(() => new URL(phone.url()).hash).toBe('');
    await expect.poll(() => phone.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
    await expect.poll(() => registrationId).not.toBe('');
    await expect(phone.getByLabel('手机名称')).toHaveValue('我的手机');
    await phone.screenshot({ path: testInfo.outputPath('phone-connect.png'), fullPage: true, animations: 'disabled' });
    await phone.getByRole('button', { name: '连接这台电脑', exact: true }).click();
    const desktopCode = page.getByLabel('我的手机的配对码');
    await expect(desktopCode).toBeVisible();
    const code = (await desktopCode.textContent())!;
    await expect(phone.getByText(code, { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '数字一致，批准' })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('desktop-code.png'), fullPage: true, animations: 'disabled' });
    await phone.screenshot({ path: testInfo.outputPath('phone-code.png'), fullPage: true, animations: 'disabled' });
    await phone.getByLabel('电脑上的六位配对码').fill(String((Number(code) + 1) % 1000000).padStart(6, '0'));
    await expect(phone.getByRole('alert')).toContainText('配对码不正确');
    await phone.reload();
    await expect(phone.getByLabel('电脑上的六位配对码')).toBeVisible();
    await phone.getByLabel('电脑上的六位配对码').fill(code);
    await expect(phone.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    await expect(desktopCode).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('desktop-paired.png'), fullPage: true, animations: 'disabled' });
    for (const width of [320, 390]) {
      await phone.setViewportSize({ width, height: 844 });
      expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await phone.screenshot({ path: testInfo.outputPath('phone-sessions.png'), fullPage: true, animations: 'disabled' });
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    // The narrow rail must leave the conversation and composer usable on small screens.
    for (const viewport of [{ width: 320, height: 568 }, { width: 390, height: 420 }, { width: 390, height: 844 }]) {
      await phone.setViewportSize(viewport);
      await phone.getByLabel('消息', { exact: true }).fill('通过 PWA 继续这个会话');
      await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeInViewport();
      await expect(phone.getByLabel('消息', { exact: true })).toBeInViewport();
      await expect(phone.getByRole('button', { name: '连接', exact: true })).toBeInViewport();
      expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await phone.getByRole('button', { name: '发送', exact: true }).click();
    await expect(phone.getByText('已在电脑上的同一个会话收到：通过 PWA 继续这个会话')).toBeVisible();
    await phone.screenshot({ path: testInfo.outputPath('phone-chat.png'), fullPage: true, animations: 'disabled' });
    expect(await phone.evaluate(() => document.cookie)).not.toContain('__Host-compass-device');
    const cookie = (await context.cookies()).find(cookie => cookie.name === '__Host-compass-device')!;
    expect(cookie.httpOnly && cookie.secure && cookie.sameSite === 'Strict').toBe(true);
    expect(await phone.evaluate(() => JSON.stringify(localStorage))).not.toContain(cookie.value);
    await phone.getByRole('button', { name: '连接', exact: true }).click();
    const connectionChecks = await phone.evaluate(() => performance.getEntriesByType('resource').filter(entry => entry.name.includes('/pairing/status')).length);
    await phone.getByRole('button', { name: '刷新连接状态', exact: true }).click();
    await expect.poll(() => phone.evaluate(() => performance.getEntriesByType('resource').filter(entry => entry.name.includes('/pairing/status')).length)).toBeGreaterThan(connectionChecks);
    await expect(phone.getByText(/^已刷新 ·/)).toBeVisible();
    const connectionCard = phone.getByRole('region', { name: '电脑连接状态' });
    await connectionCard.getByText('连接管理', { exact: true }).click();
    await expect(connectionCard.getByRole('button', { name: '移除连接' })).toBeVisible();
    await expect(connectionCard.getByText('移除连接后，需要重新扫码。电脑中的会话会保留。')).toBeVisible();
    await connectionCard.getByText('连接管理', { exact: true }).click();
    await expect(phone.getByText('通知未收到？')).toHaveCount(0);
    await phone.getByRole('switch', { name: '手机通知', exact: true }).click();
    await expect(phone.getByRole('switch', { name: '手机通知', exact: true })).toBeChecked();
    await expect(phone.getByText('通知已开启')).toBeVisible();
    await page.getByText('通知检查', { exact: true }).click();
    await page.getByRole('button', { name: '发送测试通知' }).click();
    await expect(page.getByText('已发送，等待手机接收')).toBeVisible();
    const sent = await (await page.request.get('/test/pwa')).json();
    expect(sent.payloads).toHaveLength(1);
    await cdp.send('ServiceWorker.deliverPushMessage', { origin: 'https://localhost:19746', registrationId, data: JSON.stringify(sent.payloads[0]) });
    await expect(page.getByText('手机已接收')).toBeVisible();
    await expect.poll(() => phone.evaluate(async () => (await (await navigator.serviceWorker.ready).getNotifications()).length)).toBe(1);
    const cacheURLs = await phone.evaluate(async () => (await Promise.all((await caches.keys()).map(async name => (await (await caches.open(name)).keys()).map(request => request.url)))).flat());
    expect(cacheURLs.some(url => url.includes('/mobile/v1/'))).toBe(false);
    await phone.screenshot({ path: testInfo.outputPath('pwa-notifications.png'), fullPage: true, animations: 'disabled' });
    await context.setOffline(true);
    await phone.reload();
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await expect(phone.getByText('已在电脑上的同一个会话收到：通过 PWA 继续这个会话')).toBeVisible();
    await expect(phone.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
    await context.setOffline(false);
    await phone.reload();
    await expect(phone.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    await expect(page.getByRole('button', { name: '数字一致，批准' })).toHaveCount(0);
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await expect(phone.getByText('已在电脑上的同一个会话收到：通过 PWA 继续这个会话')).toBeVisible();
    await phone.getByRole('button', { name: '连接', exact: true }).click();
    await expect(phone.getByRole('switch', { name: '接收任务提醒' })).not.toBeChecked();
    // Existing test subscriptions stay test-only. A fresh task appears in the queue only after opt-in.
    await page.request.post('/test/task');
    await phone.getByRole('switch', { name: '接收任务提醒' }).check();
    await expect(page.getByText('已配对 · 任务提醒已开启', { exact: true })).toBeVisible();
    // Changing browser permission must update effective notification status
    // without silently changing the saved task preference.
    await context.clearPermissions();
    await phone.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect(phone.getByRole('switch', { name: '手机通知', exact: true })).not.toBeChecked();
    await expect(phone.getByText('提醒偏好已保留，开启手机通知后生效')).toBeVisible();
    await expect(page.getByText('已配对 · 任务提醒已开启', { exact: true })).toBeVisible();
    await context.grantPermissions(['notifications']);
    await phone.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect(phone.getByRole('switch', { name: '手机通知', exact: true })).toBeChecked();
    await expect(phone.getByRole('switch', { name: '接收任务提醒' })).toBeEnabled();
    await page.request.post('/test/task');
    await expect.poll(async () => (await (await page.request.get('/test/pwa')).json()).payloads.length).toBe(2);
    const taskPush = (await (await page.request.get('/test/pwa')).json()).payloads[1];
    expect(taskPush.kind).toBe('task');
    expect(JSON.stringify(taskPush)).not.toContain('手机页面资源检查完成');
    await cdp.send('ServiceWorker.deliverPushMessage', { origin: 'https://localhost:19746', registrationId, data: JSON.stringify(taskPush) });
    await expect.poll(() => phone.evaluate(async () => (await (await navigator.serviceWorker.ready).getNotifications()).some(n => n.data.kind === 'task'))).toBe(true);
    expect(await phone.evaluate(async () => (await (await navigator.serviceWorker.ready).getNotifications()).some(n => n.title === '交易罗盘任务消息' && n.data.kind === 'task'))).toBe(true);
    const worker = context.serviceWorkers().find(w => w.url().startsWith(mobileURL!))!;
    await phone.getByRole('button', { name: '会话', exact: true }).click();
    await phone.getByLabel('消息', { exact: true }).fill('收到通知时保留这份未发送草稿');
    await phone.getByRole('button', { name: '任务消息', exact: true }).click();
    await phone.getByRole('button', { name: '查看任务详情：收盘复盘已完成', exact: true }).click();
    await expect(phone.getByRole('dialog')).toBeVisible();
    expect(await clickTaskNotification(worker, taskPush.id, true)).toBeNull();
    await expect(phone.getByRole('dialog')).toHaveCount(0);
    await expect(phone.getByText('任务推送验证：手机页面资源检查完成。')).toHaveCount(2);
    await phone.getByRole('button', { name: '会话', exact: true }).click();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('收到通知时保留这份未发送草稿');
    await phone.getByRole('button', { name: '任务消息', exact: true }).click();
    await expect(phone.getByText('任务推送验证：手机页面资源检查完成。')).toHaveCount(2);
    await phone.screenshot({ path: testInfo.outputPath('phone-alerts.png'), fullPage: true, animations: 'disabled' });
    await phone.getByRole('button', { name: '连接', exact: true }).click();
    await expect(phone.getByRole('switch', { name: '接收任务提醒' })).toBeChecked();
    await phone.screenshot({ path: testInfo.outputPath('phone-settings.png'), fullPage: true, animations: 'disabled' });
    await phone.setViewportSize({ width: 320, height: 568 });
    expect(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await phone.getByRole('button', { name: '会话', exact: true }).click();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('收到通知时保留这份未发送草稿');
    await phone.setViewportSize({ width: 390, height: 844 });
    // Cold launch: simulate the OS opening only the application's home URL, losing ?view=notices.
    await page.request.post('/test/task');
    await expect.poll(async () => (await (await page.request.get('/test/pwa')).json()).payloads.length).toBe(3);
    const nextPush = (await (await page.request.get('/test/pwa')).json()).payloads[2];
    await cdp.send('ServiceWorker.deliverPushMessage', { origin: 'https://localhost:19746', registrationId, data: JSON.stringify(nextPush) });
    await expect.poll(() => phone.evaluate(async id => (await (await navigator.serviceWorker.ready).getNotifications()).some(n => n.tag === id), nextPush.id)).toBe(true);
    await phone.close();
    expect(await clickTaskNotification(worker, nextPush.id, false)).toBe(`${mobileURL}?view=notices`);
    const reopened = await context.newPage();
    await reopened.goto(mobileURL!);
    await expect(reopened.getByText('任务推送验证：手机页面资源检查完成。')).toHaveCount(3);
    await reopened.reload();
    await expect(reopened.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    await page.getByRole('button', { name: '移除', exact: true }).click();
    await page.getByRole('button', { name: '确认移除', exact: true }).click();
    await expect(reopened.getByRole('alert')).toContainText('连接已失效');
  } finally { await context.close(); await browser.close(); }
});

test('untrusted test certificate is rejected without the test-only trust override', async ({ page }) => {
  await expect(page.goto('https://localhost:19746/phone/')).rejects.toThrow(/ERR_CERT/);
});
