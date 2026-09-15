// @vitest-environment jsdom
// @vitest-environment-options {"url":"https://computer.test.ts.net/mobile/"}
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from './App';

const computer = { connected: true, endpoint: location.origin, computer_id: 'original' };
const prefix = 'compass.v1.original.';
const session = { session_id: 'session-one', title: '原会话', preview: '原始内容', updated_at: '2026-09-14' };
const page = { session_id: session.session_id, title: session.title, has_active_turn: false,
  messages: [{ role: 'assistant', content: '原始内容' }], page: { start_index: 0, next_before: null } };
let element: HTMLDivElement; let root: Root; let network: boolean; let loseReply: boolean;
let posts: { request_id: string; message: string }[];
let transcript: typeof page.messages; let receiptStatus: string;
let failBefore: number | null; let beforeRequests: number[];
let pendingGap: Promise<void> | null;
const settle = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(0); }); };
const click = async (name: string) => {
  const button = [...element.querySelectorAll('button')].find(node => (node.getAttribute('aria-label') || node.textContent)?.includes(name));
  expect(button).toBeTruthy();
  await act(async () => { button!.click(); }); await settle();
};
const draft = async (text: string) => {
  const input = element.querySelector('textarea')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, text);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};

beforeEach(() => {
  vi.useFakeTimers(); network = false; loseReply = false; posts = [];
  transcript = page.messages; receiptStatus = 'completed'; failBefore = null; beforeRequests = [];
  pendingGap = null;
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('scrollTo', vi.fn());
  vi.stubGlobal('matchMedia', () => ({ matches: false }));
  localStorage.clear(); sessionStorage.clear();
  localStorage.setItem('compass.browser.connection.v1', JSON.stringify(computer));
  localStorage.setItem(prefix + 'sessions', JSON.stringify([session]));
  localStorage.setItem(prefix + 'page.' + session.session_id, JSON.stringify(page));
  vi.stubGlobal('fetch', vi.fn(async (path: string, options: RequestInit) => {
    expect(options.credentials).toBe('same-origin');
    if (!network) return new Response('Gateway unavailable', { status: 502 });
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body as string));
      if (loseReply) { network = false; throw new TypeError('connection lost after acceptance'); }
      if (path.endsWith('/turns')) return Response.json({ ...posts.at(-1), status: 'running' });
    }
    if (path.endsWith('/browser/connection')) return Response.json(computer);
    if (path.endsWith('/pairing/status')) return Response.json({ status: 'approved', verification_code: '123456' });
    if (path.includes('/messages')) {
      const query = new URL(path, location.origin).searchParams;
      const before = query.has('before') ? Number(query.get('before')) : transcript.length;
      if (query.has('before')) {
        beforeRequests.push(before);
        if (pendingGap) await pendingGap;
        if (before === failBefore) return new Response('Unavailable', { status: 502 });
      }
      const start = Math.max(0, before - Number(query.get('limit')));
      return Response.json({ ...page, messages: transcript.slice(start, before),
        page: { start_index: start, next_before: start || null } });
    }
    if (path.includes('/turns/')) return Response.json({ ...posts[0], status: receiptStatus });
    if (path.includes('/notifications')) return Response.json([]);
    if (path.endsWith('/push')) return Response.json({ subscribed: true, tasks_enabled: true, last_test: null, last_task: null });
    return Response.json({ sessions: [session] });
  }));
  element = document.createElement('div'); document.body.append(element); root = createRoot(element);
});
afterEach(async () => {
  await act(async () => root.unmount()); element.remove(); vi.unstubAllGlobals(); vi.useRealTimers();
});

