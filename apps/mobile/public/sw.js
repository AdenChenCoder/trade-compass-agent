/* Only versioned application assets are cached. Session/API responses never enter CacheStorage. */
const VERSION = '__VERSION__';
const PREFIX = `compass-shell-${encodeURIComponent(new URL(self.registration.scope).pathname)}-`;
const CACHE = PREFIX + VERSION;
const ASSETS = __PRECACHE__;
const PEER_MODE = __PEER_MODE__;
const clientVersions = new Map();
self.addEventListener('install', event => {
  // Activate only after a complete shell is available. Never navigate a live document.
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    await self.clients.claim();
    for (const client of await scopedWindows()) client.postMessage({ type: 'compass-shell-updated', version: VERSION });
  })());
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || !url.href.startsWith(self.registration.scope)) return;
  if (url.pathname.startsWith('/mobile/v1/')) return;
  const notificationEntry = event.request.mode === 'navigate' && url.pathname === new URL(self.registration.scope).pathname
    && url.searchParams.get('view') === 'notices' && [...url.searchParams].length === 1;
  if (url.search && !notificationEntry) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const request = notificationEntry ? self.registration.scope : event.request;
    // Navigation must use this worker's shell, not whichever old cache was created first.
    // Old hashed chunks remain available while a previous document is still open.
    const cached = await cache.match(request) || (event.request.mode !== 'navigate' ? await caches.match(request) : null);
    return cached || fetch(event.request);
  })());
});
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let payload = {};
    try { payload = event.data?.json() || {}; } catch { /* Always show a visible notification. */ }
    const task = payload.kind === 'task';
    await self.registration.showNotification(task ? '交易罗盘任务消息' : '交易罗盘连接测试', {
      body: task ? '电脑有一条新的任务结果。打开交易罗盘查看。' : '这是你的电脑发送的测试通知。点击返回交易罗盘。',
      icon: './icon-192.png', tag: /^[a-f0-9]{32}$/.test(payload.id) ? payload.id : 'compass-test',
      data: { kind: task ? 'task' : 'test' },
    });
    if (PEER_MODE && /^[a-f0-9]{32}$/.test(payload.id)) await receipts('put', payload.id);
    if (!PEER_MODE && /^[a-f0-9]{32}$/.test(payload.id)) {
      try {
        await fetch('/mobile/v1/push/received', { method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-Compass-PWA': '1' }, body: JSON.stringify({ id: payload.id }) });
      } catch { /* A notification can arrive while the phone cannot reach the computer. */ }
    }
  })());
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const home = new URL('./', self.registration.scope).href;
    const task = event.notification.data?.kind === 'task';
    const id = crypto.randomUUID().replaceAll('-', '');
    // Save only an explicit click. iOS may resume an existing document or drop an opening URL's query.
    if (task) await notificationNavigation('put', id).catch(() => null);
    const windows = (await self.clients.matchAll({ type: 'window', includeUncontrolled: true }))
      .filter(client => client.url.startsWith(home));
    const existing = windows.find(client => client.focused) || windows.find(client => client.visibilityState === 'visible') || windows[0];
    if (existing) {
      if (task) try { existing.postMessage({ type: 'compass-open-notices', id }); } catch { /* Document is not live yet. */ }
      try { return await existing.focus(); } catch { /* The resumed document can consume the saved click once ready. */ }
    }
    const opened = await self.clients.openWindow(task ? `${home}?view=notices` : home);
    if (task && opened) try { opened.postMessage({ type: 'compass-open-notices', id }); } catch { /* Read on startup. */ }
  })());
});

async function notificationNavigation(action, id) {
  const db = await new Promise((resolve, reject) => {
    const open = indexedDB.open(`compass-navigation:${new URL(self.registration.scope).pathname}`, 1);
    open.onupgradeneeded = () => open.result.createObjectStore('navigation');
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(open.error);
  });
  try {
    return await new Promise((resolve, reject) => {
      const transaction = db.transaction('navigation', 'readwrite');
      const store = transaction.objectStore('navigation');
      const request = store.get('pending');
      let result = null;
      request.onsuccess = () => {
        const saved = request.result;
        if (saved && Date.now() - saved.created_at < 5 * 60 * 1000) result = saved;
        else store.delete('pending');
        if (action === 'put') store.put({ id, created_at: Date.now() }, 'pending');
        if (action === 'ack' && saved?.id === id) store.delete('pending');
      };
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error);
    });
  } finally { db.close(); }
}

