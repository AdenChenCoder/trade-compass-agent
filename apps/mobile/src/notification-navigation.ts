// A notification click can arrive before React mounts or while the PWA is suspended.
// The worker retains that intent until the mounted app acknowledges it.
type Click = { id: string };
const valid = (value: unknown): value is Click => !!value && typeof value === 'object'
  && 'id' in value && typeof value.id === 'string' && /^[a-f0-9]{32}$/.test(value.id);

async function exchange(message: { type: string; id?: string }): Promise<unknown> {
  const registration = await navigator.serviceWorker.getRegistration();
  const worker = registration?.active;
  if (!worker) return null;
  return new Promise(resolve => {
    const channel = new MessageChannel();
    const finish = (value: unknown) => { clearTimeout(timer); channel.port1.close(); resolve(value); };
    const timer = setTimeout(() => finish(null), 2000);
    channel.port1.onmessage = event => finish(event.data);
    worker.postMessage(message, [channel.port2]);
  });
}

export function listenForNotificationNavigation(open: (id: string) => void) {
  if (!('serviceWorker' in navigator)) return () => {};
  let active = true;
  const receive = (value: unknown) => { if (active && valid(value)) open(value.id); };
  const pull = () => { void exchange({ type: 'compass-navigation-get' }).then(receive).catch(() => {}); };
  const visible = () => { if (document.visibilityState === 'visible') pull(); };
  const message = (event: MessageEvent) => {
    const worker = event.source;
    if (worker && 'scriptURL' in worker && worker.scriptURL === new URL('./sw.js', location.href).href
        && event.data?.type === 'compass-open-notices') receive(event.data);
  };
  navigator.serviceWorker.addEventListener('message', message);
  navigator.serviceWorker.addEventListener('controllerchange', pull);
  document.addEventListener('visibilitychange', visible);
  window.addEventListener('focus', pull);
  window.addEventListener('pageshow', pull);
  pull();
  void navigator.serviceWorker.ready.then(() => { if (active) pull(); });
  return () => {
    active = false;
    navigator.serviceWorker.removeEventListener('message', message);
    navigator.serviceWorker.removeEventListener('controllerchange', pull);
    document.removeEventListener('visibilitychange', visible);
    window.removeEventListener('focus', pull);
    window.removeEventListener('pageshow', pull);
  };
}

export function acknowledgeNotificationNavigation(id: string) {
  return exchange({ type: 'compass-navigation-ack', id }).catch(() => null);
}