it.each([false, true])('never renders pairing while a returning device is loading (cached history: %s)', async hasHistory => {
  network = true;
  if (!hasHistory) localStorage.removeItem(prefix + 'sessions');
  const request = vi.mocked(fetch).getMockImplementation()!;
  let connectionReply!: (response: Response) => void;
  let pairingReply!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn((path: string, options: RequestInit) => {
    if (path.endsWith('/browser/connection')) return new Promise<Response>(resolve => { connectionReply = resolve; });
    if (path.endsWith('/pairing/status')) return new Promise<Response>(resolve => { pairingReply = resolve; });
    return request(path, options);
  }));
  let sawPairing = false;
  const observer = new MutationObserver(() => { sawPairing ||= !!element.querySelector('.onboarding, .connect-view'); });
  observer.observe(element, { childList: true, subtree: true });
  try {
    await act(async () => root.render(createElement(App))); await settle();
    expect(element.textContent).toContain('正在打开交易罗盘');
    expect(element.querySelector('.app-shell')).toBeNull();
    await act(async () => connectionReply(Response.json(computer))); await settle();
    expect(element.querySelector('.connect-view')).toBeNull();
    if (!hasHistory) expect(element.querySelector('.app-shell')).toBeNull();
    await act(async () => pairingReply(Response.json({ status: 'approved' }))); await settle();
    expect(element.querySelector('.sessions-view')).not.toBeNull();
    expect(element.querySelector('[aria-label="新建会话"]')).not.toBeNull();
    expect(sawPairing).toBe(false);
    expect(posts).toHaveLength(0);
  } finally { observer.disconnect(); }
});

it('shows first-use pairing only after an explicit disconnected response', async () => {
  let reply!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { reply = resolve; })));
  await act(async () => root.render(createElement(App))); await settle();
  expect(element.querySelector('.connect-view')).toBeNull();
  await act(async () => reply(Response.json({ ...computer, connected: false }))); await settle();
  expect(element.querySelector('.connect-view')).not.toBeNull();
  expect(element.querySelector('input[placeholder="我的手机"]')).not.toBeNull();
});

it('restores the connection tab without showing pairing while authorization is loading', async () => {
  network = true;
  sessionStorage.setItem('compass.resume-after-update', JSON.stringify({ computerId: computer.computer_id,
    tab: 'connection', selected: null, createdAt: Date.now() }));
  const request = vi.mocked(fetch).getMockImplementation()!;
  let reply!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn((path: string, options: RequestInit) => path.endsWith('/pairing/status')
    ? new Promise<Response>(resolve => { reply = resolve; }) : request(path, options)));
  let sawPairing = false;
  const observer = new MutationObserver(() => { sawPairing ||= !!element.querySelector('.pairing-form, .connection-illustration'); });
  observer.observe(element, { childList: true, subtree: true });
  try {
    await act(async () => root.render(createElement(App))); await settle();
    expect(element.textContent).toContain('正在恢复连接');
    await act(async () => reply(Response.json({ status: 'approved' }))); await settle();
    expect(element.querySelector('.service-card')).not.toBeNull();
    expect(sawPairing).toBe(false);
  } finally { observer.disconnect(); }
});

it('shows recovery instead of pairing when the saved device has no cached history and is offline', async () => {
  localStorage.removeItem(prefix + 'sessions');
  await act(async () => root.render(createElement(App))); await settle();
  expect(element.textContent).toContain('暂时无法连接电脑');
  expect(element.querySelector('.connect-view')).toBeNull();
  network = true;
  await click('重试');
  expect(element.querySelector('.sessions-view')).not.toBeNull();
  expect(element.querySelector('.connect-view')).toBeNull();
  expect(posts).toHaveLength(0);
});

it.each([true, false])('recovers an unavailable origin without reload or pairing (cached connection: %s)', async cached => {
  if (!cached) localStorage.removeItem('compass.browser.connection.v1');
  await act(async () => root.render(createElement(App))); await settle();
  expect(element.textContent).toContain('暂时无法连接电脑');
  if (cached) { await click('原会话'); await draft('断网期间保存的草稿'); }
  network = true;
  await act(async () => { window.dispatchEvent(new Event('online')); window.dispatchEvent(new Event('pageshow')); });
  await settle();
  expect(element.querySelector('[role="status"][aria-label="已连接"]')).not.toBeNull();
  expect(element.textContent).not.toContain('暂时无法连接电脑');
  expect(element.textContent).toContain('原会话');
  if (cached) expect(element.querySelector('textarea')!.value).toBe('断网期间保存的草稿');
  expect(posts).toHaveLength(0);
  expect(JSON.parse(localStorage.getItem('compass.browser.connection.v1')!)).toEqual(computer);
});

