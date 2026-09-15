import { useCallback, useEffect, useRef, useState } from 'react';
import { Capacitor } from '@capacitor/core';
import { Markdown } from './Markdown';
import { TaskMessages, type Notice } from './TaskMessages';
import { api, Compass, ConnectionInfo, parseInvitation, RequestError } from './native';
import { PushSettings } from './PushSettings';
import { Icon } from './Icon';
import { PairingCode } from './PairingCode';
import { AppUpdate, restoreUpdateView } from './AppUpdate';
import { PeerSetup } from './PeerSetup';
import { peerMode } from './peer';
import { acknowledgeNotificationNavigation, listenForNotificationNavigation } from './notification-navigation';
import { fillHistoryGap } from '../../shared/session-history';

interface Session { session_id: string; title?: string; preview?: string; updated_at: string }
interface SessionList { sessions: Session[]; next_cursor?: string | null }
interface Message { role: string; content: string; timestamp?: string; sections?: { title: string; content: string }[]; tool_calls?: { name: string }[] }
interface Page { session_id: string; title?: string; messages: Message[]; has_active_turn: boolean; page: { start_index: number; next_before: number | null }; cache_version?: 1 }
interface Receipt { request_id: string; session_id: string; status: string }
interface Pending { request_id: string; session_id: string; message: string }
const errorText = (error: unknown) => error instanceof Error ? error.message : '暂时无法连接电脑';
function cached<T>(key: string, fallback: T): T { try { return JSON.parse(localStorage.getItem(key) || 'null') ?? fallback; } catch { return fallback; } }
function save(key: string, value: unknown) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Online use remains available if storage is full. */ } }

