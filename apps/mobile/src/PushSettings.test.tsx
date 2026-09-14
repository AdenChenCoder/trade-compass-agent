// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { PushSettings } from './PushSettings';
import { api } from './native';

vi.mock('./native', () => ({ api: vi.fn() }));
vi.mock('./peer', () => ({ peerMode: false }));
const key = new Uint8Array([1, 2, 3]);
const empty = { subscribed: false, tasks_enabled: false, public_key: 'AQID', last_test: null, last_task: null };
let server = { ...empty };
let element: HTMLDivElement; let root: Root;
const permission = vi.fn(); const unsubscribe = vi.fn(); const subscribe = vi.fn();
const subscription = { options: { applicationServerKey: key.buffer }, unsubscribe, toJSON: () => ({ endpoint: 'https://push.example.test/one' }) };
const switchFor = (name: string) => element.querySelector<HTMLInputElement>(`input[aria-label="${name}"]`)!;
const toggle = async (name: string) => { await act(async () => { switchFor(name).click(); }); };
const mount = async () => { await act(async () => root.render(createElement(PushSettings))); };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }

beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks(); server = { ...empty };
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('matchMedia', () => ({ matches: true }));
  vi.stubGlobal('Notification', { permission: 'granted', requestPermission: permission });
  vi.stubGlobal('PushManager', class {});
  permission.mockResolvedValue('granted'); unsubscribe.mockResolvedValue(true); subscribe.mockResolvedValue(subscription);
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: {
    getRegistration: vi.fn().mockResolvedValue({ active: {}, pushManager: {
      getSubscription: vi.fn().mockResolvedValue(subscription), subscribe,
    } }),
  } });
  vi.mocked(api).mockImplementation(async (path, method, body) => {
    if (path === 'push/subscription') server = { ...server, subscribed: true };
    else if (path === 'push/unsubscribe') server = { ...server, subscribed: false, tasks_enabled: false };
    else if (path === 'push/tasks') server = { ...server, tasks_enabled: (body as { enabled: boolean }).enabled };
    return { ...server };
  });
  element = document.createElement('div'); document.body.append(element); root = createRoot(element);
});
afterEach(async () => {
  await act(async () => root.unmount()); element.remove();
  delete (navigator as unknown as Record<string, unknown>).serviceWorker;
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers();
});

it('requests permission from the switch tap and keeps both controls disabled until enabling finishes', async () => {
  const requested = deferred<NotificationPermission>(); permission.mockReturnValueOnce(requested.promise);
  await mount();
  await act(async () => { switchFor('手机通知').click(); expect(permission).toHaveBeenCalledTimes(1); });
  expect(switchFor('手机通知').disabled).toBe(true);
  expect(switchFor('手机通知').checked).toBe(false);
  expect(switchFor('接收任务提醒').disabled).toBe(true);
  expect(vi.mocked(api).mock.calls.some(call => call[1] === 'POST')).toBe(false);
  await act(async () => requested.resolve('granted'));
  expect(switchFor('手机通知').checked).toBe(true);
  expect(switchFor('接收任务提醒').checked).toBe(false);
  expect(switchFor('接收任务提醒').disabled).toBe(false);
});

it('turns off task reminders with phone notifications and requires a new task opt-in after re-enabling', async () => {
  server = { ...server, subscribed: true, tasks_enabled: true }; await mount();
  await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(false);
  expect(switchFor('接收任务提醒').checked).toBe(false);
  expect(switchFor('接收任务提醒').disabled).toBe(true);
  expect(unsubscribe).toHaveBeenCalledTimes(1);
  await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(true);
  expect(switchFor('接收任务提醒').checked).toBe(false);
  expect(vi.mocked(api).mock.calls.some(call => call[0] === 'push/tasks')).toBe(false);
});

