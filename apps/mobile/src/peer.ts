// Shared static PWA: the hosting origin receives only static asset requests.
// Pairing credentials stay in browser storage and travel only inside DTLS.
const KEY = 'compass.peer.device.v1';
export const peerMode = import.meta.env.VITE_COMPASS_TRANSPORT === 'peer';
interface Device { computer_id: string; credential: string; paired: boolean }
interface Offer { protocol: string; peer_id: string; sdp: string; computer_id: string; invitation: string; expires_at: number }
interface Reply { status: number; data: unknown }
let pc: RTCPeerConnection | undefined;
let channel: RTCDataChannel | undefined;
let offer: Offer | undefined;
let device: Device | undefined;
const pending = new Map<string, { text: string; resolve: (reply: Reply) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
function stored(): Device | undefined {
  try { return JSON.parse(localStorage.getItem(KEY) || 'null') || undefined; } catch { return undefined; }
}
function disconnected() {
  for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('直连已断开，请在连接页面重新连接电脑')); }
  pending.clear();
}
function close() { disconnected(); channel?.close(); pc?.close(); channel = undefined; pc = undefined; }
export function parsePeerOffer(text: string): Offer {
  if (text.trim().startsWith('https://')) text = new URLSearchParams(new URL(text).hash.slice(1)).get('peer') || '';
  const value = JSON.parse(text);
  if (value.protocol !== 'compass-rtc-v1' || !/^[a-f0-9]{32}$/.test(value.peer_id)
      || typeof value.sdp !== 'string' || value.sdp.length > 65536 || !value.sdp.includes('a=fingerprint:sha-256 ')
      || typeof value.computer_id !== 'string' || value.computer_id.length > 100
      || !/^[A-Za-z0-9_-]{43}$/.test(value.invitation) || !Number.isFinite(value.expires_at)
      || value.expires_at * 1000 <= Date.now()
      || value.sdp.split(/\r?\n/).some((line: string) => line.startsWith('m=') && !line.startsWith('m=application '))) {
    throw new Error('电脑连接信息无效或已过期，请重新生成');
  }
  return value;
}
function rpc(options: { method: string; path: string; body?: string }): Promise<Reply> {
  if (channel?.readyState !== 'open' || !device) return Promise.reject(new Error('直连未建立，请在连接页面交换连接信息'));
  const id = crypto.randomUUID();
  const raw = JSON.stringify({ id, ...options, credential: device.credential });
  if (new TextEncoder().encode(raw).length > 65536 || pending.size >= 8 || channel.bufferedAmount > 262144) {
    return Promise.reject(new Error('请求过大或连接繁忙，请稍后重试'));
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('电脑尚未返回结果，请恢复连接后检查原请求')); }, 20000);
    pending.set(id, { text: '', resolve, reject, timer });
    try { channel!.send(raw); } catch { clearTimeout(timer); pending.delete(id); reject(new Error('直连已断开')); }
  });
}
export const PeerCompass = {
  async connection() {
    const saved = stored();
    return { connected: !!saved?.paired, computer_id: saved?.computer_id, endpoint: '电脑直连 · 同一 Wi-Fi' };
  },
  async prepare(text: string) {
    const next = parsePeerOffer(text);
    const saved = stored();
    if (saved && saved.computer_id !== next.computer_id) throw new Error('请先移除已有电脑连接，再配对另一台电脑');
    close(); offer = next;
    device = saved || { computer_id: next.computer_id, paired: false,
      credential: btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '') };
    // Explicitly no STUN/TURN service. This first validation supports LAN only.
    const current = new RTCPeerConnection({ iceServers: [] }); pc = current;
    current.ondatachannel = event => {
      if (pc !== current || event.channel.label !== 'compass-rtc-v1' || channel) { event.channel.close(); return; }
      channel = event.channel;
      channel.onclose = disconnected;
      channel.onmessage = event => {
        try {
          const part = JSON.parse(event.data);
          const item = pending.get(part.id);
          if (!item || typeof part.chunk !== 'string') return;
          item.text += part.chunk;
          if (item.text.length > 8 * 1024 * 1024) { close(); return; }
          if (part.end === true) { const result = JSON.parse(item.text); clearTimeout(item.timer); pending.delete(part.id); item.resolve(result); }
        } catch { close(); }
      };
    };
    try {
      await current.setRemoteDescription({ type: 'offer', sdp: next.sdp });
      await current.setLocalDescription(await current.createAnswer());
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => { current.onicegatheringstatechange = null; reject(new Error('准备连接超时，请确认网络后重试')); }, 12000);
        const check = () => { if (current.iceGatheringState === 'complete') { clearTimeout(timer); current.onicegatheringstatechange = null; resolve(); } };
        current.onicegatheringstatechange = check; check();
      });
      return JSON.stringify({ peer_id: next.peer_id, sdp: current.localDescription!.sdp });
    } catch (error) { close(); throw error; }
  },
  async finish(name: string) {
    if (!offer || !device) throw new Error('请先生成手机返回信息');
    const deadline = Date.now() + 15000;
    while (channel?.readyState !== 'open' && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 100));
    if (channel?.readyState !== 'open') throw new Error('尚未连通：请确认电脑已导入返回信息、两端同一 Wi-Fi，且网络允许设备互访');
    let result = await rpc({ method: 'GET', path: '/mobile/v1/pairing/status' });
    if (result.status === 401 && !device.paired) {
      // Save before sending so a lost reply can be recovered with the same secret.
      localStorage.setItem(KEY, JSON.stringify(device));
      result = await rpc({ method: 'POST', path: '/mobile/v1/pairing/claim', body: JSON.stringify({
        invitation: offer.invitation, name, device_secret: device.credential }) });
    }
    if (![200, 202].includes(result.status)) throw new Error('设备授权不可用，请移除旧连接并在电脑重新生成');
    device.paired = true; localStorage.setItem(KEY, JSON.stringify(device));
    return result;
  },
  pair(_options: { invitation: string; name: string }): Promise<Reply> { return Promise.reject(new Error('请使用直连配对步骤')); },
  request: rpc,
  async forget() {
    if (channel?.readyState === 'open') {
      const result = await rpc({ method: 'POST', path: '/mobile/v1/pairing/forget' });
      if (result.status !== 200 && result.status !== 401) throw new Error('撤销未完成，请在电脑设备列表中撤销');
    }
    // Offline removal only clears this browser. UI also directs the user to revoke on PC.
    close(); localStorage.removeItem(KEY); device = undefined; offer = undefined;
    const registration = await navigator.serviceWorker?.getRegistration();
    await (await registration?.pushManager?.getSubscription())?.unsubscribe().catch(() => false);
  },
};


function workerMessage(message: unknown): Promise<unknown> {
  return new Promise(resolve => {
    if (!navigator.serviceWorker?.controller) { resolve([]); return; }
    const ports = new MessageChannel();
    const timer = setTimeout(() => { ports.port1.close(); resolve([]); }, 1500);
    ports.port1.onmessage = event => { clearTimeout(timer); ports.port1.close(); resolve(event.data); };
    navigator.serviceWorker.controller.postMessage(message, [ports.port2]);
  });
}
export async function flushPushReceipts() {
  const rows = await workerMessage({ type: 'compass-receipts' });
  if (!Array.isArray(rows)) return;
  for (const row of rows) {
    if (!/^[a-f0-9]{32}$/.test(row.id)) continue;
    const result = await rpc({ method: 'POST', path: '/mobile/v1/push/received', body: JSON.stringify({ id: row.id }) });
    if (result.status === 200) await workerMessage({ type: 'compass-receipt-ack', id: row.id });
  }
}