it.each(['completed', 'interrupted'])('clears only the sent draft after recovering a %s receipt', async status => {
  receiptStatus = status;
  network = true;
  await act(async () => root.render(createElement(App))); await settle();
  await click('原会话'); await draft('  仅发送一次  ');
  loseReply = true;
  await click('发送');
  expect(posts).toHaveLength(1);
  const pending = JSON.parse(localStorage.getItem(prefix + 'request.' + session.session_id)!);
  expect(pending.request_id).toBe(posts[0].request_id);
  network = true;
  await act(async () => window.dispatchEvent(new Event('pageshow'))); await settle();
  expect(element.querySelector('[role="status"][aria-label="已连接"]')).not.toBeNull();
  expect(localStorage.getItem(prefix + 'request.' + session.session_id)).toBeNull();
  expect(element.querySelector('textarea')!.value).toBe('');
  expect(JSON.parse(localStorage.getItem(prefix + 'draft.' + session.session_id)!)).toBe('');
  await click('发送');
  expect(posts).toHaveLength(1);
});

it.each(['completed', 'interrupted', 'failed', 'unknown'])('preserves a newer saved draft when recovering a %s receipt', async status => {
  network = true; receiptStatus = status;
  const outstanding = { request_id: 'original-request-123', session_id: session.session_id, message: '已经发送' };
  posts = [outstanding];
  localStorage.setItem(prefix + 'request.' + session.session_id, JSON.stringify(outstanding));
  localStorage.setItem(prefix + 'draft.' + session.session_id, JSON.stringify('后来写的新草稿'));
  await act(async () => root.render(createElement(App))); await settle();
  await click('原会话');
  expect(element.querySelector('textarea')!.value).toBe('后来写的新草稿');
  expect(JSON.parse(localStorage.getItem(prefix + 'draft.' + session.session_id)!)).toBe('后来写的新草稿');
  expect(localStorage.getItem(prefix + 'request.' + session.session_id)).toBeNull();
  expect(posts).toHaveLength(1);
});

it.each(['failed', 'unknown'])('keeps the original message available for an uncertain %s result', async status => {
  network = true; receiptStatus = status;
  await act(async () => root.render(createElement(App))); await settle();
  await click('原会话'); await draft('请先核对执行结果');
  loseReply = true; await click('发送'); network = true;
  await act(async () => window.dispatchEvent(new Event('pageshow'))); await settle();
  expect(element.querySelector('textarea')!.value).toBe('请先核对执行结果');
  expect(posts).toHaveLength(1);
});

const history = (count: number) => Array.from({ length: count }, (_, index) => ({ role: 'assistant', content: `历史消息 ${index}` }));
const visibleHistory = () => [...element.querySelectorAll('.message .markdown')].map(node => node.textContent?.trim());

it.each([60, 150, 275])('fills history gaps on reconnect to %s messages without losing the draft', async count => {
  network = true; transcript = history(50);
  await act(async () => root.render(createElement(App))); await settle();
  await click('原会话'); await draft('补齐历史时保留草稿');
  transcript = history(count);
  await act(async () => window.dispatchEvent(new Event('pageshow'))); await settle();
  expect(visibleHistory()).toEqual(transcript.map(message => message.content));
  expect(element.querySelector('textarea')!.value).toBe('补齐历史时保留草稿');
  expect(posts).toHaveLength(0);
});

it('keeps distinct transcript entries with identical text when filling a gap', async () => {
  const repeated = { role: 'assistant', content: '合法的重复消息' };
  network = true; transcript = Array.from({ length: 50 }, () => repeated);
  await act(async () => root.render(createElement(App))); await settle(); await click('原会话');
  transcript = Array.from({ length: 175 }, () => repeated);
  await act(async () => window.dispatchEvent(new Event('pageshow'))); await settle();
  expect(visibleHistory()).toEqual(transcript.map(message => message.content));
});