// Static hosting cannot call the computer while the PWA is suspended. Store
// only receipt IDs locally and deliver them after the next direct connection.
async function receipts(action, id) {
  const db = await new Promise((resolve, reject) => {
    const open = indexedDB.open('compass-push-receipts', 1);
    open.onupgradeneeded = () => open.result.createObjectStore('receipts', { keyPath: 'id' });
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(open.error);
  });
  try {
    return await new Promise((resolve, reject) => {
      const transaction = db.transaction('receipts', 'readwrite');
      const store = transaction.objectStore('receipts');
      const get = store.getAll();
      let result = [];
      get.onsuccess = () => {
        result = get.result;
        if (action === 'put') {
          for (const row of result.slice(0, Math.max(0, result.length - 99))) store.delete(row.id);
          store.put({ id });
        } else if (action === 'delete') store.delete(id);
      };
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error);
    });
  } finally { db.close(); }
}
self.addEventListener('message', event => {
  if (event.ports[0] && event.source?.url?.startsWith(self.registration.scope)) {
    if (event.data?.type === 'compass-shell-version') {
      if (/^[a-f0-9]{12}$/.test(event.data.clientVersion)) clientVersions.set(event.source.id, event.data.clientVersion);
      event.ports[0].postMessage({ version: VERSION });
      event.waitUntil(cleanOldShells());
      return;
    }
    if (event.data?.type === 'compass-navigation-get') {
      event.waitUntil(notificationNavigation('get').then(value => event.ports[0].postMessage(value))
        .catch(() => event.ports[0].postMessage(null)));
      return;
    }
    if (event.data?.type === 'compass-navigation-ack' && /^[a-f0-9]{32}$/.test(event.data.id)) {
      event.waitUntil(notificationNavigation('ack', event.data.id).then(() => event.ports[0].postMessage(true))
        .catch(() => event.ports[0].postMessage(false)));
      return;
    }
  }
  if (!PEER_MODE || !event.ports[0]) return;
  if (event.data?.type === 'compass-receipts') {
    event.waitUntil(receipts('list').then(rows => event.ports[0].postMessage(rows)).catch(() => event.ports[0].postMessage([])));
  } else if (event.data?.type === 'compass-receipt-ack' && /^[a-f0-9]{32}$/.test(event.data.id)) {
    event.waitUntil(receipts('delete', event.data.id).then(() => event.ports[0].postMessage(true)));
  }
});

async function scopedWindows() {
  return (await self.clients.matchAll({ type: 'window', includeUncontrolled: true }))
    .filter(client => client.url.startsWith(self.registration.scope));
}
async function cleanOldShells() {
  const windows = await scopedWindows();
  // An unknown/legacy document may still need its lazy-loaded chunks. A page
  // reload is an explicit user action, not a reason to discard those resources early.
  if (!windows.length || windows.some(client => clientVersions.get(client.id) !== VERSION)) return;
  for (const id of clientVersions.keys()) if (!windows.some(client => client.id === id)) clientVersions.delete(id);
  if (self.registration.installing || self.registration.waiting) return;
  const names = await caches.keys();
  const currentIndex = names.indexOf(CACHE);
  if (currentIndex < 0) return;
  // Never prune caches created by an update that is being prepared after us.
  for (const name of names.slice(0, currentIndex)) {
    if (self.registration.installing || self.registration.waiting) return;
    if (name.startsWith(PREFIX)) await caches.delete(name);
    else if (/^compass-shell-[a-f0-9]{12}$/.test(name)) {
      // Older releases shared a cache across /phone/ and /mobile/. Leave the
      // other installation's entries, credentials and all user stores untouched.
      const cache = await caches.open(name);
      for (const request of await cache.keys()) if (request.url.startsWith(self.registration.scope)) await cache.delete(request);
      if (!(await cache.keys()).length) await caches.delete(name);
    }
  }
}
