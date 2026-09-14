import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AutonomousTradingControl } from "./AutonomousTradingControl";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { read, update } = vi.hoisted(() => ({ read: vi.fn(), update: vi.fn() }));
vi.mock("@/lib/workbench-api", () => ({
  fetchAutonomousTrading: read,
  updateAutonomousTrading: update,
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));

let root: Root;
let container: HTMLDivElement;
let client: QueryClient;

async function render() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(<QueryClientProvider client={client}><AutonomousTradingControl /></QueryClientProvider>);
  });
}

async function settle(check: () => void) {
  await act(async () => { await vi.waitFor(check); });
}

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  client?.clear();
  vi.resetAllMocks();
});

describe("global autonomous trading switch", () => {
  it("loads saved state, waits for persistence and lets the user turn it off again", async () => {
    read.mockResolvedValue({ enabled: false });
    let save: (value: { enabled: boolean }) => void = () => {};
    update.mockImplementationOnce(() => new Promise((resolve) => { save = resolve; }));
    await render();
    await settle(() => expect(container.textContent).toContain("已关闭"));
    const toggle = container.querySelector<HTMLButtonElement>('[role="switch"]')!;
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(container.textContent).toContain("关闭后仍可按你的明确指令交易");
    await act(async () => toggle.click());
    await settle(() => expect(toggle.disabled).toBe(true));
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(update).toHaveBeenCalledWith(true, expect.anything());
    await act(async () => save({ enabled: true }));
    await settle(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
    update.mockResolvedValueOnce({ enabled: false });
    await act(async () => toggle.click());
    await settle(() => expect(toggle.getAttribute("aria-checked")).toBe("false"));
  });

  it("keeps the saved state visible when a write fails", async () => {
    read.mockResolvedValue({ enabled: true });
    update.mockRejectedValue(new Error("保存失败，请重试"));
    await render();
    await settle(() => expect(container.textContent).toContain("已开启"));
    const toggle = container.querySelector<HTMLButtonElement>('[role="switch"]')!;
    await act(async () => toggle.click());
    await settle(() => expect(container.querySelector('[role="alert"]')?.textContent).toContain("保存失败"));
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect(toggle.disabled).toBe(false);
  });

  it("does not present a failed settings read as a usable off switch", async () => {
    read.mockRejectedValue(new Error("设置暂不可用"));
    await render();
    await settle(() => expect(container.querySelector('[role="alert"]')?.textContent).toContain("设置暂不可用"));
    const toggle = container.querySelector<HTMLButtonElement>('[role="switch"]')!;
    expect(toggle.disabled).toBe(true);
    expect(toggle.textContent).toBe("不可用");
    expect(update).not.toHaveBeenCalled();
  });
});
