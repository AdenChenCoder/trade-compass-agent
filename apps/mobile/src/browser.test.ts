// @vitest-environment jsdom
// @vitest-environment-options {"url":"https://computer.test.ts.net/mobile/"}
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { BrowserCompass } from './browser';

const key = 'compass.browser.connection.v1';
const connection = { connected: true, endpoint: location.origin, computer_id: 'original-computer' };
beforeEach(() => {
  vi.stubGlobal('isSecureContext', true);
  localStorage.clear();
  localStorage.setItem(key, JSON.stringify(connection));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it.each([502, 503, 504])('keeps the original pairing when the gateway returns %s during reopening', async status => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('Gateway unavailable', { status })));
  expect(await BrowserCompass.connection()).toEqual(connection);
  expect(JSON.parse(localStorage.getItem(key)!)).toEqual(connection);
});

it('ends a silent request and leaves a submitted message available for explicit retry', async () => {
  vi.useFakeTimers();
  const fetch = vi.fn((_path, options) => new Promise((_resolve, reject) => {
    options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
  }));
  vi.stubGlobal('fetch', fetch);
  const pending = JSON.stringify({ request_id: 'original-request', message: 'original draft' });
  let error: unknown;
  void BrowserCompass.request({ path: '/mobile/v1/turns', method: 'POST', body: pending }).catch(value => { error = value; });
  await vi.advanceTimersByTimeAsync(15000);
  expect(error).toBeInstanceOf(Error);
  expect((error as Error).message).toContain('暂时无法连接电脑');
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][1].body).toBe(pending);
});

it('uses an explicit disconnected response instead of falling back to an old authorization', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ ...connection, connected: false })));
  expect(await BrowserCompass.connection()).toEqual({ ...connection, connected: false });
});

it('preserves authentication rejection and never retries the request itself', async () => {
  const fetch = vi.fn().mockResolvedValue(Response.json({ detail: 'Unauthorized' }, { status: 401 }));
  vi.stubGlobal('fetch', fetch);
  expect(await BrowserCompass.request({ path: '/mobile/v1/sessions', method: 'GET' }))
    .toEqual({ status: 401, data: { detail: 'Unauthorized' } });
  expect(fetch).toHaveBeenCalledTimes(1);
});


it.each([true, false])('honors connected=%s despite denied cache writes', async connected => {
  const response = { ...connection, connected };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(response)));
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Quota', 'QuotaExceededError'); });
  expect(await BrowserCompass.connection()).toEqual(response);
});

it.each(['corrupt', 'denied'])('preserves the connection recovery error when the offline cache is %s', async failure => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
  if (failure === 'corrupt') localStorage.setItem(key, '{invalid');
  else vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('Denied', 'SecurityError'); });
  await expect(BrowserCompass.connection()).rejects.toThrow('暂时无法连接电脑');
});
