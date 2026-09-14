import { test, expect, chromium } from '@playwright/test';

test('existing cached install upgrades without removing pairing, drafts or an open old page', async ({ page }, testInfo) => {
  const fixture = await (await page.request.get('/test/pwa')).json();
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [`--ignore-certificate-errors-spki-list=${fixture.spki}`] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  let temporaryDevice: string | undefined;
  const release = async (name: string) => { expect((await page.request.post('/test/release', { data: { release: name } })).ok()).toBe(true); };
  try {
    await release('legacy');
    const phone = await context.newPage();
    await phone.goto('https://localhost:19746/mobile/');
    await expect.poll(() => phone.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
    // Establish the already-authorized phone using the original desktop approval contract.
    const invite = await (await page.request.post('/api/mobile/pairing/invitations')).json();
    const claim = await phone.evaluate(async invitation => (await fetch('/mobile/v1/browser/claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Compass-PWA': '1' },
      body: JSON.stringify({ invitation, name: '原来的手机' }),
    })).json(), invite.invitation);
    temporaryDevice = claim.device_id;
    const device = (await (await page.request.get('/api/mobile/devices')).json()).devices.find((d: any) => d.device_id === claim.device_id);
    expect((await page.request.post(`/api/mobile/devices/${device.device_id}/approve`, { data: { verification_code: device.verification_code } })).ok()).toBe(true);
    await phone.reload();
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await phone.getByLabel('消息', { exact: true }).fill('升级前保留的草稿');
    const originalCookies = await context.cookies();
    const oldCSS = await phone.locator('link[rel="stylesheet"]').getAttribute('href');
    const oldWorker = await phone.evaluate(() => navigator.serviceWorker.controller!.scriptURL);
    // Keep a second old document alive, as can happen with a suspended installed PWA.
    const other = await context.newPage(); await other.goto('https://localhost:19746/mobile/');
    await expect(other.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    const otherScope = await context.newPage(); await otherScope.goto('https://localhost:19746/phone/');
    await expect.poll(() => otherScope.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
    await expect(otherScope.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    await release('before-fix');
    await phone.evaluate(async () => { await (await navigator.serviceWorker.getRegistration())!.update(); });
    await expect.poll(() => phone.evaluate(async () => !!(await navigator.serviceWorker.getRegistration())?.waiting)).toBe(true);
    await phone.reload();
    expect(await phone.locator('link[rel="stylesheet"]').getAttribute('href')).toBe(oldCSS);
    await expect.poll(() => phone.evaluate(async () => !!(await navigator.serviceWorker.getRegistration())?.waiting)).toBe(true);
    // Let this document finish its bounded startup before publishing another fixture release.
    await expect(phone.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    // Reproduced: installing new files and refreshing did not replace the cached application.
    await release('current');
    await phone.evaluate(async () => { await (await navigator.serviceWorker.getRegistration())!.update(); });
    await expect.poll(() => phone.evaluate(() => new Promise<boolean>(resolve => {
      const channel = new MessageChannel();
      const timer = setTimeout(() => { channel.port1.close(); resolve(false); }, 300);
      channel.port1.onmessage = event => { clearTimeout(timer); channel.port1.close(); resolve(/^[a-f0-9]{12}$/.test(event.data?.version)); };
      navigator.serviceWorker.controller!.postMessage({ type: 'compass-shell-version' }, [channel.port2]);
    }))).toBe(true);
    // A reload uses the new complete shell even though the other old page remains open.
    await phone.reload();
    await expect(phone.locator('meta[name="compass-version"]')).toHaveAttribute('content', /^[a-f0-9]{12}$/);
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('升级前保留的草稿');
    expect(await context.cookies()).toEqual(originalCookies);
    expect(await phone.evaluate(() => navigator.serviceWorker.controller!.scriptURL)).toBe(oldWorker);
    // Already-open old clients must still be able to load their hashed assets.
    expect(await other.evaluate(async href => (await fetch(href!)).ok, oldCSS)).toBe(true);
    await other.close();
    await phone.getByLabel('消息', { exact: true }).fill('发现更新时，仍保留当前草稿');
    const currentVersion = await phone.locator('meta[name="compass-version"]').getAttribute('content');
    await release('incomplete');
    await phone.evaluate(async () => { await (await navigator.serviceWorker.getRegistration())!.update(); });
    await expect.poll(() => phone.evaluate(async () => !(await navigator.serviceWorker.getRegistration())?.installing)).toBe(true);
    await expect(phone.getByRole('button', { name: '确定', exact: true })).toHaveCount(0);
    expect(await phone.locator('meta[name="compass-version"]').getAttribute('content')).toBe(currentVersion);
    await release('next');
    await phone.evaluate(async () => { await (await navigator.serviceWorker.getRegistration())!.update(); });
    await expect(phone.getByRole('button', { name: '确定', exact: true })).toBeVisible();
    await expect(phone.locator('.app-update')).toHaveText('检测到新的版本，是否更新？确定');
    await expect(phone.locator('.app-update button')).toHaveCount(1);
    expect(await phone.locator('meta[name="compass-version"]').getAttribute('content')).toBe(currentVersion);
    await phone.screenshot({ path: testInfo.outputPath('update-ready.png'), animations: 'disabled' });
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('发现更新时，仍保留当前草稿');
    await phone.getByRole('button', { name: '确定', exact: true }).click();
    await expect(phone.locator('meta[name="compass-version"]')).toHaveAttribute('content', 'eeeeeeeeeeee');
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('发现更新时，仍保留当前草稿');
    await expect(phone.getByRole('button', { name: '确定', exact: true })).toHaveCount(0);
    expect(await context.cookies()).toEqual(originalCookies);
    const history = await (await page.request.get('/api/agent/sessions/shared-demo/messages')).json();
    expect(history.messages.some((m: any) => m.content.includes('升级前保留') || m.content.includes('发现更新时'))).toBe(false);
    await context.setOffline(true);
    await otherScope.reload();
    await expect(otherScope.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    await phone.reload();
    await phone.getByRole('button', { name: /消费行业观察/ }).click();
    await expect(phone.getByLabel('消息', { exact: true })).toHaveValue('发现更新时，仍保留当前草稿');
  } finally {
    await release('current');
    if (temporaryDevice) await page.request.delete(`/api/mobile/devices/${temporaryDevice}`);
    await context.close(); await browser.close();
  }
});

test('a fresh document loads the new version automatically, while a live or resumed document waits for confirmation', async ({ page }) => {
  const fixture = await (await page.request.get('/test/pwa')).json();
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [`--ignore-certificate-errors-spki-list=${fixture.spki}`] });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const home = 'https://localhost:19746/mobile/';
  const release = async (name: string) => { expect((await page.request.post('/test/release', { data: { release: name } })).ok()).toBe(true); };
  let deviceId: string | undefined;
  try {
    await release('current');
    const original = await context.newPage(); await original.goto(home);
    const invite = await (await page.request.post('/api/mobile/pairing/invitations')).json();
    const claim = await original.evaluate(async invitation => (await fetch('/mobile/v1/browser/claim', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Compass-PWA': '1' },
      body: JSON.stringify({ invitation, name: '启动更新测试手机' }),
    })).json(), invite.invitation);
    deviceId = claim.device_id;
    const device = (await (await page.request.get('/api/mobile/devices')).json()).devices.find((row: any) => row.device_id === deviceId);
    await page.request.post(`/api/mobile/devices/${deviceId}/approve`, { data: { verification_code: device.verification_code } });
    await original.reload();
    await original.getByRole('button', { name: /消费行业观察/ }).click();
    await original.getByLabel('消息', { exact: true }).fill('关闭后重新打开，保留这份草稿');
    const currentVersion = await original.locator('meta[name="compass-version"]').getAttribute('content');
    const cookies = await context.cookies();
    await original.close();
    await release('next');
    const reopened = await context.newPage();
    await reopened.addInitScript(() => {
      // Record only versions which actually expose an interactive app, across a startup reload.
      new MutationObserver(() => {
        if (!document.querySelector('.app-shell')) return;
        const version = document.querySelector<HTMLMetaElement>('meta[name="compass-version"]')?.content;
        const seen: string[] = JSON.parse(sessionStorage.getItem('test-interactive-versions') || '[]');
        if (version && !seen.includes(version)) sessionStorage.setItem('test-interactive-versions', JSON.stringify([...seen, version]));
      }).observe(document, { childList: true, subtree: true });
    });
    await reopened.goto(home);
    await expect(reopened.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    await expect(reopened.locator('meta[name="compass-version"]')).toHaveAttribute('content', 'eeeeeeeeeeee');
    expect(await reopened.evaluate(() => JSON.parse(sessionStorage.getItem('test-interactive-versions') || '[]'))).toEqual(['eeeeeeeeeeee']);
    await expect(reopened.locator('.app-update')).toHaveCount(0);
    expect(await context.cookies()).toEqual(cookies);
    await reopened.getByRole('button', { name: /消费行业观察/ }).click();
    await expect(reopened.getByLabel('消息', { exact: true })).toHaveValue('关闭后重新打开，保留这份草稿');
    // A continuously open app checks periodically without a foreground or online event.
    await reopened.clock.install();
    await reopened.evaluate(() => { (window as any).liveDocumentMarker = 'still-open'; });
    await release('current');
    await reopened.clock.fastForward(61_000);
    await expect(reopened.getByRole('button', { name: '确定', exact: true })).toBeVisible();
    await reopened.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
    expect(await reopened.evaluate(() => (window as any).liveDocumentMarker)).toBe('still-open');
    await expect(reopened.locator('meta[name="compass-version"]')).toHaveAttribute('content', 'eeeeeeeeeeee');
    await expect(reopened.getByLabel('消息', { exact: true })).toHaveValue('关闭后重新打开，保留这份草稿');
    await reopened.close();
    // A partially deployed release must not replace the last working shell at startup.
    await release('incomplete');
    const fallback = await context.newPage(); await fallback.goto(home);
    await expect(fallback.getByRole('button', { name: /消费行业观察/ })).toBeVisible();
    await expect(fallback.locator('meta[name="compass-version"]')).toHaveAttribute('content', currentVersion!);
    await expect(fallback.locator('.app-update')).toHaveCount(0);
    await fallback.close();
    await context.setOffline(true);
    const offline = await context.newPage(); await offline.goto(home);
    await offline.getByRole('button', { name: /消费行业观察/ }).click();
    await expect(offline.getByLabel('消息', { exact: true })).toHaveValue('关闭后重新打开，保留这份草稿');
    await expect(offline.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  } finally {
    await release('current');
    if (deviceId) await page.request.delete(`/api/mobile/devices/${deviceId}`);
    await context.close(); await browser.close();
  }
});
