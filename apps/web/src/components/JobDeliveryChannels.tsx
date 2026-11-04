import { useId, useRef, useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { Bell, Building2, Check, ChevronDown, Loader2, MessageCircle, Send, X } from "lucide-react";
import { DELIVERY_CHANNEL_OPTIONS } from "@/lib/types";

const channelIcons = { web_log: Bell, feishu: Send, wecom: Building2, weixin: MessageCircle };
const channelLabel = (id: string) => DELIVERY_CHANNEL_OPTIONS.find(option => option.id === id)?.label ?? id;

export function JobDeliveryChannels({ name, channels, disabled, onChange }: {
  name: string;
  channels: string[];
  disabled: boolean;
  onChange: (channels: string[]) => Promise<unknown>;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const savingRef = useRef(false);

  async function toggle(channel: string) {
    if (disabled || savingRef.current) return;
    const next = channels.includes(channel) ? channels.filter(item => item !== channel) : [...channels, channel];
    if (!next.length) { setError("至少保留一个推送渠道"); return; }
    savingRef.current = true;
    setSaving(true); setSaved(false); setError("");
    try { await onChange(next); setSaved(true); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "保存失败，请重试"); }
    finally { savingRef.current = false; setSaving(false); }
  }

  return <Popover.Root open={open} onOpenChange={value => { setOpen(value); if (value && !saving) { setSaved(false); setError(""); } }}>
    <Popover.Trigger className="delivery-trigger" aria-label={`调整 ${name} 的推送渠道`} title={channels.map(channelLabel).join("、")}>
      <span className="delivery-marks" aria-hidden="true">{channels.map(channel => {
        const Icon = channelIcons[channel as keyof typeof channelIcons] ?? Bell;
        return <span key={channel}><Icon size={13} strokeWidth={1.7} /></span>;
      })}</span>
      <span className="delivery-summary">{channels.length > 1 ? `${channels.length} 个渠道` : channels.length ? channelLabel(channels[0]) : "选择渠道"}</span>
      {saving ? <Loader2 size={13} className="animate-spin" aria-label="正在保存" /> : <ChevronDown size={13} className="delivery-chevron" aria-hidden="true" />}
    </Popover.Trigger>
    <Popover.Portal>
      <Popover.Content className="delivery-popover" align="start" sideOffset={8} collisionPadding={16} hideWhenDetached aria-labelledby={`${id}-title`} aria-describedby={`${id}-hint`}>
        <div className="delivery-popover-heading">
          <div><h3 id={`${id}-title`}>推送渠道</h3><p>{name}</p></div>
          <Popover.Close className="delivery-close" aria-label="关闭渠道设置"><X size={16} /></Popover.Close>
        </div>
        <fieldset className="delivery-options" disabled={disabled || saving} aria-busy={saving}>
          <legend className="sr-only">选择推送渠道</legend>
          {DELIVERY_CHANNEL_OPTIONS.map(({ id: channel, label }) => {
            const Icon = channelIcons[channel];
            return <label key={channel} className="delivery-option">
              <input type="checkbox" checked={channels.includes(channel)} onChange={() => void toggle(channel)} />
              <span className="delivery-option-icon" aria-hidden="true"><Icon size={18} strokeWidth={1.6} /></span>
              <span>{label}</span><span className="delivery-option-check" aria-hidden="true"><Check size={13} strokeWidth={2.5} /></span>
            </label>;
          })}
        </fieldset>
        <div className="delivery-popover-footer">
          <p id={`${id}-hint`} role="status">{saving ? <><Loader2 size={12} className="animate-spin" />正在保存…</> : saved ? <><Check size={12} />已保存</> : "勾选后立即保存"}</p>
          {error ? <p className="delivery-error" role="alert">{error}</p> : null}
        </div>
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>;
}
