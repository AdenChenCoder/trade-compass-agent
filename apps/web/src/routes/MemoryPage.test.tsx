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

async function showHistory(entries: object[]) {
  read.mockResolvedValue({ chars_used: 12, char_limit: 3000, entries });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={client}><MemoryPage /></QueryClientProvider>));
  for (let i = 0; i < 10 && !container.textContent?.includes("12/3000"); i++) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });
  }
  const history = [...container.querySelectorAll("button")].find((b) => b.textContent?.startsWith("历史"))!;
  await act(async () => history.click());
}

const historical = { index: 1, entry_id: "old", text: "历史判断", status: "archived", source: "curator",
  confidence: .85, access_count: 3, version: 1, evidence: [], pinned: false, needs_review: true };

it("explains exact deduplication and shows the retained text outside the history filter", async () => {
  await showHistory([{ ...historical, reason: "duplicate_of:kept", change_kind: "deduplicated", review_method: "",
    successors: [{ entry_id: "kept", version: 1, text: "保留的完整原文及全部条件", status: "active" }], lineage_status: "complete" }]);
  expect(container.textContent).toContain("完全重复，已保留一条");
  expect(container.textContent).toContain("置信度 85%");
  expect(container.textContent).toContain("访问 3 次");
  expect(container.textContent).not.toContain("待复评");
  expect(container.textContent).not.toContain("duplicate_of:");
  const details = container.querySelector("details")!;
  expect(details.open).toBe(false);
  await act(async () => details.querySelector("summary")!.click());
  expect(details.open).toBe(true);
  expect(details.textContent).toContain("未调用 AI 重写正文");
  expect(details.textContent).toContain("保留的完整原文及全部条件");
  expect(details.textContent).toContain("记录 ID：kept");
  expect(details.textContent).toContain("有效 · v1");
  expect(container.textContent).toContain("12/3000");
});

it("shows the reviewed merge result and subsequent version separately", async () => {
  await showHistory([{ ...historical, reason: "保留条件和例外", change_kind: "merged", review_method: "ai",
    successors: [
      { entry_id: "merged", version: 1, text: "当时合并后的完整内容", status: "archived" },
      { entry_id: "merged", version: 2, text: "后来修订的当前有效内容", status: "active" },
    ], lineage_status: "complete" }]);
  expect(container.textContent).toContain("经 AI 审查后合并");
  const details = container.querySelector("details")!;
  await act(async () => details.querySelector("summary")!.click());
  expect(details.textContent).toContain("变更原因：保留条件和例外");
  expect(details.textContent).toContain("当时合并后的完整内容");
  expect(details.textContent).toContain("后来修订的当前有效内容");
  expect(details.textContent).toContain("后续版本 · 有效 · v2");
});

it.each([
  ["unavailable", "保留或替代记录缺失"],
  ["ambiguous", "旧记录未注明替代版本"],
  ["cycle", "历史关联存在循环"],
])("shows %s history honestly without claiming AI involvement", async (lineage_status, message) => {
  await showHistory([{ ...historical, reason: "legacy_superseded", change_kind: "", review_method: "", successors: [], lineage_status }]);
  const details = container.querySelector("details")!;
  await act(async () => details.querySelector("summary")!.click());
  expect(details.textContent).toContain(message);
  expect(details.textContent).toContain("历史记录未注明是否经过 AI 审查");
  expect(container.textContent).not.toContain("经 AI 审查后合并");
  expect(container.textContent).not.toContain("待复评");
});
