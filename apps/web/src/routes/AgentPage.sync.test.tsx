import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AgentPage } from "./AgentPage";

// JSDOM has no layout. Render every virtual row so assertions cover the user's history.
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getVirtualItems: () => Array.from({ length: count }, (_, index) => ({ index, key: index, start: index * 100 })),
    getTotalSize: () => count * 100,
    measureElement: () => {}, scrollToIndex: () => {},
  }),
}));

let root: Root; let element: HTMLDivElement; let count: number;
let failBefore: number | null; let cursors: number[];
let extraMessages: { role: string; content: string }[];
const contents = (size: number) => Array.from({ length: size }, (_, i) => `历史消息 ${i}`);
const rendered = () => [...element.querySelectorAll('[data-testid="agent-message-list"] [data-message-index]')].map(node => node.textContent?.trim());
const sync = async () => { await act(async () => { await vi.advanceTimersByTimeAsync(2000); }); };

beforeEach(async () => {
  vi.useFakeTimers(); count = 50; failBefore = null; cursors = []; extraMessages = [];
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  localStorage.clear(); localStorage.setItem("trade-compass-session-id", "original");
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    if (path.includes("/messages")) {
      const query = new URL(path, "http://localhost").searchParams;
      const transcript = [...contents(count).map(content => ({ role: "assistant", content })), ...extraMessages];
      const end = query.has("before") ? Number(query.get("before")) : transcript.length;
      if (query.has("before")) cursors.push(end);
      if (query.has("before") && end === failBefore) return new Response("Unavailable", { status: 503 });
      const start = Math.max(0, end - Number(query.get("limit")));
      return Response.json({ session_id: "original", updated_at: "2026-09-14", has_active_turn: false,
        messages: transcript.slice(start, end),
        page: { start_index: start, next_before: start || null, total_messages: transcript.length } });
    }
    return Response.json({ skills: [], sessions: [] });
  }));
  element = document.createElement("div"); document.body.append(element); root = createRoot(element);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={queryClient}><AgentPage /></QueryClientProvider>));
});
afterEach(async () => {
  await act(async () => root.unmount()); element.remove();
  vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); localStorage.clear();
});

it.each([60, 150, 275])("retains a continuous desktop transcript after syncing %s messages", async size => {
  count = size; await sync();
  expect(rendered()).toEqual(contents(size));
});

it("keeps visible history on a gap-fetch failure and fills it on the next sync", async () => {
  count = 250; failBefore = 150; await sync();
  expect(cursors).toContain(150);
  expect(rendered()).toEqual(contents(50));
  failBefore = null; await sync();
  expect(rendered()).toEqual(contents(250));
});

const draft = async (text: string) => {
  const input = element.querySelector('textarea')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, text);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};
const submit = async () => {
  await act(async () => { element.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
};

it.each([503, 502, 409])("preserves a %s failure as a draft through sync and removes the error after a successful retry", async code => {
  vi.stubGlobal('EventSource', class { addEventListener() {} close() {} });
  const existingFetch = fetch;
  let retry = false; let posts = 0;
  vi.stubGlobal('fetch', vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith('/turn') && options?.method === 'POST') {
      posts++;
      if (!retry) return Response.json({ detail: '本次请求未执行' }, { status: code });
      extraMessages = [{ role: 'user', content: '失败后需要保留的消息' }, { role: 'assistant', content: '重试成功的回复' }];
      return Response.json({ session_id: 'original', turn_id: 'retry-turn', summary: '重试成功的回复', sections: [] });
    }
    return existingFetch(path, options);
  }));
  await draft('失败后需要保留的消息'); await submit();
  expect(element.textContent).toContain('本次请求未执行');
  await sync(); await sync();
  expect(element.querySelector('textarea')!.value).toBe('失败后需要保留的消息');
  expect(element.textContent).toContain('本次请求未执行');
  expect(posts).toBe(1);
  expect(rendered()).toEqual([...contents(50), code === 503 ? '本次请求未执行（请配置 LLM API key）' : '本次请求未执行']);
  retry = true; await submit(); await sync();
  expect(rendered()).toEqual([...contents(50), ...extraMessages.map(m => m.content)]);
  expect(element.textContent).not.toContain('本次请求未执行');
  expect(element.querySelector('textarea')!.value).toBe('');
  expect(posts).toBe(2);
});

it('keeps the failed draft without duplicating a user message already saved by the computer', async () => {
  vi.stubGlobal('EventSource', class { addEventListener() {} close() {} });
  const existingFetch = fetch;
  vi.stubGlobal('fetch', vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith('/turn') && options?.method === 'POST') {
      extraMessages = [{ role: 'user', content: '已保存但执行失败' }];
      return Response.json({ detail: '执行失败' }, { status: 502 });
    }
    return existingFetch(path, options);
  }));
  await draft('已保存但执行失败'); await submit(); await sync();
  expect(rendered()).toEqual([...contents(50), '已保存但执行失败', '执行失败']);
  expect(element.querySelector('textarea')!.value).toBe('已保存但执行失败');
  expect(element.textContent).toContain('执行失败');
});

it('does not restore a sent draft when streaming already confirmed success before an HTTP error', async () => {
  const listeners = new Map<string, (event: MessageEvent) => void>();
  vi.stubGlobal('EventSource', class {
    addEventListener(name: string, listener: (event: MessageEvent) => void) { listeners.set(name, listener); }
    close() {}
  });
  const existingFetch = fetch;
  let respond!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith('/turn') && options?.method === 'POST') {
      return await new Promise<Response>(resolve => { respond = resolve; });
    }
    return existingFetch(path, options);
  }));
  await draft('已通过流式回复完成'); await submit();
  extraMessages = [{ role: 'user', content: '已通过流式回复完成' }, { role: 'assistant', content: '已经完成' }];
  await act(async () => {
    listeners.get('done')!(new MessageEvent('done', { data: JSON.stringify({ ok: true, summary: '已经完成', sections: [] }) }));
  });
  await act(async () => { respond(Response.json({ detail: '迟到的网关错误' }, { status: 502 })); });
  await sync();
  expect(element.querySelector('textarea')!.value).toBe('');
  expect(rendered()).toEqual([...contents(50), ...extraMessages.map(m => m.content)]);
  expect(element.textContent).not.toContain('迟到的网关错误');
});
