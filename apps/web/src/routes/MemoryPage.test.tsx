import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryPage } from "./MemoryPage";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { read } = vi.hoisted(() => ({ read: vi.fn() }));
vi.mock("@/lib/workbench-api", () => ({ fetchMemory: read }));
let root: Root;
let container: HTMLDivElement;
let client: QueryClient;
afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  client?.clear();
  vi.clearAllMocks();
});
it("shows only effective capacity and keeps candidate and retired memories accessible", async () => {
  const common = { confidence: .85, access_count: 0, source: "promotion", version: 1, evidence: [], pinned: false, needs_review: false };
  read.mockResolvedValue({ chars_used: 6, char_limit: 3000, entries: [
    { ...common, index: 0, entry_id: "a", text: "有效的判断原则", status: "active", reason: "admitted" },
    { ...common, index: 1, entry_id: "b", text: "仍待验证的发现", status: "candidate", reason: "awaiting_evidence" },
    { ...common, index: 2, entry_id: "c", text: "已被替代的旧判断", status: "archived", reason: "条件已失效" },
  ] });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={client}><MemoryPage /></QueryClientProvider>));
  for (let i = 0; i < 10 && !container.textContent?.includes("有效的判断原则"); i++) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
  }
  expect(container.textContent).toContain("6/3000");
  expect(container.textContent).toContain("有效的判断原则");
  expect(container.textContent).not.toContain("仍待验证的发现");
  const select = async (label: string) => {
    const button = [...container.querySelectorAll("button")].find((b) => b.textContent?.startsWith(label))!;
    await act(async () => button.click());
  };
  await select("候选");
  expect(container.textContent).toContain("仍待验证的发现");
  expect(container.textContent).toContain("等待更多证据");
  await select("历史");
  expect(container.textContent).toContain("已被替代的旧判断");
  expect(container.textContent).toContain("条件已失效");
  expect(container.textContent).toContain("6/3000");
});
