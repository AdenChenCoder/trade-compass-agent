// @vitest-environment jsdom
// @vitest-environment-options {"url":"https://computer.test.ts.net/mobile/"}
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { prepareStartupUpdate } from './startup-update';

const CURRENT = 'aaaaaaaaaaaa'; const NEXT = 'bbbbbbbbbbbb';
class Worker extends EventTarget {
  state: ServiceWorkerState = 'activated'; version = CURRENT;
  postMessage(_message: unknown, ports: { reply: (data: unknown) => void }[]) { ports[0].reply({ version: this.version }); }
  activate() { this.state = 'activated'; this.dispatchEvent(new Event('statechange')); }
}
let worker: Worker;
let registration: { active: Worker; installing: Worker | null; waiting: Worker | null; update: ReturnType<typeof vi.fn> };
let register: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers(); sessionStorage.clear();
  document.head.innerHTML = `<meta name="compass-version" content="${CURRENT}">`;
  worker = new Worker();
  registration = { active: worker, installing: null, waiting: null, update: vi.fn().mockResolvedValue(undefined) };
  register = vi.fn().mockResolvedValue(registration);
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: { register } });
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(true);
  vi.stubGlobal('MessageChannel', class {
    port1 = { onmessage: null as null | ((event: { data: unknown }) => void), close: vi.fn() };
    port2 = { close: vi.fn(), reply: (data: unknown) => { this.port1.onmessage?.({ data }); } };
  });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); delete (navigator as unknown as Record<string, unknown>).serviceWorker; });

it('checks the computer at startup and keeps the current document when already current', async () => {
  expect(await prepareStartupUpdate()).toBe(false);
  expect(registration.update).toHaveBeenCalledTimes(1);
});

it('waits for a completely installed version before requesting a startup reload', async () => {
  worker.version = NEXT; worker.state = 'installing'; registration.installing = worker;
  let finished = false;
  const result = prepareStartupUpdate().then(value => { finished = true; return value; });
  await vi.advanceTimersByTimeAsync(0);
  expect(finished).toBe(false);
  registration.installing = null; worker.activate();
  expect(await result).toBe(true);
});

it('falls back after four seconds and a late update cannot request an automatic reload', async () => {
  let resolve!: () => void;
  registration.update.mockReturnValue(new Promise<void>(done => { resolve = done; }));
  const result = prepareStartupUpdate();
  await vi.advanceTimersByTimeAsync(4000);
  expect(await result).toBe(false);
  worker.version = NEXT; resolve(); await vi.advanceTimersByTimeAsync(0);
  expect(sessionStorage.getItem('compass.startup-update')).toBeNull();
});

it('keeps the cached version when installation fails', async () => {
  const installing = new Worker(); installing.state = 'installing'; installing.version = NEXT;
  registration.installing = installing;
  const result = prepareStartupUpdate(); await vi.advanceTimersByTimeAsync(0);
  installing.state = 'redundant'; installing.dispatchEvent(new Event('statechange'));
  expect(await result).toBe(false);
});

it('starts offline immediately without waiting for a network check', async () => {
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
  expect(await prepareStartupUpdate()).toBe(false);
  expect(register).not.toHaveBeenCalled();
});

it('prevents a reload loop when the next navigation still receives the old page', async () => {
  worker.version = NEXT;
  expect(await prepareStartupUpdate()).toBe(true);
  expect(await prepareStartupUpdate()).toBe(false);
  document.querySelector('meta')!.content = NEXT;
  expect(await prepareStartupUpdate()).toBe(false);
  expect(sessionStorage.getItem('compass.startup-update')).toBeNull();
});
