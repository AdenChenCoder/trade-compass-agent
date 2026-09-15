import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { Markdown } from './Markdown';

export interface Notice { title: string; message: string; severity: string; task_status?: string | null }

function noticeResult(notice: Notice): { tone: 'error' | 'warning' | ''; label: string } {
  if (notice.task_status === 'failed') return { tone: 'error', label: '任务失败' };
  if (notice.task_status === 'timed_out') return { tone: 'error', label: '任务超时' };
  if (notice.task_status === 'degraded') return { tone: 'warning', label: '结果不完整' };
  if (notice.severity === 'error') return { tone: 'error', label: '任务失败' };
  if (notice.severity === 'warning' || notice.severity === 'critical') return { tone: 'warning', label: '提醒' };
  return { tone: '', label: '任务结果' };
}

export function TaskMessages({ notices, notificationClick }: { notices: Notice[]; notificationClick: string | null }) {
  const [active, setActive] = useState<{ notice: Notice; opener: HTMLButtonElement; entry: string } | null>(null);
  const navigation = useRef<{ entry: string; returning: boolean } | null>(null);
  const returnToList = useCallback(() => {
    const current = navigation.current;
    if (current && !current.returning && history.state?.compassNotice === current.entry) {
      current.returning = true; history.back();
    }
  }, []);
  const close = useCallback(() => { returnToList(); setActive(null); }, [returnToList]);
  useEffect(() => returnToList, [returnToList]);
  useEffect(close, [notificationClick, close]);
  // The API has no record IDs. Content + occurrence keeps existing card buttons
  // mounted during polling, including identical notifications. Never use these as task IDs.
  const occurrences = new Map<string, number>();
  const cards = [...notices].reverse().map(notice => {
    const content = JSON.stringify([notice.title, notice.message, notice.severity, notice.task_status]);
    const occurrence = occurrences.get(content) ?? 0;
    occurrences.set(content, occurrence + 1);
    return { notice, key: `${content}:${occurrence}`, result: noticeResult(notice) };
  });
  return <section className="notices-view" aria-label="任务结果">
    <div className="section-heading"><h2>最近的任务</h2><span>{notices.length} 条消息</span></div>
    {notices.length === 0 ? <div className="empty"><Icon name="bell" /><h3>还没有任务消息</h3><p>任务结果和提醒会出现在这里。</p></div> : cards.map(({ notice, key, result }) => <article className={`task-card ${result.tone ? `task-${result.tone}` : ''}`} key={key}>
      <button className="task-card-open" aria-label={`查看任务详情：${notice.title}`} aria-haspopup="dialog"
        onClick={event => {
          const entry = crypto.randomUUID();
          // A user gesture creates one history entry, independent of effect replays.
          history.pushState({ ...history.state, compassNotice: entry }, '');
          navigation.current = { entry, returning: false };
          setActive({ notice, opener: event.currentTarget, entry });
        }} />
      <div className="task-heading" aria-hidden="true"><span className="task-icon"><Icon name={result.tone ? 'alert' : 'bell'} /></span><h3>{notice.title}</h3></div>
      <div className="task-preview" aria-hidden="true"><Markdown text={notice.message} /></div>
      <div className="task-footer" aria-hidden="true"><ResultBadge notice={notice} /><span>查看详情<Icon name="chevron" /></span></div>
    </article>)}
    {active ? <NoticeDetails notice={active.notice} opener={active.opener} entry={active.entry} onReturn={returnToList} onClose={close} /> : null}
  </section>;
}

function ResultBadge({ notice }: { notice: Notice }) {
  const result = noticeResult(notice);
  return <span className={`result-badge ${result.tone ? `result-${result.tone}` : ''}`}><Icon name={result.tone ? 'alert' : 'check'} />{result.label}</span>;
}

function NoticeDetails({ notice, opener, entry, onReturn, onClose }: { notice: Notice; opener: HTMLButtonElement; entry: string; onReturn: () => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const dismissalRequested = useRef(false);
  const backdropPressed = useRef(false);
  const [closing, setClosing] = useState(false);
  const [copyStatus, setCopyStatus] = useState('');

  useEffect(() => {
    const element = dialog.current!;
    const overflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = 'hidden';
    // Back (including Android's back gesture) dismisses this view before leaving the PWA.
    const back = () => { if (history.state?.compassNotice !== entry) setClosing(true); };
    window.addEventListener('popstate', back);
    element.showModal();
    return () => {
      window.removeEventListener('popstate', back);
      element.close();
      document.documentElement.style.overflow = overflow;
      if (opener.isConnected) opener.focus({ preventScroll: true });
    };
  }, [opener, entry]);

  useEffect(() => {
    if (!closing) return;
    const timer = setTimeout(onClose, matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 180);
    return () => clearTimeout(timer);
  }, [closing, onClose]);

  function dismiss() {
    if (closing || dismissalRequested.current) return;
    dismissalRequested.current = true;
    onReturn();
    if (history.state?.compassNotice !== entry) setClosing(true);
  }
  async function copy() {
    try { await navigator.clipboard.writeText(`${notice.title}\n\n${notice.message}`); setCopyStatus('已复制完整内容'); }
    catch { setCopyStatus('复制未完成，请长按正文手动复制。'); }
  }
  return <dialog ref={dialog} className={`notice-sheet ${closing ? 'is-closing' : ''}`} aria-labelledby="notice-detail-title"
    onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const targets = event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex="0"]');
      const first = targets[0], last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }}
    onCancel={event => { event.preventDefault(); dismiss(); }}
    onPointerDown={event => { backdropPressed.current = event.target === event.currentTarget; }}
    onClick={event => { if (backdropPressed.current && event.target === event.currentTarget) dismiss(); }}>
    <div className="notice-sheet-surface">
      <div className="sheet-grip" aria-hidden="true"><span /></div>
      <div className="notice-sheet-heading"><div><ResultBadge notice={notice} /><h2 id="notice-detail-title">{notice.title}</h2></div>
        <button className="icon-button sheet-close" aria-label="关闭详情" autoFocus onClick={dismiss}><Icon name="close" /></button></div>
      <div className="notice-sheet-content" tabIndex={0} aria-label="完整任务消息">
        <h3 className="detail-section-title">完整消息</h3>
        <Markdown text={notice.message} />
      </div>
      <div className="notice-sheet-actions">
        {copyStatus ? <p role="status">{copyStatus}</p> : null}
        <div><button className="primary" onClick={() => void copy()}><Icon name="copy" />复制内容</button><button className="secondary" onClick={dismiss}>返回列表</button></div>
      </div>
    </div>
  </dialog>;
}
