export function ReadState({ subject, state }: { subject: string; state: 'loading' | 'unavailable' }) {
  return <div className="read-state" role="status">
    <p>{state === 'loading' ? `正在读取${subject}…` : `${subject}暂不可用`}</p>
    {state === 'loading' ? <div className="read-placeholder" aria-hidden="true">
      <span /><span /><span />
    </div> : <small>连接恢复后会自动读取。</small>}
  </div>;
}
