import { Capacitor } from '@capacitor/core';

const RELOAD_GUARD = 'compass.startup-update';
const STARTUP_TIMEOUT = 4000;
const validVersion = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{12}$/.test(value);

function waitForActivation(worker: ServiceWorker, signal: AbortSignal): Promise<boolean> {
  return new Promise(resolve => {
    const finish = (ready: boolean) => {
      worker.removeEventListener('statechange', changed); signal.removeEventListener('abort', aborted); resolve(ready);
    };
    const changed = () => {
      if (worker.state === 'activated') finish(true);
      else if (worker.state === 'redundant') finish(false);
    };
    const aborted = () => finish(false);
    worker.addEventListener('statechange', changed); signal.addEventListener('abort', aborted, { once: true });
    if (signal.aborted) aborted(); else changed();
  });
}

function shellVersion(worker: ServiceWorker, version: string, signal: AbortSignal): Promise<unknown> {
  return new Promise(resolve => {
    const channel = new MessageChannel();
    const finish = (value: unknown) => {
      clearTimeout(timer); channel.port1.close(); channel.port2.close(); signal.removeEventListener('abort', aborted); resolve(value);
    };
    const aborted = () => finish(null);
    const timer = setTimeout(aborted, 1000);
    signal.addEventListener('abort', aborted, { once: true });
    channel.port1.onmessage = event => finish(event.data?.version);
    if (signal.aborted) aborted();
    else try { worker.postMessage({ type: 'compass-shell-version', clientVersion: version }, [channel.port2]); }
    catch { finish(null); }
  });
}

// Called once per new document, before the interactive application is mounted.
// Background/BFCache resumes keep their document and never enter this path.
export async function prepareStartupUpdate(): Promise<boolean> {
  if (Capacitor.isNativePlatform() || location.protocol !== 'https:' || !('serviceWorker' in navigator) || navigator.onLine === false) return false;
  const version = document.querySelector<HTMLMetaElement>('meta[name="compass-version"]')?.content || '';
  if (!validVersion(version)) return false;
  const controller = new AbortController();
  const { signal } = controller;
  let timeout: ReturnType<typeof setTimeout>;
  const deadline = new Promise<false>(resolve => { timeout = setTimeout(() => { controller.abort(); resolve(false); }, STARTUP_TIMEOUT); });
  async function check() {
    const registration = await navigator.serviceWorker.register('./sw.js', { scope: './', updateViaCache: 'none' });
    if (signal.aborted) return false;
    await registration.update();
    if (signal.aborted) return false;
    const candidate = registration.installing || registration.waiting || registration.active;
    if (!candidate || !(await waitForActivation(candidate, signal)) || signal.aborted) return false;
    const ready = registration.active;
    if (!ready || ready.state !== 'activated') return false;
    const next = await shellVersion(ready, version, signal);
    if (signal.aborted || !validVersion(next)) return false;
    if (next === version) {
      try { sessionStorage.removeItem(RELOAD_GUARD); } catch { /* No reload needs guarding. */ }
      return false;
    }
    // A failed navigation/cache replacement must not trap the user in a reload loop.
    // If storage cannot provide this guard, keep the current UI and manual update.
    const previous = JSON.parse(sessionStorage.getItem(RELOAD_GUARD) || 'null');
    if (previous?.target === next && Date.now() - previous.at < 60_000) return false;
    sessionStorage.setItem(RELOAD_GUARD, JSON.stringify({ target: next, at: Date.now() }));
    return true;
  }
  try { return await Promise.race([check().catch(() => false), deadline]); }
  finally { clearTimeout(timeout!); controller.abort(); }
}