it('keeps the last continuous history when gap retrieval fails and retries after recovery', async () => {
  network = true; transcript = history(50);
  await act(async () => root.render(createElement(App))); await settle(); await click('原会话');
  const originalCache = localStorage.getItem(prefix + 'page.' + session.session_id);
  transcript = history(250); failBefore = 150;
  await act(async () => window.dispatchEvent(new Event('pageshow'))); await settle();
  expect(beforeRequests).toContain(150);
  expect(visibleHistory()).toEqual(history(50).map(message => message.content));
  expect(localStorage.getItem(prefix + 'page.' + session.session_id)).toBe(originalCache);
  failBefore = null;
  await act(async () => window.dispatchEvent(new Event('pageshow'))); await settle();
  expect(visibleHistory()).toEqual(transcript.map(message => message.content));
});

it('repairs a legacy cache with a hidden gap without clearing offline history first', async () => {
  localStorage.setItem(prefix + 'page.' + session.session_id, JSON.stringify({ ...page,
    messages: [...history(50), ...history(150).slice(100)], page: { start_index: 0, next_before: null } }));
  await act(async () => root.render(createElement(App))); await settle(); await click('原会话');
  expect(visibleHistory()).toHaveLength(100);
  network = true; transcript = history(150);
  await act(async () => window.dispatchEvent(new Event('pageshow'))); await settle();
  expect(visibleHistory()).toEqual(transcript.map(message => message.content));
  const repaired = JSON.parse(localStorage.getItem(prefix + 'page.' + session.session_id)!);
  expect(repaired.messages).toHaveLength(150);
});

it('stops filling a gap after leaving the conversation and discards late responses', async () => {
  network = true; transcript = history(50);
  await act(async () => root.render(createElement(App))); await settle(); await click('原会话');
  const originalCache = localStorage.getItem(prefix + 'page.' + session.session_id);
  let release!: () => void;
  pendingGap = new Promise<void>(resolve => { release = resolve; });
  transcript = history(250);
  await act(async () => window.dispatchEvent(new Event('pageshow'))); await settle();
  expect(beforeRequests).toEqual([200]);
  await click('任务消息');
  pendingGap = null;
  await act(async () => release()); await settle();
  expect(beforeRequests).toEqual([200]);
  expect(localStorage.getItem(prefix + 'page.' + session.session_id)).toBe(originalCache);
  expect(element.textContent).toContain('还没有任务消息');
  await click('会话');
  expect(visibleHistory()).toEqual(transcript.map(message => message.content));
});

it('clears the stale notification-status error after connectivity returns', async () => {
  network = true;
  await act(async () => root.render(createElement(App))); await settle();
  await click('连接');
  network = false;
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(element.textContent).toContain('暂时无法读取通知状态');
  network = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(element.textContent).not.toContain('暂时无法读取通知状态');
});

it.each(['failed', 'unknown', 'completed', 'interrupted'])('keeps a polled %s result when an earlier acceptance arrives late', async status => {
  network = true; receiptStatus = status;
  const existingFetch = fetch;
  let accept!: (value: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (path: string, options: RequestInit) => {
    if (path.endsWith('/turns') && options.method === 'POST') {
      posts.push(JSON.parse(options.body as string));
      return await new Promise<Response>(resolve => { accept = resolve; });
    }
    return existingFetch(path, options);
  }));
  await act(async () => root.render(createElement(App))); await settle();
  await click('原会话'); await draft('等待确认的消息'); await click('发送');
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(localStorage.getItem(prefix + 'request.' + session.session_id)).toBeNull();
  const before = element.textContent;
  await act(async () => { accept(Response.json({ ...posts[0], status: 'running' })); }); await settle();
  const expectedDraft = ['failed', 'unknown'].includes(status) ? '等待确认的消息' : '';
  expect(element.querySelector('textarea')!.value).toBe(expectedDraft);
  expect(JSON.parse(localStorage.getItem(prefix + 'draft.' + session.session_id)!)).toBe(expectedDraft);
  // Only the send button changes as its HTTP wait ends; terminal notices remain.
  for (const notice of ['这次执行未成功', '无法确定这条请求是否完成', '这次执行已停止']) {
    expect(element.textContent!.includes(notice)).toBe(before!.includes(notice));
  }
  expect(element.textContent).not.toContain('正在处理');
  expect(posts).toHaveLength(1);
});

