// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { TaskMessages, type Notice } from './TaskMessages';

it.each([
  ['failed', 'warning', '任务失败', 'error'],
  ['timed_out', 'warning', '任务超时', 'error'],
  ['degraded', 'warning', '结果不完整', 'warning'],
  ['completed', 'info', '任务结果', ''],
  [null, 'warning', '提醒', 'warning'],
  [null, 'critical', '提醒', 'warning'],
  [null, 'info', '任务结果', ''],
  [null, 'error', '任务失败', 'error'],
])('renders task status %s and severity %s without treating ordinary warnings as failures', async (task_status, severity, label, tone) => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  const element = document.createElement('div'); document.body.append(element);
  const root = createRoot(element);
  const notice: Notice = { title: '任务记录', message: '原始内容', severity: severity!, task_status };
  try {
    await act(async () => root.render(createElement(TaskMessages, { notices: [notice], notificationClick: null })));
    expect(element.querySelector('.result-badge')?.textContent).toBe(label);
    expect(element.querySelector('.task-card')?.classList.contains('task-error')).toBe(tone === 'error');
    expect(element.querySelector('.task-card')?.classList.contains('task-warning')).toBe(tone === 'warning');
    expect(element.textContent).toContain('原始内容');
  } finally { await act(async () => root.unmount()); element.remove(); vi.unstubAllGlobals(); }
});