it('preserves enabled switches when the computer cannot stop notifications, with the error inside the phone setting', async () => {
  server = { ...server, subscribed: true, tasks_enabled: true }; await mount();
  vi.mocked(api).mockRejectedValueOnce(new Error('offline'));
  await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(true);
  expect(switchFor('接收任务提醒').checked).toBe(true);
  expect(unsubscribe).not.toHaveBeenCalled();
  expect(element.querySelector('.notification-setting [role="alert"]')?.textContent).toContain('关闭未完成');
});

it('shows that delivery stopped when browser cleanup fails after the computer disabled notifications', async () => {
  server = { ...server, subscribed: true, tasks_enabled: true }; await mount();
  unsubscribe.mockRejectedValueOnce(new Error('browser cleanup failed'));
  await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(false);
  expect(switchFor('接收任务提醒').checked).toBe(false);
  expect(element.querySelector('[role="alert"]')?.textContent).toContain('已停止通知');
});

it('keeps notifications off and explains denied permission in the phone setting', async () => {
  permission.mockResolvedValueOnce('denied'); await mount(); await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(false);
  expect(element.querySelector('.notification-setting [role="alert"]')?.textContent).toContain('系统设置');
  expect(vi.mocked(api).mock.calls.some(call => call[1] === 'POST')).toBe(false);
});

it('ignores an old poll arriving after the user disables notifications', async () => {
  server = { ...server, subscribed: true, tasks_enabled: true }; await mount();
  const previous = { ...server }; const delayed = deferred<typeof server>();
  vi.mocked(api).mockImplementationOnce(() => delayed.promise);
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  await toggle('手机通知');
  await act(async () => delayed.resolve(previous));
  expect(switchFor('手机通知').checked).toBe(false);
  expect(switchFor('接收任务提醒').checked).toBe(false);
});

it('restores the previous task preference on failure and blocks polling during the save', async () => {
  server = { ...server, subscribed: true }; await mount();
  const saving = deferred<typeof server>(); vi.mocked(api).mockImplementationOnce(() => saving.promise);
  await toggle('接收任务提醒');
  const reads = vi.mocked(api).mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(vi.mocked(api).mock.calls.length).toBe(reads);
  await act(async () => saving.resolve({ ...server, tasks_enabled: true }));
  vi.mocked(api).mockRejectedValueOnce(new Error('任务提醒设置未保存'));
  await toggle('接收任务提醒');
  expect(switchFor('接收任务提醒').checked).toBe(true);
  expect(element.querySelector('.notification-setting [role="alert"]')?.textContent).toBe('任务提醒设置未保存');
});

it('shows a disabled switch and an inline installation hint for iPhone browser tabs', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: false }));
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('iPhone');
  await mount();
  expect(switchFor('手机通知').disabled).toBe(true);
  expect(element.querySelector('.notification-setting')?.textContent).toContain('添加到主屏幕');
  expect(element.querySelector('details')).toBeNull();
  expect(permission).not.toHaveBeenCalled();
});


it('shows revoked system permission without changing the saved task preference, and recovers on resume', async () => {
  server = { ...server, subscribed: true, tasks_enabled: true };
  await mount();
  expect(switchFor('手机通知').checked).toBe(true);
  vi.stubGlobal('Notification', { permission: 'denied', requestPermission: permission });
  await act(async () => { window.dispatchEvent(new Event('pageshow')); });
  expect(switchFor('手机通知').checked).toBe(false);
  expect(element.textContent).not.toContain('通知已开启');
  expect(element.textContent).toContain('系统设置');
  expect(element.textContent).toContain('提醒偏好已保留');
  expect(switchFor('接收任务提醒').checked).toBe(true);
  expect(switchFor('接收任务提醒').disabled).toBe(true);
  expect(server.tasks_enabled).toBe(true);
  expect(vi.mocked(api).mock.calls.some(call => call[1] === 'POST')).toBe(false);
  vi.stubGlobal('Notification', { permission: 'granted', requestPermission: permission });
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
  expect(switchFor('手机通知').checked).toBe(true);
  expect(switchFor('接收任务提醒').disabled).toBe(false);
});

