import { Icon } from './Icon';

export function TurnFeedback({ receipt, pending, sending, active, online, onRetry }: {
  receipt: string; pending: boolean; sending: boolean; active: boolean; online: boolean; onRetry: () => void;
}) {
  const terminal = ['failed', 'unknown', 'interrupted'].includes(receipt);
  if (!terminal && !pending && !active) return null;
  const working = !terminal && online && (pending ? receipt === 'running' : active);
  const confirming = !terminal && !working && sending;
  const title = receipt === 'failed' ? '回复未完成' : receipt === 'unknown' ? '执行结果待核实'
    : receipt === 'interrupted' ? '回复已停止' : !online ? '连接中断，等待恢复'
    : working ? '正在回复' : confirming ? '正在发送' : '发送结果待确认';
  const detail = receipt === 'failed' ? '这次执行未成功，请在电脑查看详情。'
    : receipt === 'unknown' ? '无法确定这条请求是否完成。请先检查历史和执行结果，再决定是否发起新请求。'
    : receipt === 'interrupted' ? '这次执行已停止。已有内容会保留。'
    : !online ? '恢复连接后会同步结果，请勿重复提交。'
    : working ? '收到回复后会显示在这里' : confirming ? '等待电脑确认接收'
    : receipt === 'unconfirmed' ? '尚无接收记录，可用原请求重试。' : '正在核对电脑是否收到消息';
  return <section className={`turn-feedback ${terminal || (!working && !confirming) ? 'needs-attention' : ''}`} aria-label="回复状态">
    <span className="turn-avatar" aria-hidden="true">{terminal ? <Icon name={receipt === 'interrupted' ? 'pause' : 'alert'} /> : <span className="brand-mark" />}</span>
    <div className="turn-feedback-body">
      <div role="status" aria-live="polite" aria-atomic="true"><strong>{title}</strong><p>{detail}</p></div>
      {working || confirming ? <span className="reply-pulse" aria-hidden="true"><i /><i /><i /></span> : null}
      {pending && !working && !sending ? <button type="button" className="text-button retry-message" aria-label="确认并重试原消息" disabled={!online || active} onClick={onRetry}><Icon name="refresh" />确认并重试</button> : null}
    </div>
  </section>;
}
