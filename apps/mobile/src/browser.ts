// Same-origin HTTPS only. The server owns an HttpOnly credential; JS stores no bearer secret.
const CONNECTION = 'compass.browser.connection.v1';
class ConnectionUnavailable extends Error {
  constructor() { super('暂时无法连接电脑，正在等待连接恢复。请确认电脑已唤醒并联网。'); }
}
async function request(path: string, method = 'GET', body?: string) {
  if (!window.isSecureContext || location.protocol !== 'https:') throw new Error('请使用电脑配置的可信 HTTPS 地址打开');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, { method, body, signal: controller.signal,
      credentials: 'same-origin', redirect: 'error',
      headers: { 'Content-Type': 'application/json', 'X-Compass-PWA': '1' } });
    if (response.status >= 500) throw new ConnectionUnavailable();
    return { status: response.status, data: await response.json() };
  } catch (error) {
    if (controller.signal.aborted || error instanceof TypeError) throw new ConnectionUnavailable();
    throw error;
  } finally { clearTimeout(timer); }
}
export const BrowserCompass = {
  async connection() {
    try {
      const result = await request('/mobile/v1/browser/connection');
      if (result.status !== 200) throw new Error('此地址未开启 PWA 连接');
      try { localStorage.setItem(CONNECTION, JSON.stringify(result.data)); }
      catch { /* Optional offline cache must not block a valid online response. */ }
      return result.data;
    } catch (error) {
      if (error instanceof ConnectionUnavailable) {
        try {
          const cached = JSON.parse(localStorage.getItem(CONNECTION) || 'null');
          if (cached) return cached;
        } catch { /* Keep the actionable connection error if the cache is unavailable. */ }
      }
      throw error;
    }
  },
  pair(options: { invitation: string; name: string }) {
    const invite = JSON.parse(options.invitation);
    if (new URL(invite.endpoint).origin !== location.origin) throw new Error('请打开这台电脑的配对链接');
    return request('/mobile/v1/browser/claim', 'POST', JSON.stringify({ invitation: invite.invitation, name: options.name }));
  },
  request(options: { path: string; method: string; body?: string }) {
    if (!options.path.startsWith('/mobile/v1/') || options.path.includes('..')) throw new Error('请求无效');
    return request(options.path, options.method, options.body);
  },
  async forget() {
    const result = await request('/mobile/v1/browser/forget', 'POST');
    if (result.status !== 200) throw new Error('请连接电脑后移除配对；离线时可通过浏览器清除本站数据');
    localStorage.removeItem(CONNECTION);
    const registration = await navigator.serviceWorker.getRegistration();
    const subscription = await registration?.pushManager?.getSubscription();
    await subscription?.unsubscribe().catch(() => false);
  },
};
