import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentConnection } from "./AgentConnection";
import { fetchAgentConnection, saveAgentConnection, checkAgentConnection } from "@/lib/agent-api";

vi.mock("@/lib/agent-api", () => ({
  fetchAgentConnection: vi.fn(), saveAgentConnection: vi.fn(), checkAgentConnection: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root | null = null;
let container: HTMLDivElement | null = null;
const settings = { provider: "lmstudio", model: "existing", options: [
  { provider: "lmstudio", label: "LM Studio", model: "existing" },
  { provider: "ollama", label: "Ollama", model: "gemma4:26b" },
] };

async function render(value = settings) {
  vi.mocked(fetchAgentConnection).mockResolvedValue(value);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => { root?.render(<QueryClientProvider client={cache}><AgentConnection /></QueryClientProvider>); });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  return container;
}

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove(); root = null; container = null; vi.resetAllMocks();
});

describe("Agent connection settings", () => {
  it("keeps an unsupported saved connection visible until the user chooses a replacement", async () => {
    const view = await render({ ...settings, provider: "unreleased-provider", model: "previous-model" });
    const select = view.querySelector("select")!;
    expect(select.value).toBe("unreleased-provider");
    expect(select.selectedOptions[0]?.textContent).toBe("当前接入不可用");
    expect(select.selectedOptions[0]?.disabled).toBe(true);
    expect(saveAgentConnection).not.toHaveBeenCalled();
    await act(async () => { select.value = "lmstudio"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(select.value).toBe("lmstudio");
    expect(view.querySelector("input")?.value).toBe("existing");
  });

  it("saves a local provider with its selected model", async () => {
    const view = await render();
    vi.mocked(saveAgentConnection).mockResolvedValue({ ...settings, provider: "ollama", model: "gemma4:26b" });
    const select = view.querySelector("select")!;
    await act(async () => { select.value = "ollama"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(view.querySelector("input")?.value).toBe("gemma4:26b");
    const save = Array.from(view.querySelectorAll("button")).find(button => button.textContent === "保存")!;
    await act(async () => { save.click(); });
    expect(saveAgentConnection).toHaveBeenCalledWith("ollama", "gemma4:26b");
    expect(view.textContent).toContain("将用于后续任务");
  });

  it("retains the selection after a failed save", async () => {
    const view = await render();
    vi.mocked(saveAgentConnection).mockRejectedValue(new Error("无法保存，请重试"));
    const select = view.querySelector("select")!;
    await act(async () => { select.value = "ollama"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    await act(async () => { view.querySelector<HTMLButtonElement>("button")!.click(); });
    expect(view.querySelector("select")?.value).toBe("ollama");
    expect(view.querySelector('[role="alert"]')?.textContent).toBe("无法保存，请重试");
  });

  it("shows configuration state without claiming a successful model request", async () => {
    const view = await render();
    vi.mocked(checkAgentConnection).mockResolvedValue({ status: "configured", message: "已配置本地模型，请确认服务已启动且模型已加载。" });
    const check = Array.from(view.querySelectorAll("button")).find(button => button.textContent === "检查接入状态")!;
    await act(async () => { check.click(); });
    expect(view.querySelector('[role="status"]')?.textContent).toContain("已配置本地模型");
    expect(view.textContent).not.toContain("连接成功");
  });
});