it('ignores a late network error after polling confirms completion', async () => {
  network = true;
  const existingFetch = fetch;
  let reject!: (reason: Error) => void;
  vi.stubGlobal('fetch', vi.fn(async (path: string, options: RequestInit) => {
    if (path.endsWith('/turns') && options.method === 'POST') {
      posts.push(JSON.parse(options.body as string));
      return await new Promise<Response>((_, fail) => { reject = fail; });
    }
    return existingFetch(path, options);
  }));
  await act(async () => root.render(createElement(App))); await settle();
  await click('原会话'); await draft('已经执行'); await click('发送');
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  await act(async () => { reject(new TypeError('reply lost')); }); await settle();
  expect(element.querySelector('[role="alert"]')).toBeNull();
  expect(element.querySelector('textarea')!.value).toBe('');
  expect(localStorage.getItem(prefix + 'request.' + session.session_id)).toBeNull();
  expect(posts).toHaveLength(1);
});

it.each(['failed', 'unknown'])('preserves the message when retrying an existing %s receipt', async status => {
  network = true;
  await act(async () => root.render(createElement(App))); await settle();
  await click('原会话'); await draft('请核对执行结果');
  loseReply = true; await click('发送'); network = true;
  const original = posts[0];
  vi.stubGlobal('fetch', vi.fn(async (path: string, options: RequestInit) => {
    expect(path.endsWith('/turns')).toBe(true);
    const retry = JSON.parse(options.body as string); posts.push(retry);
    return Response.json({ ...retry, status });
  }));
  await click('确认并重试原消息');
  expect(posts).toEqual([original, original]);
  expect(element.querySelector('textarea')!.value).toBe('请核对执行结果');
  expect(localStorage.getItem(prefix + 'request.' + session.session_id)).toBeNull();
});

it('keeps explicitly loaded history when the next sync fails and the app reopens offline', async () => {
  network = true; transcript = history(150);
  localStorage.removeItem(prefix + 'page.' + session.session_id);
  await act(async () => root.render(createElement(App))); await settle(); await click('原会话');
  expect(visibleHistory()).toEqual(history(150).slice(100).map(m => m.content));
  const existingFetch = fetch;
  vi.stubGlobal('fetch', vi.fn(async (path: string, options: RequestInit) => {
    const response = await existingFetch(path, options);
    if (path.includes('before=100')) network = false;
    return response;
  }));
  await click('查看更早消息');
  const expected = history(150).slice(50).map(m => m.content);
  expect(visibleHistory()).toEqual(expected);
  await click('所有会话'); await click('原会话');
  expect(visibleHistory()).toEqual(expected);
  await act(async () => root.unmount()); root = createRoot(element);
  await act(async () => root.render(createElement(App))); await settle(); await click('原会话');
  expect(visibleHistory()).toEqual(expected);
  expect(posts).toHaveLength(0);
});

it('refreshes the actual computer status and reports pending, success and failure without changing pairing', async () => {
  network = true;
  await act(async () => root.render(createElement(App))); await settle(); await click('连接');
  const originalFetch = fetch;
  let respond!: (value: Response) => void;
  let checks = 0;
  vi.stubGlobal('fetch', vi.fn((path: string, options: RequestInit) => {
    if (path.endsWith('/pairing/status')) { checks += 1; return new Promise<Response>(resolve => { respond = resolve; }); }
    return originalFetch(path, options);
  }));
  await click('刷新连接状态');
  const refreshButton = element.querySelector<HTMLButtonElement>('button[aria-label="刷新连接状态"]')!;
  expect(refreshButton.disabled).toBe(true);
  expect(refreshButton.textContent).toContain('刷新中');
  expect(element.querySelector('.refresh-result')?.textContent).toBe('');
  await click('刷新连接状态');
  expect(checks).toBe(1);
  await act(async () => respond(Response.json({ status: 'approved', name: '重命名后的手机' })));
  expect(refreshButton.disabled).toBe(false);
  expect(element.querySelector('.refresh-result')?.textContent).toContain('已刷新');
  expect(element.querySelector('.device-facts')?.textContent).toContain('重命名后的手机');
  await click('刷新连接状态');
  await act(async () => respond(new Response('Unavailable', { status: 502 })));
  expect(refreshButton.disabled).toBe(false);
  expect(element.querySelector('.refresh-result')?.textContent).toBe('刷新失败，请重试');
  expect(element.querySelector('.service-status')?.textContent).toBe('正在重连');
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  await act(async () => respond(Response.json({ status: 'approved', name: '重命名后的手机' })));
  expect(element.querySelector('.service-status')?.textContent).toBe('已连接');
  expect(element.querySelector('.refresh-result')?.textContent).not.toContain('失败');
  expect(JSON.parse(localStorage.getItem('compass.browser.connection.v1')!)).toEqual(computer);
  expect(posts).toHaveLength(0);
});

