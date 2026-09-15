import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { MobileConnection } from "./MobileConnection";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { encodeQR } = vi.hoisted(() => ({ encodeQR: vi.fn(async (_text: string, _options?: object) => "data:image/png;base64,test") }));
vi.mock("qrcode", () => ({ default: { toDataURL: encodeQR } }));
let root: Root;
let container: HTMLDivElement;
let client: QueryClient;

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); });
}
async function render(status: object, access: (enabled: boolean) => object, devices: object[] = []) {
  let state = status;
  vi.stubGlobal("fetch", vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith("/access")) state = access(JSON.parse(String(options?.body)).enabled);
    const value = path.endsWith("/devices") ? { devices }
      : path.endsWith("/pairing/invitations") ? { pwa_url: "https://computer.test.ts.net/mobile/",
        invitation: "one-use-invitation", expires_at: Date.now() / 1000 + 300,
        computer_id: "same-computer", certificate_sha256: "certificate" } : state;
    return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
  }));
  container = document.createElement("div"); document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={client}><MobileConnection /></QueryClientProvider>));
  await settle();
  return async (value: object) => {
    state = value;
    await act(async () => { await client.invalidateQueries({ queryKey: ["mobile-status"] }); });
    await settle();
  };
}
async function click(label: string) {
  const button = [...container.querySelectorAll("button")].find(item => item.textContent === label);
  expect(button).toBeDefined();
  await act(async () => button!.click());
  await settle();
}
afterEach(async () => {
  if (root) await act(async () => root.unmount());
  client?.clear(); container?.remove(); vi.unstubAllGlobals(); encodeQR.mockClear();
});

it("guides enable, provider authorization and one-use QR pairing without manual network fields", async () => {
  const disabled = { enabled: false, requested: false, provider: "tailscale" };
  const update = await render(disabled, enabled => enabled ? { ...disabled, requested: true,
    connection: { phase: "needs_login_or_network", message: "请登录", action_url: "https://login.tailscale.com/test-only" } } : disabled);
  expect(container.querySelector("input")).toBeNull();
  await click("开启手机连接");
  expect(container.textContent).toContain("登录 / 注册");
  expect(container.querySelector("a")?.getAttribute("href")).toBe("https://login.tailscale.com/test-only");
  expect(container.querySelector("img")).toBeNull();
  await update({ enabled: true, requested: true, provider: "tailscale", pwa_url: "https://computer.test.ts.net/mobile/",
    connection: { phase: "ready", message: "手机连接已开启。请用手机扫码，申请配对后在这台电脑批准。", action_url: null } });
  expect(container.querySelector('[role="status"]')?.textContent).toContain("手机连接已开启");
  expect(container.querySelector('[role="status"]')?.textContent).not.toContain("入口已就绪");
  await click("生成配对二维码");
  expect(container.querySelector("img")?.alt).toContain("申请配对");
  expect(encodeQR.mock.calls[0]?.[0]).toMatch(/^https:\/\/computer\.test\.ts\.net\/mobile\/#pair=/);
  expect(container.textContent).toContain("输入电脑显示的六位配对码");
  await click("关闭连接");
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toContain("开启手机连接");
});

it("keeps retry and close available after an interrupted connection, without offering a stale QR", async () => {
  await render({ enabled: false, requested: true, provider: "tailscale",
    connection: { phase: "error", message: "原会话仍会保留", action_url: null } },
    enabled => ({ enabled: false, requested: enabled, provider: "tailscale",
      connection: { phase: "starting", message: "正在准备", action_url: null } }));
  expect(container.textContent).toContain("重新连接");
  expect(container.textContent).toContain("关闭连接");
  expect(container.querySelector("img")).toBeNull();
  await click("重新连接");
  expect(container.textContent).toContain("正在建立安全连接");
});

it("reports computer-side partial failure and recovery without hiding pairing or the approved phone", async () => {
  const state = (phase: string, message: string, reachable: number, checked = 2) => ({ enabled: true, requested: true, provider: "tailscale",
    pwa_url: "https://computer.test.ts.net/mobile/", connection: { phase: "ready", message: "手机连接已开启", action_url: null,
      public_check: { state: phase, message, reachable, checked, checked_at: 1750000000 } } });
  const update = await render(state("partial", "部分公网路径未连通", 1), () => { throw new Error("No connection mutation expected"); },
    [{ device_id: "same-phone", name: "原来的手机", status: "approved", verification_code: "" }]);
  await click("连接另一台");
  const originalQR = container.querySelector("img")?.src;
  for (const [phase, message, reachable, checked] of [
    ["partial", "部分公网路径未连通", 1, 2], ["unreachable", "这台电脑暂未连通公网入口", 0, 2],
    ["stale", "上次检测已过期", 0, 0], ["reachable", "已检测的公网路径均可访问", 2, 2],
  ] as const) {
    await update(state(phase, message, reachable, checked));
    expect(container.textContent).toContain(message);
    expect(container.querySelector('[aria-label="此电脑的公网检测"]')?.closest("details")?.open).toBe(false);
    expect(container.textContent).toContain("手机网络可能不同");
    expect(container.textContent).toContain("原来的手机");
    expect(container.textContent).toContain("已配对");
    expect(container.querySelector("img")?.src).toBe(originalQR);
    expect([...container.querySelectorAll("button")].some(item => item.textContent === "重新连接")).toBe(false);
  }
});


it("shows the code only on desktop and completes pairing without an approval button", async () => {
  const phones = [{ device_id: "new-phone", name: "我的手机", status: "pending", verification_code: "012345" }];
  await render({ enabled: true, requested: true, provider: "tailscale", pwa_url: "https://computer.test.ts.net/mobile/" }, () => ({}), phones);
  expect(container.querySelector('[aria-label="我的手机的配对码"]')?.textContent).toBe("012345");
  expect(container.textContent).toContain("等待手机输入");
  expect(container.textContent).not.toContain("数字一致，批准");
  phones[0] = { ...phones[0], status: "approved" };
  await act(async () => { await client.invalidateQueries({ queryKey: ["mobile-devices"] }); });
  await settle();
  expect(container.textContent).toContain("我的手机已连接");
  expect(container.querySelector('[aria-label="我的手机的配对码"]')).toBeNull();
  expect(vi.mocked(fetch).mock.calls.some(call => String(call[0]).endsWith('/approve'))).toBe(false);
});
