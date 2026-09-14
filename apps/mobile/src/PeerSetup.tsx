import { useState } from 'react';
import { PeerCompass } from './peer';

export function PeerSetup({ onConnected }: { onConnected: () => void }) {
  const [input, setInput] = useState(() => {
    const value = new URLSearchParams(location.hash.slice(1)).get('peer');
    if (value) { history.replaceState(null, '', location.pathname); sessionStorage.setItem('compass.peer.offer', value); }
    return value || sessionStorage.getItem('compass.peer.offer') || '';
  });
  const [answer, setAnswer] = useState('');
  const [name, setName] = useState('我的手机');
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState('');
  async function act(work: () => Promise<void>) {
    setBusy(true); setHint('');
    try { await work(); } catch (error) { setHint(error instanceof Error ? error.message : '连接未完成'); }
    finally { setBusy(false); }
  }
  return <section className="connection-card">
    <h3>同一 Wi-Fi 直连验证</h3>
    <p>首次连接或刷新后，请交换两端连接信息。电脑批准记录会保留，断开不会停止已经提交的任务。</p>
    <label>手机名称<input value={name} maxLength={80} onChange={e => setName(e.target.value)} /></label>
    <label>电脑直连信息<textarea value={input} maxLength={65536} onChange={e => setInput(e.target.value)} placeholder="在电脑设置中生成并复制直连信息" /></label>
    <button className="secondary" disabled={busy || !input || !name.trim()} onClick={() => void act(async () => {
      setAnswer(''); setAnswer(await PeerCompass.prepare(input));
    })}>生成手机返回信息</button>
    {answer ? <>
      <label htmlFor="phone-answer">手机返回信息</label><textarea id="phone-answer" readOnly value={answer} onFocus={e => e.target.select()} />
      <p>将这段信息复制到电脑的“手机返回信息”，点击“建立直连”，再回到手机继续。</p>
      <button className="secondary" onClick={() => void act(async () => { await navigator.clipboard.writeText(answer); setHint('已复制，请在电脑粘贴'); })}>复制手机返回信息</button>
      <button className="primary" disabled={busy} onClick={() => void act(async () => {
        await PeerCompass.finish(name.trim()); sessionStorage.removeItem('compass.peer.offer'); setInput(''); setAnswer(''); onConnected();
      })}>{busy ? '正在连接…' : '电脑已导入，继续连接'}</button>
    </> : null}
    {hint ? <p role="status">{hint}</p> : null}
    <p className="muted">这是直连验证版，暂不支持跨网络或后台自动重连。移除手机本地连接后，也可在电脑设备列表中撤销其授权。</p>
  </section>;
}