it.each([0, 1])('keeps the create button with %s existing sessions and creates once per completed click', async count => {
  network = true;
  let available = count ? [session] : [];
  const originalFetch = fetch;
  let resolve!: (value: Response) => void;
  let creates = 0;
  vi.stubGlobal('fetch', vi.fn((path: string, options: RequestInit) => {
    if (path.endsWith('/sessions') && options.method === 'POST') {
      creates += 1; return new Promise<Response>(done => { resolve = done; });
    }
    if (path.endsWith('/sessions?limit=100')) return Promise.resolve(Response.json({ sessions: available }));
    if (path.includes('/messages') && !path.includes(session.session_id)) {
      const id = path.split('/sessions/')[1].split('/messages')[0];
      return Promise.resolve(Response.json({ ...page, session_id: id, title: null, messages: [] }));
    }
    return originalFetch(path, options);
  }));
  await act(async () => root.render(createElement(App))); await settle();
  for (let index = 1; index <= 2; index += 1) {
    await click('新建会话');
    const button = element.querySelector<HTMLButtonElement>('[aria-label="新建会话"]')!;
    expect(button.disabled).toBe(true);
    await click('新建会话');
    expect(creates).toBe(index);
    const created = { ...session, session_id: `created-${index}`, title: '', preview: '' };
    available = [created, ...available];
    await act(async () => resolve(Response.json({ session_id: created.session_id, updated_at: created.updated_at })));
    await settle();
    expect(button.disabled).toBe(false);
    expect(element.textContent).toContain('今天想研究什么？');
    expect(element.querySelector('textarea')!.value).toBe('');
  }
  await click('所有会话');
  expect(element.querySelectorAll('.session-list > button')).toHaveLength(count + 2);
  expect(element.querySelector('[aria-label="新建会话"]')).not.toBeNull();
});


it('keeps an unsaved draft visible and blocks navigation until storage can save it', async () => {
  network = true;
  await act(async () => root.render(createElement(App))); await settle(); await click('原会话');
  const blocked = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
  try {
    await draft('这段没有保存成功的研究问题');
    await click('所有会话');
    expect(element.querySelector('textarea')!.value).toBe('这段没有保存成功的研究问题');
    expect(element.querySelector('[role="alert"]')?.textContent).toContain('草稿暂时无法保存');
    await click('新建会话');
    expect(posts).toHaveLength(0);
    const updating = new Event('compass-before-update', { cancelable: true });
    await act(async () => { window.dispatchEvent(updating); });
    expect(updating.defaultPrevented).toBe(true);
  } finally { blocked.mockRestore(); }
  await click('所有会话'); await click('原会话');
  expect(element.querySelector('textarea')!.value).toBe('这段没有保存成功的研究问题');
  expect(element.querySelector('[role="alert"]')).toBeNull();
});

