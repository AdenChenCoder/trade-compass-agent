import { useState } from 'react';
import QRCode from 'qrcode';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

async function request(path: string, body: unknown) {
  const response = await fetch(`/api/mobile/peer/${path}`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '直连未完成');
  return value;
}
export function MobilePeerConnection() {
  const [offer, setOffer] = useState('');
  const [answer, setAnswer] = useState('');
  const [site, setSite] = useState('');
  const [qr, setQr] = useState('');
  const [hint, setHint] = useState('');
  const [busy, setBusy] = useState(false);
  async function act(work: () => Promise<void>) {
    setBusy(true); setHint('');
    try { await work(); } catch (error) { setHint(error instanceof Error ? error.message : '操作未完成'); }
    finally { setBusy(false); }
  }
  return <section className="space-y-3 rounded-lg border p-4">
    <h3 className="text-sm font-medium">PWA 直连验证 · 无需电脑域名和证书</h3>
    <p className="text-xs text-muted-foreground">手机从项目移动端页面添加到主屏幕后，先用同一 Wi-Fi 连接。此验证版需要交换两端连接信息，刷新或断开后需重新交换；已有设备批准记录保留。</p>
    <label className="block text-sm" htmlFor="peer-site">项目移动端 HTTPS 地址（可选，用于生成扫码链接）</label>
    <Input id="peer-site" value={site} onChange={e => { setSite(e.target.value); setQr(''); }} placeholder="https://项目的手机访问地址/mobile/" />
    <Button disabled={busy} onClick={() => void act(async () => {
      if (site) { const url = new URL(site); if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) throw new Error('请填写项目移动端的 HTTPS 地址'); }
      const previous = offer;
      setOffer(''); setAnswer(''); setQr('');
      if (previous) await fetch(`/api/mobile/peer/${JSON.parse(previous).peer_id}`, { method: 'DELETE' });
      const value = await request('offer', {});
      const text = JSON.stringify(value); setOffer(text); setAnswer(''); setQr('');
      if (site) {
        try { setQr(await QRCode.toDataURL(`${site}#peer=${encodeURIComponent(text)}`, { width: 360, margin: 2, errorCorrectionLevel: 'L' })); }
        catch { setHint('连接信息较长，请使用下方复制方式。'); }
      }
    })}>{busy ? '正在处理…' : '生成直连信息'}</Button>
    {offer ? <>
      {qr ? <img src={qr} width={280} height={280} alt="手机扫描打开直连配对页面" /> : null}
      <label htmlFor="peer-offer" className="block text-sm">电脑直连信息（5 分钟内使用）</label>
      <textarea id="peer-offer" readOnly value={offer} onFocus={e => e.target.select()} className="min-h-24 w-full rounded-md border bg-background p-2 text-xs" />
      <Button variant="outline" onClick={() => void act(async () => { await navigator.clipboard.writeText(offer); setHint('已复制，请粘贴到手机的“电脑直连信息”'); })}>复制直连信息</Button>
      <label htmlFor="peer-answer" className="block text-sm">手机返回信息</label>
      <textarea id="peer-answer" value={answer} maxLength={65536} onChange={e => setAnswer(e.target.value)} className="min-h-24 w-full rounded-md border bg-background p-2 text-xs" placeholder="粘贴手机生成的返回信息" />
      <Button disabled={busy || !answer} onClick={() => void act(async () => {
        const value = JSON.parse(answer);
        if (value.peer_id !== JSON.parse(offer).peer_id) throw new Error('返回信息与当前连接不匹配，请使用本次手机返回信息');
        await request('answer', value); setHint('已导入，请回手机点击“电脑已导入，继续连接”，再核对下方设备数字并批准。');
      })}>建立直连</Button>
    </> : null}
    {hint ? <p role="status" className="text-sm">{hint}</p> : null}
  </section>;
}
