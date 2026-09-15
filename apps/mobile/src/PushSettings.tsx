import { useEffect, useRef, useState } from 'react';
import { peerMode, flushPushReceipts } from './peer';
import { api } from './native';

interface Status { subscribed: boolean; public_key: string; tasks_enabled: boolean }
interface BrowserStatus { permission: NotificationPermission; subscribed: boolean; publicKey: string | null }
function subscriptionKey(subscription: PushSubscription | null): string | null {
  const key = subscription?.options.applicationServerKey;
  return key ? btoa(String.fromCharCode(...new Uint8Array(key))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '') : null;
}
async function readBrowserStatus(): Promise<BrowserStatus> {
  const permission = 'Notification' in window ? Notification.permission : 'default';
  if (permission !== 'granted') return { permission, subscribed: false, publicKey: null };
  const registration = await navigator.serviceWorker?.getRegistration();
  const subscription = await registration?.pushManager.getSubscription() ?? null;
  return { permission, subscribed: !!subscription, publicKey: subscriptionKey(subscription) };
}
const STATUS_UNAVAILABLE = '暂时无法读取通知状态，请确认电脑仍在线';
const PERMISSION_BLOCKED = '通知未获允许，可在系统设置中调整';
export function PushSettings() {
  const [status, setStatus] = useState<Status | null>(null);
  const [browserStatus, setBrowserStatus] = useState<BrowserStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const revision = useRef(0);
  const changing = useRef(false);
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  const standalone = matchMedia('(display-mode: standalone)').matches || (navigator as Navigator & { standalone?: boolean }).standalone;
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  useEffect(() => {
    let active = true; let reading = false;
    async function refresh() {
      if (reading || changing.current) return;
      reading = true;
      const started = revision.current;
      try {
        if (peerMode) await flushPushReceipts();
        const [next, browser] = await Promise.all([api<Status>('push'), readBrowserStatus()]);
        if (active && started === revision.current) {
          setStatus(next); setBrowserStatus(browser);
          setError(previous => previous === STATUS_UNAVAILABLE || (browser.permission === 'granted' && previous === PERMISSION_BLOCKED) ? '' : previous);
        }
      }
      catch { if (active && started === revision.current) { setBrowserStatus(null); setError(STATUS_UNAVAILABLE); } }
      finally { reading = false; }
    }
    void refresh();
    const resume = () => { if (document.visibilityState !== 'hidden') void refresh(); };
    const timer = setInterval(() => { if (document.visibilityState !== 'hidden') void refresh(); }, 5000);
    window.addEventListener('pageshow', resume); window.addEventListener('online', resume);
    document.addEventListener('visibilitychange', resume);
    return () => {
      active = false; clearInterval(timer);
      window.removeEventListener('pageshow', resume); window.removeEventListener('online', resume);
      document.removeEventListener('visibilitychange', resume);
    };
  }, []);
  function beginChange() {
    if (changing.current) return false;
    changing.current = true; revision.current += 1; setBusy(true); setError(''); return true;
  }
  function endChange() {
    revision.current += 1; changing.current = false; setBusy(false);
  }
  async function enable() {
    if (!beginChange()) return;
    try {
      // Ask directly from the tap; iOS requires a user gesture.
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        setBrowserStatus({ permission, subscribed: false, publicKey: null });
        throw new Error(PERMISSION_BLOCKED);
      }
      const registration = await navigator.serviceWorker?.getRegistration();
      if (!registration?.active) throw new Error('主屏幕应用尚未准备好，请稍后重新打开');
      const current = await api<Status>('push');
      const key = Uint8Array.from(atob(current.public_key.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
      let subscription = await registration.pushManager.getSubscription();
      if (subscription && subscriptionKey(subscription) !== current.public_key.replace(/=+$/, '')) {
        // Replace only the browser subscription; explicit disabling also clears task preferences.
        await subscription.unsubscribe();
        subscription = await registration.pushManager.getSubscription();
        if (subscription && subscriptionKey(subscription) !== current.public_key.replace(/=+$/, '')) {
          throw new Error('通知恢复未完成，请稍后重试。');
        }
        setBrowserStatus({ permission, subscribed: false, publicKey: null });
      }
      subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      await api('push/subscription', 'POST', subscription.toJSON());
      setStatus(await api<Status>('push'));
      setBrowserStatus({ permission, subscribed: true, publicKey: subscriptionKey(subscription) });
    } catch (err) { setError(err instanceof Error ? err.message : '无法开启此浏览器的通知'); }
    finally { endChange(); }
  }
  async function disable() {
    if (!beginChange()) return;
    let stopped = false;
    try {
      await api('push/unsubscribe', 'POST');
      stopped = true;
      // The computer has stopped delivery, even if browser cleanup fails next.
      setStatus(value => value ? { ...value, subscribed: false, tasks_enabled: false } : value);
      const registration = await navigator.serviceWorker?.getRegistration();
      const subscription = await registration?.pushManager.getSubscription();
      await subscription?.unsubscribe(); setStatus(await api<Status>('push'));
    } catch { setError(stopped ? '已停止通知，手机订阅清理未完成，可稍后重新开启。' : '关闭未完成，请连接电脑后重试'); }
    finally { endChange(); }
  }
  async function setTasks(enabled: boolean) {
    if (!beginChange()) return;
    const previous = !!status?.tasks_enabled;
    setStatus(value => value ? { ...value, tasks_enabled: enabled } : value);
    try { setStatus(await api<Status>('push/tasks', 'POST', { enabled })); }
    catch (err) { setStatus(value => value ? { ...value, tasks_enabled: previous } : value);
      setError(err instanceof Error ? err.message : '任务提醒设置未保存'); }
    finally { endChange(); }
  }
  const canEnable = supported && (!ios || standalone);
  const availability = ios && !standalone ? '添加到主屏幕后，即可开启通知。'
    : !supported ? '此浏览器不支持通知，可在「任务消息」查看结果。' : '';
  const enabled = canEnable && !!status?.subscribed && browserStatus?.permission === 'granted' && browserStatus.subscribed
    && browserStatus.publicKey === status.public_key?.replace(/=+$/, '');
  const description = availability || (!status || !browserStatus ? error ? '通知状态暂不可用' : '正在读取通知状态…'
    : browserStatus.permission === 'denied' ? '通知权限已关闭，请在系统设置中允许通知。'
    : status.subscribed && !enabled ? '通知需要重新开启。'
    : enabled ? '通知已开启' : '通知未开启');
  return <section className="connection-card" aria-label="通知与提醒">
    <h3>通知与提醒</h3>
    <div className="notification-controls" aria-busy={busy}>
      <div className="notification-setting">
        <label className="notification-row notification-toggle"><span><strong>手机通知</strong><span className="notification-description" id="phone-notification-status">{busy ? '正在设置…' : description}</span></span>
          <input type="checkbox" role="switch" aria-label="手机通知" aria-describedby="phone-notification-status" checked={enabled}
            disabled={busy || !status || !browserStatus || !canEnable} onChange={event => void (event.target.checked ? enable() : disable())} />
        </label>
        {error || (status?.subscribed && canEnable) ? <div className="notification-assistance">
          {error ? <p className="field-error" role="alert">{error}</p> : null}
          {canEnable && status?.subscribed ? <button className="notification-repair" disabled={busy} onClick={() => void enable()}>重新开启通知</button> : null}
        </div> : null}
      </div>
      <label className="notification-row notification-toggle"><span><strong>接收任务提醒</strong><span className="notification-description">{status?.tasks_enabled && !enabled ? '提醒偏好已保留，开启手机通知后生效' : '任务完成时通知我'}</span></span><input type="checkbox" role="switch" aria-label="接收任务提醒" checked={!!status?.tasks_enabled} disabled={busy || !enabled}
        onChange={event => void setTasks(event.target.checked)} /></label>
    </div>
  </section>;
}
