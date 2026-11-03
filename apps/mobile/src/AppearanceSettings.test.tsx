// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { AppearanceSettings } from './AppearanceSettings';
import { APPEARANCE_KEY, initializeAppearance } from '../../shared/appearance';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it('lets the phone choose and restore a locally saved palette without changing its connection', async () => {
  localStorage.setItem('compass.connection', 'existing-pairing');
  const stop = initializeAppearance();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<AppearanceSettings />));
    for (const value of ['ember', 'glacier', 'berry', 'volt']) {
      await act(async () => container.querySelector<HTMLInputElement>(`input[value="${value}"]`)!.click());
      expect(localStorage.getItem(APPEARANCE_KEY)).toBe(value);
      expect(document.documentElement.dataset.theme).toBe(value);
      expect(container.querySelector<HTMLInputElement>(`input[value="${value}"]`)!.checked).toBe(true);
    }
    await act(async () => container.querySelector<HTMLButtonElement>('button')!.click());
    expect(localStorage.getItem(APPEARANCE_KEY)).toBe('classic');
    expect(localStorage.getItem('compass.connection')).toBe('existing-pairing');
  } finally {
    await act(async () => root.unmount());
    container.remove();
    stop();
    localStorage.clear();
  }
});

it('reports an unsaved palette while keeping the current choice usable', async () => {
  const stop = initializeAppearance();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
  try {
    await act(async () => root.render(<AppearanceSettings />));
    await act(async () => container.querySelector<HTMLInputElement>('input[value="glacier"]')!.click());
    expect(document.documentElement.dataset.theme).toBe('glacier');
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('无法保存');
    storage.mockRestore();
    await act(async () => container.querySelector<HTMLInputElement>('input[value="ember"]')!.click());
    expect(localStorage.getItem(APPEARANCE_KEY)).toBe('ember');
    expect(container.querySelector('[role="alert"]')).toBeNull();
  } finally {
    storage.mockRestore();
    await act(async () => root.unmount());
    container.remove(); stop(); localStorage.clear();
  }
});
