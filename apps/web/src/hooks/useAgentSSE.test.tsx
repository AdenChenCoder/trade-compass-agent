import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAgentSSE } from "./useAgentSSE";

class FakeSource extends EventTarget {
  static instances: FakeSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;
  constructor(readonly url: string) { super(); FakeSource.instances.push(this); }
  close() { this.closed = true; }
  networkError() { this.dispatchEvent(new Event("error")); this.onerror?.(); }
}

let root: Root;
let host: HTMLDivElement;
let api: ReturnType<typeof useAgentSSE>;
function Harness() { api = useAgentSSE(); return null; }

beforeEach(async () => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("EventSource", FakeSource);
  FakeSource.instances = [];
  host = document.createElement("div"); root = createRoot(host);
  await act(async () => root.render(<Harness />));
});
afterEach(async () => {
  api.disconnect(); await act(async () => root.unmount());
  vi.unstubAllGlobals(); vi.useRealTimers();
});

it("reconnects a transport error without reporting a failed agent task", async () => {
  const error = vi.fn(); const reconnect = vi.fn();
  api.connect("/api/agent/stream?session_id=test", { error, reconnect });
  FakeSource.instances[0].networkError();
  expect(error).not.toHaveBeenCalled();
  expect(reconnect).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(1000);
  expect(FakeSource.instances).toHaveLength(2);
});

it.each(["done", "interrupted", "error"])("delivers %s once and closes a terminal stream", async event => {
  const terminal = vi.fn(); const error = vi.fn(); const reconnect = vi.fn();
  api.connect("/api/agent/stream?session_id=test", { error, reconnect, [event]: terminal });
  const source = FakeSource.instances[0];
  source.dispatchEvent(new MessageEvent(event, {
    data: JSON.stringify({ summary: event === "interrupted" ? "[已停止]" : "结束", message: "运行时错误" }),
    lastEventId: "evt-3",
  }));
  source.networkError();
  await vi.advanceTimersByTimeAsync(30_000);
  expect(terminal).toHaveBeenCalledOnce();
  expect(source.closed).toBe(true);
  expect(reconnect).not.toHaveBeenCalled();
  expect(FakeSource.instances).toHaveLength(1);
  if (event !== "error") expect(error).not.toHaveBeenCalled();
});
