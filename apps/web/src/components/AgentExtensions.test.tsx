import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { AgentExtensions } from "./AgentExtensions";
import * as api from "@/lib/agent-api";

vi.mock("@/lib/agent-api", () => ({ fetchAgentSkills: vi.fn(), fetchAgentMcp: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let view: HTMLDivElement; let root: Root; let client: QueryClient;
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 15)); }); };
async function render() {
  view = document.createElement("div"); document.body.append(view); root = createRoot(view);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={client}><AgentExtensions /></QueryClientProvider>)); await settle();
}
afterEach(async () => { await act(async () => root?.unmount()); view?.remove(); client?.clear(); vi.resetAllMocks(); });

it("keeps complete skill identities, descriptions and fallback sources with read-only enabled states", async () => {
  const description = "保留支持与相反证据、观察日期和完整的失效条件。".repeat(8);
  vi.mocked(api.fetchAgentSkills).mockResolvedValue({ skills: [
    { name: "autonomous-paper-trading-with-a-long-name", description, source: "project", enabled: true },
    { name: "disabled-skill", description: "尚未启用的技能", path: "/a/long/path/SKILL.md", enabled: false },
    { name: "legacy-skill", source: "external-source" },
  ] });
  vi.mocked(api.fetchAgentMcp).mockResolvedValue({ servers: [] });
  await render();
  const rows = view.querySelectorAll('.extension-skill');
  expect(rows).toHaveLength(3);
  expect(rows[0].querySelector('h3')?.textContent).toBe("autonomous-paper-trading-with-a-long-name");
  expect(rows[0].querySelector('.extension-description')?.textContent).toBe(description);
  expect(rows[0].textContent).toContain("项目技能");
  expect(rows[1].textContent).toContain("/a/long/path/SKILL.md");
  expect(rows[1].querySelector('.extension-state')?.textContent).toBe("未启用");
  expect(rows[2].textContent).toContain("external-source");
  expect(rows[2].querySelector('.extension-state')?.textContent).toBe("已启用");
  expect(view.querySelector('button,input')).toBeNull();
  expect(view.textContent).toContain("暂未配置外部工具服务");
});

it("distinguishes configured, connected, unknown and failed servers while preserving tool access and errors", async () => {
  vi.mocked(api.fetchAgentSkills).mockResolvedValue({ skills: [] });
  vi.mocked(api.fetchAgentMcp).mockResolvedValue({ servers: [
    { name: "pending", status: "configured", command: "https://example.test/mcp", tools: [] },
    { name: "ready", status: "connected", tools: ["quote", "history"] },
    { name: "failed", status: "error", error: "无法读取工具列表，连接已中断" },
    { name: "future", status: "auth_required" },
  ] });
  await render();
  const rows = view.querySelectorAll('.extension-server');
  expect(rows[0].querySelector('.extension-state')?.textContent).toBe("已配置");
  expect(rows[0].querySelector('.extension-state')?.getAttribute('data-tone')).toBe("neutral");
  expect(rows[0].textContent).toContain("https://example.test/mcp");
  expect(rows[0].textContent).not.toContain("0 个工具");
  expect(rows[1].querySelector('.extension-state')?.textContent).toBe("已连接");
  const details = rows[1].querySelector('details')!;
  expect(details.open).toBe(false);
  await act(async () => details.querySelector('summary')!.click());
  expect(details.open).toBe(true);
  expect([...details.querySelectorAll('code')].map(e => e.textContent)).toEqual(["quote", "history"]);
  expect(rows[2].querySelector('[role="alert"]')?.textContent).toBe("无法读取工具列表，连接已中断");
  expect(rows[3].querySelector('.extension-state')?.textContent).toBe("auth_required");
});

it("keeps unresolved and failed loads distinct from confirmed emptiness and retries independently", async () => {
  vi.mocked(api.fetchAgentSkills).mockReturnValue(new Promise(() => {}));
  vi.mocked(api.fetchAgentMcp).mockRejectedValue(new Error("服务读取失败"));
  await render();
  expect(view.querySelector('[aria-label="正在加载技能"]')).not.toBeNull();
  expect(view.textContent).not.toContain("暂无已加载的技能");
  expect(view.textContent).not.toContain("暂未配置外部工具服务");
  expect(view.querySelector('[role="alert"]')?.textContent).toContain("无法加载外部工具");
  vi.mocked(api.fetchAgentMcp).mockResolvedValue({ servers: [] });
  await act(async () => view.querySelector<HTMLButtonElement>('[role="alert"] button')!.click()); await settle();
  expect(view.textContent).toContain("暂未配置外部工具服务");
  expect(view.querySelector('[aria-label="正在加载技能"]')).not.toBeNull();
});