it('keeps the connection online after a session is deleted and carries the draft into a new session', async () => {
  network = true;
  await act(async () => root.render(createElement(App))); await settle(); await click('原会话');
  await draft('删除会话前未发送的内容');
  const request = vi.mocked(fetch).getMockImplementation()!;
  let messageReads = 0;
  vi.stubGlobal('fetch', vi.fn((path: string, options: RequestInit) => {
    if (path.endsWith('/sessions') && options.method === 'POST') return Promise.resolve(Response.json({ ...session, session_id: 'replacement' }));
    if (path.endsWith('/sessions?limit=100')) return Promise.resolve(Response.json({ sessions: [] }));
    if (path.includes('/replacement/messages')) return Promise.resolve(Response.json({ ...page, session_id: 'replacement', messages: [] }));
    if (path.includes('/messages')) { messageReads += 1; return Promise.resolve(Response.json({ detail: 'session not found' }, { status: 404 })); }
    return request(path, options);
  }));
  await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
  expect(element.querySelector('[role="status"][aria-label="已连接"]')).not.toBeNull();
  expect(element.querySelector<HTMLButtonElement>('[aria-label="新建会话"]')?.disabled).toBe(false);
  expect(element.textContent).toContain('这段会话已删除');
  expect(element.querySelector('textarea')!.value).toBe('删除会话前未发送的内容');
  expect(element.querySelector<HTMLButtonElement>('[aria-label="发送"]')?.disabled).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(4100); });
  expect(messageReads).toBe(1);
  await click('用草稿新建会话');
  expect(element.textContent).toContain('今天想研究什么');
  expect(element.querySelector('textarea')!.value).toBe('删除会话前未发送的内容');
  expect(element.querySelector<HTMLButtonElement>('[aria-label="发送"]')?.disabled).toBe(false);
  expect(posts).toHaveLength(0);
});

it('loads older sessions and keeps them accessible across refreshes, opening and returning', async () => {
  network = true;
  const first = Array.from({ length: 100 }, (_, index) => ({ ...session, session_id: `recent-${index}`, title: `最近会话 ${index}` }));
  const last = { ...session, session_id: 'oldest', title: '最早的研究' };
  const request = vi.mocked(fetch).getMockImplementation()!;
  let rejectNextPage = true;
  vi.stubGlobal('fetch', vi.fn((path: string, options: RequestInit) => {
    if (path.includes('/sessions?limit=100')) {
      if (path.includes('cursor=')) return Promise.resolve(rejectNextPage ? new Response('Unavailable', { status: 502 }) : Response.json({ sessions: [last], next_cursor: null }));
      return Promise.resolve(Response.json({ sessions: first, next_cursor: 'second/page+=' }));
    }
    if (path.includes('/oldest/messages')) return Promise.resolve(Response.json({ ...page, session_id: 'oldest', title: last.title }));
    return request(path, options);
  }));
  await act(async () => root.render(createElement(App))); await settle();
  expect(element.querySelectorAll('.session-list > button')).toHaveLength(100);
  await click('查看更早会话');
  expect(element.querySelectorAll('.session-list > button')).toHaveLength(100);
  rejectNextPage = false;
  await act(async () => { window.dispatchEvent(new Event('online')); }); await settle();
  await click('查看更早会话');
  expect(element.querySelectorAll('.session-list > button')).toHaveLength(101);
  expect(element.textContent).not.toContain('查看更早会话');
  await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
  expect(element.querySelectorAll('.session-list > button')).toHaveLength(101);
  await click('最早的研究');
  expect(element.textContent).toContain('原始内容');
  expect(element.textContent).not.toContain('这段会话已删除');
  await click('所有会话');
  expect(element.querySelectorAll('.session-list > button')).toHaveLength(101);
  expect(vi.mocked(fetch).mock.calls.some(call => String(call[0]).includes('cursor=second%2Fpage%2B%3D'))).toBe(true);
});


it('opens the online paired app even when the optional connection cache cannot be written', async () => {
  network = true;
  const original = Storage.prototype.setItem;
  const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    if (key === 'compass.browser.connection.v1') throw new DOMException('Quota exceeded', 'QuotaExceededError');
    return original.call(this, key, value);
  });
  try {
    await act(async () => root.render(createElement(App))); await settle();
    expect(element.querySelector('.sessions-view')).not.toBeNull();
    await click('原会话');
    expect(element.textContent).toContain('原始内容');
    expect(element.querySelector('.connect-view')).toBeNull();
    expect(posts).toHaveLength(0);
  } finally { spy.mockRestore(); }
});
