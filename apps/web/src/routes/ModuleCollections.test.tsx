import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { SkillsPage } from "./SkillsPage";
import { AuditPage } from "./AuditPage";
import type { Skill } from "@/lib/types";
import * as api from "@/lib/workbench-api";

vi.mock("@/lib/workbench-api", () => ({ fetchSkills: vi.fn(), fetchSkillDetail: vi.fn(), pinSkill: vi.fn(), fetchAudit: vi.fn(), fetchAuditEvent: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let view: HTMLDivElement;
let client: QueryClient;
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)); }); };
async function render(page: "skills" | "audit") {
  view = document.createElement("div"); document.body.append(view); root = createRoot(view);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={client}>{page === "skills" ? <SkillsPage /> : <AuditPage />}</QueryClientProvider>));
  await settle();
}
async function fill(label: string, value: string) {
  const input = view.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function click(selector: string) { await act(async () => view.querySelector<HTMLButtonElement>(selector)!.click()); await settle(); }
afterEach(async () => { await act(async () => root?.unmount()); view?.remove(); client?.clear(); vi.resetAllMocks(); });

const common = { created_by: "agent", category: "研究", state: "active" as const, pinned: false, use_count: 3, created_at: "2026-10-20", updated_at: "2026-10-20", last_used_at: null, patch_count: 0 };
const skills: Skill[] = [{ ...common, name: "趋势研究", description: "检查趋势延续条件" }, { ...common, name: "历史风控", description: "风险约束", state: "stale" }];
it("searches skills including stale records, opens full content and keeps pinning separate from selection", async () => {
  vi.mocked(api.fetchSkills).mockResolvedValue({ skills, total: skills.length });
  vi.mocked(api.fetchSkillDetail).mockResolvedValue({ ...skills[0], content: "完整技能内容及失效条件", created_by: "agent" });
  vi.mocked(api.pinSkill).mockResolvedValue();
  await render("skills");
  expect(api.fetchSkills).toHaveBeenCalledWith(true);
  await click('[aria-label="固定 趋势研究"]');
  expect(api.pinSkill).toHaveBeenCalledWith("趋势研究", true);
  expect(api.fetchSkillDetail).not.toHaveBeenCalled();
  await fill("搜索技能", "趋势延续");
  expect(view.querySelectorAll(".skill-open")).toHaveLength(1);
  await click(".skill-open");
  expect(view.querySelector('[aria-label="技能详情"]')?.textContent).toContain("完整技能内容及失效条件");
  expect(view.querySelector(".skill-open")?.getAttribute("aria-pressed")).toBe("true");
  await fill("搜索技能", "未匹配");
  expect(view.textContent).toContain("没有匹配的技能");
  expect(view.querySelector('[aria-label="技能详情"]')?.textContent).toContain("完整技能内容及失效条件");
  await act(async () => [...view.querySelectorAll("button")].find(button => button.textContent === "清除搜索")!.click());
  expect(view.querySelectorAll(".skill-open")).toHaveLength(2);
});

it("distinguishes loading and failed skills from an empty library and offers retry", async () => {
  vi.mocked(api.fetchSkills).mockReturnValue(new Promise(() => {}));
  await render("skills");
  expect(view.textContent).not.toContain("还没有交易技能");
  await act(async () => client.setQueryData(["skills"], { skills: [], total: 0 }));
  vi.mocked(api.fetchSkills).mockRejectedValueOnce(new Error("读取失败"));
  await act(async () => { await client.refetchQueries({ queryKey: ["skills"] }, { cancelRefetch: true }); });
  await settle();
  expect(view.querySelector('[role="alert"]')?.textContent).toContain("无法加载技能");
  expect(view.textContent).not.toContain("还没有交易技能");
  vi.mocked(api.fetchSkills).mockResolvedValue({ skills: [], total: 0 });
  await click('[role="alert"] button');
  expect(view.textContent).toContain("还没有交易技能");
});

it("reveals the exact source without fetching again and resets the disclosure for another skill", async () => {
  const source = '---\nname: 趋势研究\n---\n\n# 原始内容\n\n  保留缩进与空行。\n<script>不执行</script>\n';
  vi.mocked(api.fetchSkills).mockResolvedValue({ skills, total: skills.length });
  vi.mocked(api.fetchSkillDetail).mockImplementation(async name => ({ ...skills.find(skill => skill.name === name)!, content: name === skills[0].name ? source : "另一条技能的完整原文" }));
  await render("skills");
  await click(".skill-open");
  const disclosure = view.querySelector<HTMLDetailsElement>(".document-source")!;
  expect(disclosure.open).toBe(false);
  await click(".document-source summary");
  expect(disclosure.open).toBe(true);
  expect(disclosure.querySelector("pre")?.textContent).toBe(source);
  expect(disclosure.querySelector("pre")?.tabIndex).toBe(0);
  expect(disclosure.querySelector("script")).toBeNull();
  await click(".document-source summary");
  expect(disclosure.open).toBe(false);
  expect(api.fetchSkillDetail).toHaveBeenCalledTimes(1);
  await click(".document-source summary");
  await click(".skill-library-list > li:nth-child(2) .skill-open");
  const next = view.querySelector<HTMLDetailsElement>(".document-source")!;
  expect(next.open).toBe(false);
  expect(next.querySelector("pre")?.textContent).toBe("另一条技能的完整原文");
});

it("filters the recent audit window, opens the exact record and closes its detail", async () => {
  const event = { id: "audit-1", event_type: "decision", timestamp: "2026-10-20T08:00:00Z", summary: "保留研究结论", payload: { source: "verified", message: "保留原始记录" } };
  vi.mocked(api.fetchAudit).mockResolvedValue([event, { ...event, id: "audit-2", event_type: "review", summary: "规则复查" }]);
  vi.mocked(api.fetchAuditEvent).mockResolvedValue(event);
  await render("audit");
  expect(api.fetchAudit).toHaveBeenCalledWith(100);
  await fill("搜索审计记录", "audit-1");
  expect(view.querySelectorAll(".audit-event-open")).toHaveLength(1);
  await click(".audit-event-open");
  expect(api.fetchAuditEvent).toHaveBeenCalledWith("audit-1");
  expect(view.querySelector('[aria-label="事件详情"]')?.textContent).toContain("保留原始记录");
  await click('[aria-label="关闭详情"]');
  expect(view.querySelector('[aria-label="事件详情"]')?.textContent).toContain("选择一条记录");
});

it("moves focus to a selected skill on narrow screens and offers a return to the list", async () => {
  const originalWidth = window.innerWidth;
  const originalScroll = HTMLElement.prototype.scrollIntoView;
  Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });
  HTMLElement.prototype.scrollIntoView = vi.fn();
  try {
    vi.mocked(api.fetchSkills).mockResolvedValue({ skills, total: skills.length });
    vi.mocked(api.fetchSkillDetail).mockResolvedValue({ ...skills[0], content: "完整内容" });
    await render("skills");
    await click(".skill-open");
    expect(document.activeElement?.getAttribute("aria-label")).toBe("技能详情");
    const back = [...view.querySelectorAll("button")].find(button => button.textContent === "返回列表")!;
    await act(async () => back.click());
    expect(document.activeElement?.getAttribute("aria-label")).toBe("技能列表");
    expect(view.querySelector(".skill-open")?.getAttribute("aria-pressed")).toBe("true");
  } finally {
    Object.defineProperty(window, "innerWidth", { value: originalWidth, configurable: true });
    HTMLElement.prototype.scrollIntoView = originalScroll;
  }
});
