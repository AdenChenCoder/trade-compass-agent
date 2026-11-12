// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it, vi } from 'vitest';
import { Markdown } from './Markdown';

it('opens only the selected message source, retains prose and keeps previews noninteractive', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  const scroll = vi.fn();
  vi.stubGlobal('HTMLElement', HTMLElement);
  HTMLElement.prototype.scrollIntoView = scroll;
  const element = document.createElement('div'); document.body.append(element);
  const root = createRoot(element);
  const text = '价格走高，后续方向仍不确定。[^source-1]\n\n[^source-1]: 002415，2026-11-10，10元/股。';
  try {
    await act(async () => root.render(createElement('div', null,
      createElement(Markdown, { text }), createElement(Markdown, { text }),
      createElement(Markdown, { text, interactive: false }))));
    const readers = element.querySelectorAll('.markdown');
    const links = element.querySelectorAll<HTMLAnchorElement>('a[data-footnote-ref]');
    expect(links).toHaveLength(2);
    expect(links[0].id).not.toBe(links[1].id);
    const first = readers[0].querySelector('details')!;
    const second = readers[1].querySelector('details')!;
    expect(first.open).toBe(false); expect(second.open).toBe(false);
    await act(async () => links[1].click());
    expect(first.open).toBe(false); expect(second.open).toBe(true);
    expect(document.activeElement).toBe(readers[1].querySelector('li'));
    expect(scroll).toHaveBeenCalled();
    const back = readers[1].querySelector<HTMLAnchorElement>('a[data-footnote-backref]')!;
    await act(async () => back.click());
    expect(document.activeElement).toBe(links[1]);
    expect(readers[2].querySelector('a, details, button')).toBeNull();
    expect(readers[1].textContent).toContain('价格走高，后续方向仍不确定');
  } finally {
    await act(async () => root.unmount()); element.remove(); vi.unstubAllGlobals();
    delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
  }
});
