import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import QRCode from "qrcode";
import { Smartphone, Monitor, Copy, Loader2, Check, ExternalLink, QrCode, Plus, X, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

interface Status {
  enabled: boolean; requested?: boolean; provider?: string; pwa_url?: string | null;
  connection?: { phase: string; message: string; action_url: string | null;
    public_check?: { state: string; message: string; checked: number; reachable: number; checked_at: number | null } | null };
}
interface Device { device_id: string; name: string; status: string; verification_code: string; expires_at?: number;
  push?: { subscribed: boolean; tasks_enabled?: boolean; last_test: { status: string; received_at: number | null } | null } }
async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api/mobile/${path}`, { method,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "操作未完成");
  return value;
}

export function MobileConnection() {
  const cache = useQueryClient();
  const status = useQuery({ queryKey: ["mobile-status"], queryFn: () => request<Status>("status"), refetchInterval: 2000 });
  const ready = !!status.data?.enabled;
  const requested = status.data?.requested ?? ready;
  const phase = status.data?.connection?.phase ?? (ready ? "ready" : "stopped");
  const publicCheck = status.data?.connection?.public_check;
  const devices = useQuery({ queryKey: ["mobile-devices"], queryFn: () => request<{ devices: Device[] }>("devices"),
    enabled: ready, refetchInterval: 2000 });
  const pending = devices.data?.devices.filter(d => d.status === "pending") ?? [];
  const paired = devices.data?.devices.filter(d => d.status === "approved") ?? [];
  const [invitation, setInvitation] = useState("");
  const [expires, setExpires] = useState(0);
  const [qr, setQr] = useState("");
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [remove, setRemove] = useState<string | null>(null);
  const [completed, setCompleted] = useState("");
  const seenPending = useRef(new Set<string>());
  useEffect(() => {
    for (const device of devices.data?.devices ?? []) {
      if (device.status === "pending") { seenPending.current.add(device.device_id); setInvitation(""); setQr(""); }
      if (device.status === "approved" && seenPending.current.delete(device.device_id)) setCompleted(device.name);
    }
  }, [devices.data]);
  useEffect(() => {
    if (!invitation && !pending.length) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [invitation, pending.length]);
  async function act(work: () => Promise<void>) {
    setBusy(true); setError("");
    try {
      await work();
      await Promise.all([cache.invalidateQueries({ queryKey: ["mobile-status"] }),
        cache.invalidateQueries({ queryKey: ["mobile-devices"] })]);
    } catch (err) { setError(err instanceof Error ? err.message : "操作未完成"); }
    finally { setBusy(false); }
  }
  async function enable(value: boolean) {
    await request("access", "POST", { enabled: value });
    setInvitation(""); setQr(""); setCompleted("");
  }
  async function invite() {
    const raw = await request<Record<string, unknown>>("pairing/invitations", "POST");
    const page = new URL(String(raw.pwa_url));
    if (page.protocol !== "https:" || page.username || page.password || page.search || page.hash)
      throw new Error("连接地址尚未就绪，请稍后重试");
    // The QR contains only the one-use invitation, never the verification code.
    const text = JSON.stringify({ endpoint: page.origin, protocol_version: 1,
      certificate_sha256: raw.certificate_sha256, computer_id: raw.computer_id,
      invitation: raw.invitation, expires_at: raw.expires_at });
    const link = `${page.href}#pair=${encodeURIComponent(text)}`;
    const image = await QRCode.toDataURL(link, { width: 260, margin: 2, errorCorrectionLevel: "M" });
    setInvitation(link); setExpires(Number(raw.expires_at) * 1000); setNow(Date.now());
    setCopied(false); setCompleted(""); setQr(image);
  }
  const qrSeconds = Math.max(0, Math.ceil((expires - now) / 1000));
  const valid = !!invitation && expires > now && ready;
  const action = status.data?.connection?.action_url;
  const failed = phase === "error";
  const waiting = requested && !ready && !failed;
  const showPairing = ready && status.data?.pwa_url && !pending.length && (!paired.length || !!invitation);
  const unstable = publicCheck && ["partial", "unreachable"].includes(publicCheck.state);
  return <Card className="overflow-hidden rounded-2xl shadow-none">
    <div className="flex flex-wrap items-center justify-between gap-4 border-b px-6 py-5">
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-muted"><Smartphone className="h-5 w-5" strokeWidth={1.6} /></span>
        <div><h2 className="text-base font-semibold tracking-tight">连接手机</h2><p className="mt-0.5 text-xs text-muted-foreground">同一段对话，随时继续。</p></div>
      </div>
      <span role="status" className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        {waiting || status.isLoading ? <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" /> : <span className={`h-1.5 w-1.5 rounded-full ${ready ? "bg-emerald-600" : "bg-muted-foreground"}`} />}
        {status.isLoading ? "读取状态…" : ready ? "手机连接已开启" : failed ? "连接已中断" : waiting ? "正在建立安全连接" : "未开启"}
      </span>
    </div>
    <div className="space-y-6 p-6">
      {error || status.error ? <p role="alert" className="rounded-lg bg-destructive/5 p-3 text-sm text-destructive">{error || "暂时无法读取连接状态，请稍后重试"}</p> : null}
      {!ready ? <div className="py-3">
        <div className="mb-5 flex items-center gap-3 text-muted-foreground"><Monitor className="h-9 w-9" strokeWidth={1.3} /><span className="h-px w-8 bg-border" /><Smartphone className="h-7 w-7" strokeWidth={1.3} /></div>
        <h3 className="text-xl font-semibold tracking-tight">{failed ? "恢复手机连接" : waiting ? action ? "还差一步，完成账号授权" : "正在准备你的手机入口" : "把电脑里的对话带在身边"}</h3>
        <p className="mb-5 mt-2 max-w-md text-sm leading-6 text-muted-foreground">{failed ? "原来的手机和会话都已保留，重新连接后即可继续。" : waiting ? "首次使用需要完成 Tailscale 授权，完成后会自动继续。" : "在手机上继续会话、查看任务结果，所有内容与这台电脑同步。"}</p>
        {action ? <a href={action} target="_blank" rel="noreferrer" className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          {phase === "needs_funnel_https_permission" ? "继续授权" : "登录 / 注册"}<ExternalLink className="h-3.5 w-3.5" /></a>
          : <Button disabled={busy || status.isLoading || status.isError || waiting} onClick={() => void act(() => enable(true))}>
            {busy || waiting ? <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" /> : null}{failed ? "重新连接" : waiting ? "正在准备…" : "开启手机连接"}</Button>}
        {requested ? <Button variant="ghost" disabled={busy} className="ml-2 text-muted-foreground" onClick={() => void act(() => enable(false))}>关闭连接</Button> : null}
        {failed ? <p className="mt-3 text-xs text-muted-foreground">{status.data?.connection?.message}</p> : null}
      </div> : null}
      {completed ? <p role="status" className="flex items-center gap-2 rounded-xl bg-emerald-50 p-3 text-sm text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"><Check className="h-4 w-4" />{completed}已连接，可以在手机上继续对话了。</p> : null}
      {ready && pending.map(device => <div key={device.device_id} className="rounded-xl border border-emerald-200 bg-emerald-50/40 p-5 dark:border-emerald-900 dark:bg-emerald-950/20">
        <div className="flex items-start justify-between gap-3"><div><p className="text-xs font-medium text-emerald-700 dark:text-emerald-400">等待手机输入</p><h3 className="mt-1 text-base font-semibold">在「{device.name}」上输入配对码</h3></div>
          <Button aria-label={`取消${device.name}的连接申请`} size="icon" variant="ghost" disabled={busy} onClick={() => void act(async () => { await request(`devices/${device.device_id}`, "DELETE"); })}><X className="h-4 w-4" /></Button></div>
        <p aria-label={`${device.name}的配对码`} className="my-5 select-all font-mono text-4xl font-medium tracking-[0.25em] sm:text-5xl">{device.verification_code}</p>
        <p className="text-sm text-muted-foreground">在手机上输入这六位数字，即可完成连接。</p>
        <p className="mt-2 text-xs text-muted-foreground">只在你正在连接的手机上输入。{device.expires_at ? `剩余 ${Math.max(0, Math.ceil((device.expires_at * 1000 - now) / 1000))} 秒` : "配对码五分钟内有效。"}</p>
      </div>)}
      {showPairing ? <div className="grid gap-7 py-2 sm:grid-cols-[196px_1fr] sm:items-center">
        <div className="mx-auto flex h-[196px] w-[196px] items-center justify-center rounded-2xl border bg-white p-2">
          {valid ? <img src={qr} width={180} height={180} alt="用手机相机扫描，打开交易罗盘并申请配对" />
            : <div className="flex flex-col items-center gap-4 text-muted-foreground"><QrCode className="h-12 w-12" strokeWidth={1.2} /><Button size="sm" disabled={busy} onClick={() => void act(invite)}>{invitation ? "重新生成二维码" : "生成配对二维码"}</Button></div>}
        </div>
        <div className="min-w-0"><h3 className="text-lg font-semibold tracking-tight">扫一扫，连接这台电脑</h3>
          <ol aria-label="手机配对步骤" className="mt-4 space-y-3 text-sm text-muted-foreground">{["用手机相机扫描二维码", "在手机上确认名称，开始连接", "输入电脑显示的六位配对码"].map((label, i) => <li className="flex items-center gap-3" key={label}><span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-medium text-foreground">{i + 1}</span>{label}</li>)}</ol>
          {invitation ? <p role="status" className="mt-4 text-xs tabular-nums text-muted-foreground">{valid ? `二维码 ${Math.floor(qrSeconds / 60)}:${String(qrSeconds % 60).padStart(2, "0")} 后过期` : "二维码已过期，请重新生成。"}</p> : null}
        </div>
      </div> : null}
      {ready ? <div className="space-y-3">
        {paired.length ? <div className="flex items-center justify-between gap-3"><h3 className="text-xs font-medium text-muted-foreground">已配对的手机 · {paired.length}</h3>
          {!invitation && !pending.length ? <Button variant="ghost" size="sm" disabled={busy} onClick={() => void act(invite)}><Plus className="mr-1.5 h-3.5 w-3.5" />连接另一台</Button> : null}</div> : null}
        {devices.error ? <p role="alert" className="text-sm text-destructive">暂时无法读取手机列表</p> : null}
        {paired.map(device => <div key={device.device_id} className="rounded-xl border px-4 py-4">
          <div className="flex items-center gap-3"><Smartphone className="h-5 w-5 shrink-0 text-muted-foreground" strokeWidth={1.5} /><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{device.name}</p><p className="mt-1 text-xs text-muted-foreground">已配对 · {device.push?.tasks_enabled ? "任务提醒已开启" : "可在手机上开启任务提醒"}</p></div>
            <Button variant="ghost" size="sm" disabled={busy} className="text-muted-foreground" onClick={() => setRemove(device.device_id)}>移除</Button></div>
          {remove === device.device_id ? <div className="mt-3 border-t pt-3"><p className="mb-3 text-xs text-muted-foreground">移除后，这台手机需要重新扫码连接。电脑里的会话会保留。</p><div className="flex gap-2"><Button variant="destructive" size="sm" disabled={busy} onClick={() => void act(async () => { await request(`devices/${device.device_id}`, "DELETE"); setRemove(null); })}>确认移除</Button><Button variant="ghost" size="sm" onClick={() => setRemove(null)}>取消</Button></div></div> : null}
          {device.push?.subscribed ? <details className="mt-3 text-xs text-muted-foreground"><summary className="cursor-pointer py-1">通知检查</summary><div className="mt-2 flex flex-wrap items-center gap-3"><Button variant="outline" size="sm" disabled={busy} onClick={() => void act(async () => { await request(`devices/${device.device_id}/push/test`, "POST"); })}>发送测试通知</Button>
            {device.push.last_test ? <span role="status">{device.push.last_test.received_at ? "手机已接收" : device.push.last_test.status === "accepted" ? "已发送，等待手机接收" : device.push.last_test.status === "sending" ? "正在发送…" : "尚未确认送达，请检查手机通知设置"}</span> : null}</div></details> : null}
        </div>)}
      </div> : null}
      {ready ? <div className="space-y-3 border-t pt-4">
        {unstable ? <p role="status" className="text-xs text-amber-700 dark:text-amber-400">部分连接暂时不可用。如果手机未能连接，请稍后重试。</p> : null}
        <div className="flex flex-wrap items-center justify-between gap-3"><p className="text-xs text-muted-foreground">使用时，请保持电脑开机并运行交易罗盘。</p><Button size="sm" variant="ghost" className="h-8 text-xs text-muted-foreground" disabled={busy} onClick={() => void act(() => enable(false))}>关闭连接</Button></div>
        <details className="group text-xs text-muted-foreground"><summary className="flex w-fit cursor-pointer list-none items-center gap-1 py-1"><ChevronRight className="h-3 w-3 group-open:rotate-90" />连接遇到问题？</summary>
          <div className="mt-3 space-y-3 border-l pl-4">
            {valid ? <Button variant="outline" size="sm" onClick={() => void act(async () => { await navigator.clipboard.writeText(invitation); setCopied(true); })}><Copy className="mr-2 h-3 w-3" />{copied ? "已复制" : "复制配对链接"}</Button> : null}
            {status.data?.pwa_url ? <a href={status.data.pwa_url} target="_blank" rel="noreferrer" className="flex w-fit items-center gap-1.5 underline underline-offset-4">打开移动端入口<ExternalLink className="h-3 w-3" /></a> : null}
            {publicCheck ? <div aria-label="此电脑的公网检测"><p>{publicCheck.message}{publicCheck.checked > 0 ? `（${publicCheck.reachable} / ${publicCheck.checked}）` : ""}</p><p className="mt-1">{publicCheck.checked_at ? `检测于 ${new Date(publicCheck.checked_at * 1000).toLocaleTimeString()}。` : ""}手机网络可能不同，请以手机实际连接为准。</p></div> : null}
          </div>
        </details>
      </div> : null}
    </div>
  </Card>;
}