it('repairs a missing browser subscription without resetting the task preference', async () => {
  server = { ...server, subscribed: true, tasks_enabled: true };
  const registration = await navigator.serviceWorker.getRegistration();
  vi.mocked(registration!.pushManager.getSubscription).mockResolvedValue(null);
  await mount();
  expect(switchFor('手机通知').checked).toBe(false);
  expect(element.textContent).toContain('通知需要重新开启');
  expect(switchFor('接收任务提醒').disabled).toBe(true);
  await toggle('手机通知');
  expect(subscribe).toHaveBeenCalledTimes(1);
  expect(switchFor('手机通知').checked).toBe(true);
  expect(server.tasks_enabled).toBe(true);
});


function changedPushKey() {
  server = { ...server, public_key: 'BAUG' };
  const replacement = { ...subscription, options: { applicationServerKey: new Uint8Array([4, 5, 6]).buffer } };
  const getSubscription = vi.fn().mockResolvedValue(subscription);
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: {
    getRegistration: vi.fn().mockResolvedValue({ active: {}, pushManager: { getSubscription, subscribe } }),
  } });
  unsubscribe.mockImplementation(async () => { getSubscription.mockResolvedValue(null); return true; });
  subscribe.mockImplementation(async () => { getSubscription.mockResolvedValue(replacement); return replacement; });
  return { getSubscription, replacement };
}

it.each([false, true])('replaces an old browser push key without changing task preferences (server subscribed: %s)', async subscribed => {
  server = { ...server, subscribed, tasks_enabled: true };
  changedPushKey();
  await mount();
  expect(switchFor('手机通知').checked).toBe(false);
  expect(switchFor('接收任务提醒').checked).toBe(true);
  expect(switchFor('接收任务提醒').disabled).toBe(true);
  await toggle('手机通知');
  expect(unsubscribe).toHaveBeenCalledTimes(1);
  expect(subscribe).toHaveBeenCalledWith({ userVisibleOnly: true, applicationServerKey: new Uint8Array([4, 5, 6]) });
  expect(switchFor('手机通知').checked).toBe(true);
  expect(switchFor('接收任务提醒').disabled).toBe(false);
  expect(server.tasks_enabled).toBe(true);
  expect(vi.mocked(api).mock.calls.filter(call => call[1] === 'POST').map(call => call[0])).toEqual(['push/subscription']);
});

it('allows retry when removing an old browser subscription fails', async () => {
  changedPushKey(); unsubscribe.mockRejectedValueOnce(new Error('暂时无法恢复通知，请重试'));
  await mount(); await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(false);
  expect(element.querySelector('[role="alert"]')?.textContent).toContain('请重试');
  expect(subscribe).not.toHaveBeenCalled();
  await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(true);
});

it('does not reuse an old subscription that the browser failed to remove', async () => {
  changedPushKey(); unsubscribe.mockImplementationOnce(async () => false);
  await mount(); await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(false);
  expect(element.querySelector('[role="alert"]')?.textContent).toContain('通知恢复未完成');
  expect(subscribe).not.toHaveBeenCalled();
  expect(vi.mocked(api).mock.calls.some(call => call[1] === 'POST')).toBe(false);
  await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(true);
});

it('retries creating a replacement after the old subscription was removed', async () => {
  server = { ...server, tasks_enabled: true };
  changedPushKey(); subscribe.mockRejectedValueOnce(new Error('暂时无法订阅，请重试'));
  await mount(); await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(false);
  expect(server.tasks_enabled).toBe(true);
  await toggle('手机通知');
  expect(switchFor('手机通知').checked).toBe(true);
  expect(unsubscribe).toHaveBeenCalledTimes(1);
  expect(server.tasks_enabled).toBe(true);
});
