import { useEffect, useState } from 'react';
import { Capacitor } from '@capacitor/core';

// A different worker can take over without replacing the JavaScript already
// executing in the document. Compare both versions; never force a live reload.
export function AppUpdate() {
  const [available, setAvailable] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (Capacitor.isNativePlatform() || location.protocol !== 'https:' || !('serviceWorker' in navigator)) return;
    const version = document.querySelector<HTMLMetaElement>('meta[name="compass-version"]')?.content;
    let active = true;
    let registration: ServiceWorkerRegistration | undefined;
    let checking = false;
    let lastCheck = 0;
    const ports = new Set<MessageChannel>();
    function compare(candidate: unknown) {
      if (active && typeof candidate === 'string' && /^[a-f0-9]{12}$/.test(candidate)) setAvailable(!!version && candidate !== version);
    }
    function readVersion() {
      const worker = navigator.serviceWorker.controller || registration?.active;
      if (!worker) return;
      const channel = new MessageChannel(); ports.add(channel);
      const close = () => { clearTimeout(timeout); channel.port1.close(); channel.port2.close(); ports.delete(channel); };
      const timeout = setTimeout(close, 3000);
      channel.port1.onmessage = event => { compare(event.data?.version); close(); };
      worker.postMessage({ type: 'compass-shell-version', clientVersion: version }, [channel.port2]);
    }
    async function check() {
      readVersion();
      if (!registration || checking || Date.now() - lastCheck < 60_000) return;
      checking = true; lastCheck = Date.now();
      try { await registration.update(); } catch { /* Offline use stays available. */ }
      finally { checking = false; if (active) readVersion(); }
    }
    const visible = () => { if (document.visibilityState !== 'hidden') void check(); };
    const wake = () => { void check(); };
    const message = (event: MessageEvent) => {
      if (event.source === navigator.serviceWorker.controller && event.data?.type === 'compass-shell-updated') {
        compare(event.data.version); readVersion();
      }
    };
    navigator.serviceWorker.addEventListener('controllerchange', readVersion);
    navigator.serviceWorker.addEventListener('message', message);
    window.addEventListener('online', wake); window.addEventListener('pageshow', wake);
    document.addEventListener('visibilitychange', visible);
    const timer = setInterval(visible, 60_000);
    void navigator.serviceWorker.register('./sw.js', { scope: './', updateViaCache: 'none' }).then(value => {
      if (active) { registration = value; void check(); }
    }).catch(() => { if (active) window.dispatchEvent(new Event('compass-install-failed')); });
    return () => {
      active = false;
      clearInterval(timer);
      navigator.serviceWorker.removeEventListener('controllerchange', readVersion);
      navigator.serviceWorker.removeEventListener('message', message);
      window.removeEventListener('online', wake); window.removeEventListener('pageshow', wake);
      document.removeEventListener('visibilitychange', visible);
      for (const channel of ports) { channel.port1.close(); channel.port2.close(); }
    };
  }, []);
  if (!available) return null;
  return <aside className="app-update" role="status"><div><strong>检测到新的版本，是否更新？</strong>{error ? <span>{error}</span> : null}</div>
    <button onClick={() => {
      const event = new Event('compass-before-update', { cancelable: true });
      if (!window.dispatchEvent(event)) { setError('暂时无法保存当前页面，请稍后重试。'); return; }
      location.reload();
    }}>确定</button></aside>;
}

export function restoreUpdateView(computerId?: string) {
  if (!computerId) return null;
  try {
    const raw = sessionStorage.getItem('compass.resume-after-update');
    if (!raw) return null;
    const value = JSON.parse(raw);
    sessionStorage.removeItem('compass.resume-after-update');
    if (value.computerId !== computerId || !Number.isFinite(value.createdAt) || Date.now() - value.createdAt > 300_000
        || !['sessions', 'notices', 'connection'].includes(value.tab)
        || (value.selected !== null && typeof value.selected !== 'string')) return null;
    return value as { tab: 'sessions' | 'notices' | 'connection'; selected: string | null };
  } catch { return null; }
}