export function App() {
  const [connection, setConnection] = useState<ConnectionInfo>({ connected: false });
  const [ready, setReady] = useState(false);
  // null means authorization has not been checked, not that pairing is required.
  const [approved, setApproved] = useState<boolean | null>(null);
  const [tab, setTab] = useState<'sessions' | 'notices' | 'connection'>(() =>
    new URLSearchParams(location.search).get('view') === 'notices' ? 'notices' : 'sessions');
  const [notificationClick, setNotificationClick] = useState<string | null>(null);
  const lastNotificationClick = useRef<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionPages, setSessionPages] = useState(1);
  const loadedSessionPages = useRef(1);
  const [sessionCursor, setSessionCursor] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [missingSession, setMissingSession] = useState<string | null>(null);
  const [page, setPage] = useState<Page | null>(null);
  const pageRef = useRef(page);
  pageRef.current = page;
  const [notices, setNotices] = useState<Notice[]>([]);
  const native = Capacitor.isNativePlatform();
  const [name, setName] = useState('我的手机');
  const [invitation, setInvitation] = useState(() => {
    if (Capacitor.isNativePlatform()) return '';
    const invite = new URLSearchParams(location.hash.slice(1)).get('pair');
    if (invite) { history.replaceState(null, '', location.pathname); sessionStorage.setItem('compass.invitation', invite); }
    return invite || sessionStorage.getItem('compass.invitation') || '';
  });
  const [deviceName, setDeviceName] = useState('我的手机');
  const [authorizationLost, setAuthorizationLost] = useState(false);
  const [error, setError] = useState('');
  const [copyHint, setCopyHint] = useState('');
  const [online, setOnline] = useState(false);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  const [draft, setDraft] = useState('');
  const [draftError, setDraftError] = useState('');
  const [pending, setPending] = useState<Pending | null>(null);
  const [receipt, setReceipt] = useState('');
  const [older, setOlder] = useState(false);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const refreshRequest = useRef({ id: 0, pending: false });
  const [refreshing, setRefreshing] = useState(false);
  const [refreshResult, setRefreshResult] = useState('');
  const nearBottom = useRef(true);
  const root = `compass.v1.${connection.computer_id ?? 'unpaired'}.`;
  const requestKey = `${root}request.${selected}`;
  const draftKey = `${root}draft.${selected}`;
  const clearSentDraft = useCallback((message: string) => {
    if (cached<string>(draftKey, '').trim() === message) save(draftKey, '');
    setDraft(current => current.trim() === message ? '' : current);
  }, [draftKey]);
  const applyReceipt = useCallback((record: Receipt, outstanding: Pending) => {
    // Polling can observe a terminal result before the POST response arrives.
    if (cached<Pending | null>(requestKey, null)?.request_id !== outstanding.request_id) return;
    if (record.status === 'unknown' || record.status === 'failed') {
      setDraft(current => current.trim() ? current : outstanding.message);
      if (!cached<string>(draftKey, '').trim()) save(draftKey, outstanding.message);
    } else {
      clearSentDraft(outstanding.message);
    }
    setReceipt(record.status);
    if (record.status !== 'running') {
      setPending(null); localStorage.removeItem(requestKey);
    }
  }, [requestKey, draftKey, clearSentDraft]);

  useEffect(() => listenForNotificationNavigation(id => {
    if (lastNotificationClick.current === id) return;
    lastNotificationClick.current = id;
    setTab('notices'); setNotificationClick(id);
  }), []);
  useEffect(() => {
    if (notificationClick && tab === 'notices') {
      window.scrollTo(0, 0);
      void acknowledgeNotificationNavigation(notificationClick);
    }
  }, [notificationClick, tab]);
  useEffect(() => {
    const url = new URL(location.href);
    if (url.searchParams.get('view') === 'notices') {
      url.searchParams.delete('view'); history.replaceState(null, '', url);
    }
  }, []);

  useEffect(() => {
    const track = () => { nearBottom.current = document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 180; };
    window.addEventListener('scroll', track, { passive: true });
    return () => window.removeEventListener('scroll', track);
  }, []);
  useEffect(() => {
    if (page && nearBottom.current) requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight));
  }, [page?.messages.length, selected]);

  useEffect(() => {
    if (connection.connected) return;
    let active = true; let loading = false; let timer: ReturnType<typeof setTimeout>;
    async function load() {
      if (!active || loading) return;
      loading = true;
      try {
        const value = await Compass.connection();
        if (active) { setConnection(value); setReady(true); setError(''); }
      } catch (err) {
        if (active) { setError(errorText(err)); timer = setTimeout(() => void load(), 2000); }
      } finally { loading = false; }
    }
    const wake = () => { clearTimeout(timer); void load(); };
    window.addEventListener('online', wake);
    window.addEventListener('pageshow', wake);
    void load();
    return () => { active = false; clearTimeout(timer);
      window.removeEventListener('online', wake); window.removeEventListener('pageshow', wake); };
  }, [connection.connected, refreshRevision]);
  useEffect(() => {
    setSessions(cached(`${root}sessions`, [])); setNotices(cached(`${root}notices`, []));
    setSessionPages(1); loadedSessionPages.current = 1; setSessionCursor(null); setLoadingSessions(false);
    const resume = restoreUpdateView(connection.computer_id);
    setSelected(resume?.selected ?? null); setPage(null);
    if (resume && !lastNotificationClick.current && tab !== 'notices') setTab(resume.tab);
  }, [root]);
  useEffect(() => {
    const prepare = (event: Event) => {
      try {
        if (selected) {
          localStorage.setItem(draftKey, JSON.stringify(draft));
          if (pending) localStorage.setItem(requestKey, JSON.stringify(pending));
        }
        sessionStorage.setItem('compass.resume-after-update', JSON.stringify({
          computerId: connection.computer_id, tab, selected, createdAt: Date.now(),
        }));
      } catch { event.preventDefault(); }
    };
    window.addEventListener('compass-before-update', prepare);
    return () => window.removeEventListener('compass-before-update', prepare);
  }, [connection.computer_id, tab, selected, draft, draftKey, pending, requestKey]);
  useEffect(() => {
    nearBottom.current = true;
    setMissingSession(null); setDraftError('');
    setDraft(cached(draftKey, '')); setPending(cached(requestKey, null)); setReceipt('');
    setPage(selected ? cached(`${root}page.${selected}`, null) : null);
  }, [selected, root, draftKey, requestKey]);

  useEffect(() => {
    if (!connection.connected) return;
    let active = true; let syncing = false; let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function sync() {
      if (!active || syncing) return;
      syncing = true;
      try {
        if (document.visibilityState !== 'hidden') {
          const status = await api<{ status: string; name?: string }>('pairing/status');
          if (!active) return;
          setOnline(true); setAuthorizationLost(false); setDeviceName(status.name || '我的手机'); setApproved(status.status === 'approved');
          if (refreshRequest.current.pending && refreshRequest.current.id === refreshRevision) {
            refreshRequest.current.pending = false; setRefreshing(false);
            setRefreshResult(`已刷新 · ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`);
          } else setRefreshResult(previous => previous === '刷新失败，请重试' ? '' : previous);
          if (status.status === 'approved') {
            if (tab === 'sessions') {
              const collected = new Map<string, Session>();
              let cursor: string | null = null;
              let pages = 0;
              do {
                const list: SessionList = await api<SessionList>(`sessions?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
                if (!active) return;
                for (const item of list.sessions) collected.set(item.session_id, item);
                cursor = list.next_cursor ?? null;
                pages += 1;
              } while (cursor && pages < sessionPages);
              const listed = [...collected.values()];
              loadedSessionPages.current = pages;
              setSessionCursor(cursor); setLoadingSessions(false);
              setSessions(listed); save(`${root}sessions`, listed);
              if (selected && selected !== missingSession && !older) {
                try {
                  const latest = await api<Page>(`sessions/${encodeURIComponent(selected)}/messages?limit=50`);
                  if (!active) return;
                  const previous = pageRef.current;
                  // Rebuild the previously viewed range once for legacy caches, which
                  // may already contain an undetectable gap from the old merge logic.
                  const previousEnd = previous?.session_id === selected ? previous.page.start_index
                    + (previous.cache_version === 1 ? previous.messages.length : 0) : undefined;
                  const value = await fillHistoryGap(latest, previousEnd,
                    before => api<Page>(`sessions/${encodeURIComponent(selected)}/messages?limit=50&before=${before}`), controller.signal);
                  if (!active) return;
                  setPage(previous => {
                    const prefix = previous?.session_id === value.session_id && previous.page.start_index < value.page.start_index
                      ? previous.messages.slice(0, value.page.start_index - previous.page.start_index) : [];
                    const merged = prefix.length ? { ...value, messages: [...prefix, ...value.messages], page: { ...value.page,
                      start_index: previous!.page.start_index, next_before: previous!.page.next_before } } : value;
                    const continuous: Page = { ...merged, cache_version: 1 };
                    save(`${root}page.${selected}`, continuous); return continuous;
                  });
                  const outstanding = cached<Pending | null>(requestKey, null);
                  if (outstanding) {
                    const record = await api<Receipt>(`turns/${outstanding.request_id}`).catch(err => {
                      if (err instanceof RequestError && err.status === 404) return null;
                      throw err;
                    });
                    if (!active) return;
                    if (cached<Pending | null>(requestKey, null)?.request_id !== outstanding.request_id) return;
                    if (record) applyReceipt(record, outstanding);
                    else setReceipt('unconfirmed');
                  }
                } catch (err) {
                  if (!(err instanceof RequestError && err.status === 404)) throw err;
                  if (!active) return;
                  setMissingSession(selected); setPage(null);
                }
              }
            } else if (tab === 'notices') {
              const value = await api<Notice[]>('notifications?limit=100');
              if (!active) return;
              setNotices(value); save(`${root}notices`, value);
            }
          }
          if (active) setError('');
        }
      } catch (err) {
        if (active && refreshRequest.current.pending && refreshRequest.current.id === refreshRevision) {
          refreshRequest.current.pending = false; setRefreshing(false); setRefreshResult('刷新失败，请重试');
        }
        if (active) { setOnline(false); if (err instanceof RequestError && err.status === 401) setAuthorizationLost(true); setError(err instanceof RequestError && err.status === 401
          ? '连接已失效，请重新扫码。电脑里的会话会保留。' : errorText(err)); }
      } finally {
        syncing = false;
        if (active) {
          setLoadingSessions(false);
          if (sessionPages > loadedSessionPages.current) setSessionPages(loadedSessionPages.current);
          timer = setTimeout(() => void sync(), 2000);
        }
      }
    }
    const wake = () => { clearTimeout(timer); void sync(); };
    const visible = () => { if (document.visibilityState !== 'hidden') wake(); };
    const offline = () => { setOnline(false); setError('手机网络已断开，联网后会自动恢复连接。'); };
    window.addEventListener('online', wake);
    window.addEventListener('pageshow', wake);
    window.addEventListener('offline', offline);
    document.addEventListener('visibilitychange', visible);
    void sync();
    return () => { active = false; controller.abort(); clearTimeout(timer);
      window.removeEventListener('online', wake); window.removeEventListener('pageshow', wake);
      window.removeEventListener('offline', offline); document.removeEventListener('visibilitychange', visible); };
  }, [connection, tab, selected, root, requestKey, older, applyReceipt, refreshRevision, sessionPages, missingSession]);

  function persistDraft() {
    if (!selected || (!draft && !cached<string>(draftKey, ''))) return true;
    try { localStorage.setItem(draftKey, JSON.stringify(draft)); setDraftError(''); return true; }
    catch { setDraftError('草稿暂时无法保存，请先复制内容，或释放存储空间后重试。'); return false; }
  }
  function selectSession(id: string | null) {
    if (persistDraft()) setSelected(id);
  }

  async function pair(text: string) {
    setBusy(true); setError('');
    try {
      const parsed = parseInvitation(text);
      const result = await Compass.pair({ invitation: JSON.stringify(parsed), name });
      if (result.status !== 202) throw new Error('配对申请未被接受，请重新生成二维码');
      setDeviceName(name.trim()); setAuthorizationLost(false); setInvitation('');
      sessionStorage.removeItem('compass.invitation');
    } catch (err) { setError(errorText(err)); }
    finally { setConnection(await Compass.connection().catch(() => ({ connected: false }))); setBusy(false); }
  }
  async function scan() {
    try {
      const { CapacitorBarcodeScanner, CapacitorBarcodeScannerAndroidScanningLibrary } = await import('@capacitor/barcode-scanner');
      const result = await CapacitorBarcodeScanner.scanBarcode({ hint: 0, scanInstructions: '扫描电脑“设置 → 连接手机”的二维码',
        android: { scanningLibrary: CapacitorBarcodeScannerAndroidScanningLibrary.ZXING } });
      await pair(result.ScanResult);
    } catch (err) { setError(errorText(err)); }
  }
  async function send() {
    if (!selected || selected === missingSession || busy || page?.has_active_turn) return;
    const message = pending ?? { request_id: crypto.randomUUID(), session_id: selected, message: draft.trim() };
    if (!message.message) return;
    const sid = selected;
    let persisted = false;
    nearBottom.current = true;
    setBusy(true); setError('');
    try {
      // Persist before sending. A timeout leaves the same ID available for status/retry.
      localStorage.setItem(requestKey, JSON.stringify(message)); persisted = true; setPending(message);
      const result = await api<Receipt>('turns', 'POST', message);
      applyReceipt(result, message);
    } catch (err) {
      if (persisted && cached<Pending | null>(requestKey, null)?.request_id !== message.request_id) return;
      if (err instanceof RequestError && [404, 409, 422, 429].includes(err.status)) {
        setPending(null); localStorage.removeItem(requestKey);
      }
      setError(errorText(err));
    } finally { if (selected === sid) setBusy(false); }
  }
  async function createSession() {
    if (!online || !approved || authorizationLost || busy || older) return;
    if (!persistDraft()) return;
    const recoveredDraft = selected === missingSession ? draft : '';
    setBusy(true); setCreating(true); setCreateError('');
    try {
      const created = await api<Session>('sessions', 'POST');
      if (recoveredDraft) {
        try { localStorage.setItem(`${root}draft.${created.session_id}`, JSON.stringify(recoveredDraft)); }
        catch { throw new Error('新会话已创建，但草稿未能转移。请先复制当前内容。'); }
      }
      const empty: Page = { session_id: created.session_id, messages: [], has_active_turn: false,
        page: { start_index: 0, next_before: null }, cache_version: 1 };
      save(`${root}page.${created.session_id}`, empty);
      setSessions(previous => {
        const next = [created, ...previous.filter(item => item.session_id !== created.session_id)];
        save(`${root}sessions`, next); return next;
      });
      setSelected(created.session_id); setTab('sessions'); setError('');
    } catch (err) { setCreateError(errorText(err)); }
    finally { setBusy(false); setCreating(false); }
  }
  async function loadOlder() {
    if (!selected || page?.page.next_before == null || older) return;
    setOlder(true);
    const previousHeight = document.documentElement.scrollHeight;
    const previousTop = window.scrollY;
    try {
      const value = await api<Page>(`sessions/${encodeURIComponent(selected)}/messages?limit=50&before=${page.page.next_before}`);
      setPage(previous => {
        if (previous?.session_id !== value.session_id) return previous;
        const merged = { ...previous, messages: [...value.messages, ...previous.messages], page: { ...previous.page,
          start_index: value.page.start_index, next_before: value.page.next_before } };
        save(`${root}page.${selected}`, merged);
        return merged;
      });
      requestAnimationFrame(() => window.scrollTo(0, previousTop + document.documentElement.scrollHeight - previousHeight));
    } catch (err) { setError(errorText(err)); } finally { setOlder(false); }
  }
  async function forget() {
    if (!window.confirm('移除手机上的连接和已缓存历史？电脑里的原始会话不会删除。')) return;
    try { await Compass.forget(); } catch (err) { setError(errorText(err)); return; }
    for (const key of Object.keys(localStorage)) if (key.startsWith(root)) localStorage.removeItem(key);
    refreshRequest.current.pending = false; setRefreshing(false); setRefreshResult(''); setCreateError('');
    setConnection({ connected: false }); setApproved(null); setSelected(null); setPage(null); setAuthorizationLost(false); setError(''); setTab('sessions');
  }

  const connectionView = !connection.connected || tab === 'connection' || (!approved && sessions.length === 0);
  useEffect(() => { window.scrollTo(0, 0); }, [tab, connectionView]);
  const showNavigation = connection.connected && (approved || sessions.length > 0);
  const inConversation = !connectionView && tab === 'sessions' && !!selected;
  const title = connectionView ? '我的连接' : tab === 'notices' ? '任务消息' : inConversation ? page?.title || '新对话' : '会话';
  const connectionLabel = authorizationLost ? '连接已失效' : online ? approved ? '已连接' : '等待验证' : '等待连接';
  const restoringConnection = !peerMode && connection.connected && approved === null && !authorizationLost;
  // Keep the neutral launch screen until identity and authorization are known.
  // Previously cached history may still be read while authorization is unavailable.
  // The optional peer transport needs its setup UI to establish a channel first.
  if (!ready || (restoringConnection && sessions.length === 0)) {
    return <div className="app-startup">{error ? <div className="startup-recovery"><p role="alert">{error}</p>
      <button className="secondary" onClick={() => {
        const id = refreshRequest.current.id + 1;
        refreshRequest.current = { id, pending: false }; setError(''); setRefreshRevision(id);
      }}>重试</button></div>
      : <p role="status">正在打开交易罗盘…</p>}</div>;
  }
  return <div className={`app-shell ${showNavigation ? 'has-rail' : 'onboarding'}`}>
    {showNavigation ? <aside className="side-rail">
      <div className="brand-mark" aria-label="交易罗盘"><Icon name="robot" /></div>
      <nav aria-label="主导航">{([['sessions', '会话'], ['notices', '任务消息'], ['connection', '连接']] as const).map(([value, label]) => <button key={value}
        className={`rail-item ${value === 'connection' ? 'rail-connection' : ''} ${tab === value ? 'active' : ''}`}
        aria-current={tab === value ? 'page' : undefined} disabled={busy || older} onClick={() => setTab(value)}>
        <Icon name={value === 'sessions' ? 'chat' : value === 'notices' ? 'bell' : 'link'} /><span>{label}</span>
      </button>)}</nav>
      <div className="rail-device" title={deviceName} aria-label={deviceName}><Icon name="phone" /></div>
    </aside> : null}
    <header className="app-header">
      {inConversation ? <button className="icon-button header-back" aria-label="所有会话" disabled={busy || older} onClick={() => selectSession(null)}><Icon name="back" /></button>
        : !showNavigation ? <div className="brand-mark"><Icon name="robot" /></div> : null}
      <div className="header-title"><h1>{title}</h1><p>{connectionView ? '管理设备连接与通知' : tab === 'notices' ? '查看任务结果与提醒' : inConversation ? '交易罗盘' : '个股研究与市场分析'}</p></div>
      {showNavigation && !connectionView && tab === 'sessions' ? <button className="icon-button new-session-button" aria-label="新建会话" aria-busy={creating}
        title={online ? '新建会话' : '恢复连接后可新建会话'} disabled={!online || !approved || authorizationLost || busy || older}
        onClick={() => void createSession()}><Icon name={creating ? 'more' : 'plus'} /></button> : null}
      {connection.connected ? <span className={`connection-indicator ${online && approved && !authorizationLost ? 'online' : ''}`} role="status" aria-label={connectionLabel} title={connectionLabel} /> : null}
      <AppUpdate />
    </header>
    <main className={inConversation ? 'chat-main' : ''}>
      {error || createError || draftError ? <div className="notice error" role="alert">{draftError || error || createError}</div> : null}
      {connectionView ? <section className="connect-view">
        {restoringConnection ? <p className="muted" role="status">正在恢复连接…</p> : connection.connected && approved && !authorizationLost ? <>
          <section className="service-card" aria-label="电脑连接状态">
            <div className="service-heading"><div><span className="eyebrow">连接状态</span><strong className={`service-status ${online ? 'online' : ''}`}><i />{online ? '已连接' : '正在重连'}</strong></div>
              <div className="connection-refresh"><button className="primary refresh-button" aria-label="刷新连接状态" disabled={refreshing} aria-busy={refreshing} onClick={() => {
                const id = refreshRequest.current.id + 1;
                refreshRequest.current = { id, pending: true }; setRefreshing(true); setRefreshResult(''); setRefreshRevision(id);
              }}><Icon name="refresh" /><span>{refreshing ? '刷新中…' : '刷新'}</span></button>
                <span className="refresh-result" role="status">{refreshResult}</span></div></div>
            <dl className="device-facts"><div><dt><Icon name="computer" />电脑</dt><dd>我的电脑</dd></div>
              <div><dt><Icon name="phone" />当前手机</dt><dd>{deviceName}</dd></div>
              <div><dt><Icon name="lock" />配对状态</dt><dd>已配对</dd></div>
            </dl>
            <details className="connection-details"><summary><Icon name="link" /><span>连接管理</span><Icon name="chevron" /></summary><p className="endpoint">{connection.endpoint}</p><p className="muted">移除连接后，需要重新扫码。电脑中的会话会保留。</p><button className="danger-link" onClick={() => void forget()}>移除连接</button></details>
          </section>
          {!native ? <PushSettings /> : null}
          {!native ? <InstallGuide /> : null}
          <p className="connection-footnote"><Icon name="lock" />仅你授权的手机可以访问</p>
        </> : <>
          <div className="connection-illustration" aria-hidden="true"><span><Icon name="computer" /></span><i /><span><Icon name="phone" /></span></div>
          <div className="setup-progress" aria-label="连接进度"><span className={!connection.connected ? 'current' : ''}>01 连接电脑</span><i /><span className={connection.connected ? 'current' : ''}>02 输入配对码</span></div>
          <h2>{authorizationLost ? '重新连接你的电脑' : connection.connected ? '最后一步，输入配对码' : '让对话，随你而行。'}</h2>
          <p className="connect-description">{authorizationLost ? '在电脑上生成新的二维码，然后重新扫码。' : connection.connected ? '在电脑「设置 → 连接手机」中，找到为这台手机显示的六位数字。' : '连接这台电脑，在手机上继续会话、接收任务消息。'}</p>
          {peerMode && !native ? <PeerSetup onConnected={() => { void Compass.connection().then(setConnection); }} /> : null}
          {connection.connected ? <>
            {authorizationLost ? <button className="primary" onClick={() => void forget()}>清除失效连接</button> : <PairingCode onVerified={() => {
              setApproved(true); setOnline(true); setError('');
              setTab(value => value === 'connection' ? 'sessions' : value);
              setConnection(value => ({ ...value }));
            }} />}
            <p className="pairing-device">正在连接 · {deviceName}</p>
            {!authorizationLost ? <button className="text-button" onClick={() => void forget()}>取消连接</button> : null}
          </> : peerMode ? null : <>
            {!native ? <InstallGuide /> : null}
            <label className="name-field">手机名称<input value={name} maxLength={80} placeholder="我的手机" autoComplete="off" onChange={e => setName(e.target.value)} /><span>方便在电脑上识别这台手机</span></label>
            {native ? <button className="primary connect-primary" disabled={busy || !name.trim()} onClick={() => void scan()}>扫描电脑二维码<Icon name="arrow" /></button>
              : <button className="primary connect-primary" disabled={busy || !invitation || !name.trim()} onClick={() => void pair(invitation)}>{busy ? '正在连接…' : '连接这台电脑'}<Icon name="arrow" /></button>}
            {!native && !invitation ? <p className="muted">请先用手机相机扫描电脑上的连接二维码。</p> : null}
            <details className="pairing-help"><summary>扫码或安装遇到问题？</summary>
              <p>主屏幕应用没有带入连接信息时，可粘贴配对链接继续。</p>
              {!native && invitation ? <button className="secondary" onClick={() => void navigator.clipboard.writeText(invitation)
                .then(() => setCopyHint('已复制。在主屏幕应用中粘贴后，即可继续连接。'))
                .catch(() => setError('复制未完成，请在下方手动复制连接信息。'))}>复制配对链接</button> : null}
              {copyHint ? <p role="status">{copyHint}</p> : null}
              <textarea aria-label="电脑连接信息" placeholder="粘贴配对链接" value={invitation} onChange={e => setInvitation(e.target.value)} />
              <button className="secondary" disabled={busy || !invitation || !name.trim()} onClick={() => void pair(invitation)}>继续连接</button>
            </details>
          </>}
          <p className="connection-footnote"><Icon name="lock" />仅你授权的手机可以访问电脑里的内容</p>
        </>}
      </section> : tab === 'notices' ? <TaskMessages key={connection.computer_id} notices={notices} notificationClick={notificationClick} /> : selected ? <section className="conversation" aria-label="会话内容">
        <div className="transcript">
          {page?.page.next_before != null ? <button className="secondary older" disabled={older} onClick={() => void loadOlder()}>{older ? '正在加载…' : '查看更早消息'}</button> : null}
          {page && page.messages.length === 0 ? <div className="empty"><Icon name="chat" /><h3>今天想研究什么？</h3><p>分析个股、了解市场，或梳理投资思路。<br />例如：今天大盘资金情况如何？</p></div> : null}
          {selected === missingSession ? <div className="empty" role="status"><Icon name="chat" /><h3>这段会话已删除</h3><p>可以返回会话列表，或开始新的对话。</p><button className="secondary" disabled={!online || busy} onClick={() => void createSession()}>{draft ? '用草稿新建会话' : '新建会话'}</button></div> : !page ? <div className="empty">正在读取会话…</div> : page.messages.map((message, index) => <article className={`message ${message.role === 'user' ? 'user' : 'assistant'}`} key={page.page.start_index + index}>
            <span className="message-avatar" aria-hidden="true"><Icon name={message.role === 'user' ? 'person' : 'robot'} /></span>
            <div className="message-body"><span className="sr-only">{message.role === 'user' ? '你' : '交易罗盘'}</span><div className="message-bubble"><Markdown text={message.content} />
              {message.sections?.map((section, i) => <div key={i}><h3>{section.title}</h3><Markdown text={section.content} /></div>)}
              {message.tool_calls?.length ? <details className="message-tools"><summary>使用了 {message.tool_calls.length} 个工具</summary><p>{message.tool_calls.map(tool => tool.name).join('、')}</p></details> : null}
            </div>{message.timestamp && Number.isFinite(Date.parse(message.timestamp)) ? <time className="message-time" dateTime={message.timestamp}>{new Date(message.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time> : null}</div>
          </article>)}
          {page?.has_active_turn ? <div className="working" role="status"><span className="typing-dots" aria-hidden="true"><i /><i /><i /></span>正在处理…</div> : null}
          {['failed', 'unknown', 'interrupted'].includes(receipt) ? <div className="notice">{receipt === 'unknown' ? '处理曾中断，无法确定这条请求是否完成。请先检查历史和执行结果，再决定是否发起新请求。' : receipt === 'failed' ? '这次执行未成功，请在电脑查看详情。' : '这次执行已停止。'}</div> : null}
        </div>
        <form className={`composer ${pending ? 'has-pending' : ''}`} onSubmit={e => { e.preventDefault(); void send(); }}>
          {pending ? <p className="pending-status" role="status">这条消息{receipt === 'running' ? '已接收，正在处理' : '的发送结果待确认'}。{receipt === 'unconfirmed' ? '尚无接收记录，可用原请求重试。' : ''}</p> : null}
          <div className="composer-input"><textarea aria-label="消息" rows={1} placeholder="输入你的问题…" value={draft} maxLength={8000}
            disabled={busy || !!pending} onChange={e => {
              setDraft(e.target.value);
              try { localStorage.setItem(draftKey, JSON.stringify(e.target.value)); setDraftError(''); }
              catch { setDraftError('草稿暂时无法保存，请先复制内容，或释放存储空间后重试。'); }
            }} />
            <button className="primary send-button" aria-label={busy ? '正在发送…' : pending ? '确认并重试原消息' : '发送'} disabled={selected === missingSession || !online || busy || page?.has_active_turn || (!pending && !draft.trim()) || (!!pending && receipt === 'running')}>
              <Icon name={busy ? 'more' : pending ? 'refresh' : 'send'} />{pending && !busy ? <span>确认并重试原消息</span> : null}</button></div>
        </form>
      </section> : <section className="sessions-view" aria-label="会话列表"><div className="section-heading"><h2>最近的会话</h2><span>{sessions.length} 段对话</span></div>
        {sessions.length === 0 ? <div className="empty"><Icon name="chat" /><h3>开始你的研究</h3><p>点击右上角 ＋，开始研究个股或市场。</p></div> : <div className="session-list">{sessions.map(session => <button key={session.session_id} disabled={busy} onClick={() => selectSession(session.session_id)}><span className="session-icon"><Icon name="chat" /></span><div><h3>{session.title || session.preview || '新对话'}</h3><p>{session.preview || '打开会话'}</p><time dateTime={session.updated_at}>{new Date(session.updated_at).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time></div><Icon name="chevron" /></button>)}</div>}
        {sessionCursor ? <button className="secondary older" disabled={!online || busy || loadingSessions} onClick={() => {
          setLoadingSessions(true); setSessionPages(count => count + 1);
        }}>{loadingSessions ? '正在加载…' : '查看更早会话'}</button> : null}
      </section>}
    </main>
    {!showNavigation ? <footer>TRADE COMPASS · 交易罗盘</footer> : null}
  </div>;
}

function InstallGuide() {
  const standalone = matchMedia('(display-mode: standalone)').matches || (navigator as Navigator & { standalone?: boolean }).standalone;
  if (standalone) return null;
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  return <details className="install-guide"><summary><Icon name="plus" /><span>添加到主屏幕<span>像应用一样打开，还能接收通知</span></span><Icon name="chevron" /></summary>
    <p>{ios ? '在 Safari 中轻点分享，选择「添加到主屏幕」。请从新图标打开后连接电脑。' : '打开浏览器菜单，选择「安装应用」或「添加到主屏幕」，再从新图标打开。'}</p>
    <p>若打开后没有连接信息，可再次扫码，或在下方「扫码或安装遇到问题？」中复制配对链接。</p>
  </details>;
}
