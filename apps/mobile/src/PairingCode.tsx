import { useRef, useState } from 'react';
import { api } from './native';

export function PairingCode({ onVerified }: { onVerified: () => void }) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const sending = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  async function verify(code: string) {
    if (sending.current || code.length !== 6) return;
    sending.current = true; setBusy(true); setError('');
    try {
      await api('pairing/verify', 'POST', { verification_code: code });
      onVerified();
    } catch (err) {
      setError(err instanceof Error ? err.message : '验证未完成，请重试');
      input.current?.focus(); input.current?.select();
    } finally { sending.current = false; setBusy(false); }
  }
  return <form className="pairing-form" onSubmit={event => { event.preventDefault(); void verify(value); }}>
    <label htmlFor="pairing-code">电脑上的六位配对码</label>
    <input ref={input} id="pairing-code" className="code-input" type="text" inputMode="numeric" autoComplete="one-time-code"
      pattern="[0-9]{6}" maxLength={6} autoFocus placeholder="000000" value={value} readOnly={busy}
      aria-invalid={!!error} aria-describedby={error ? 'pairing-error' : 'pairing-help'} onChange={event => {
        const next = event.target.value.replace(/[^0-9]/g, '').slice(0, 6);
        setValue(next); setError(''); if (next.length === 6) void verify(next);
      }} />
    {error ? <p id="pairing-error" className="field-error" role="alert">{error}</p> : null}
    <p id="pairing-help" className="muted" role="status">{busy ? '正在验证，马上就好…' : '输入完整后自动连接，无需在电脑上确认。'}</p>
    {error && value.length === 6 ? <button className="secondary" disabled={busy}>重新验证</button> : null}
  </form>;
}
