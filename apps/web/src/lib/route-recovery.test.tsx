import { act, useState, type ComponentType } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { recoverRoute } from "./route-recovery";

vi.mock("sonner", () => ({ toast: { error: vi.fn(() => "route-failure"), dismiss: vi.fn() } }));
import { toast } from "sonner";

let root: Root | undefined;
let element: HTMLDivElement;
let dispose: (() => void) | undefined;
afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  dispose?.(); element?.remove(); vi.clearAllMocks(); vi.unstubAllGlobals();
});

function Composer() {
  const [draft, setDraft] = useState("");
  const [attachment, setAttachment] = useState("");
  return <><textarea aria-label="草稿" value={draft} onChange={event => setDraft(event.target.value)} />
    <button onClick={() => setAttachment("合成费用.png")}>添加附件</button><p>{attachment}</p></>;
}

async function setup(initial = "/agent", load: () => Promise<{ Component: ComponentType }> = async () => { throw new TypeError("Failed to fetch dynamically imported module: /assets/Settings-old.js"); }) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const router = createMemoryRouter([
    { path: "/agent", Component: Composer },
    { path: "/settings", ...recoverRoute(load, () => {
      if (!router.state.initialized) return "/agent";
      if (router.state.navigation.location?.pathname !== "/settings") return null;
      return router.state.location.pathname;
    }) },
  ], { initialEntries: [initial] });
  dispose = () => router.dispose();
  element = document.createElement("div"); document.body.append(element); root = createRoot(element);
  await act(async () => root!.render(<RouterProvider router={router} />));
  return router;
}

it("keeps the mounted composer and attachment when a route asset is unavailable", async () => {
  const router = await setup();
  const textarea = element.querySelector("textarea")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(textarea, "尚未发送的合成草稿");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    element.querySelector("button")!.click();
  });
  await act(async () => router.navigate("/settings"));
  expect(router.state.location.pathname).toBe("/agent");
  expect(element.querySelector("textarea")).toBe(textarea);
  expect(textarea.value).toBe("尚未发送的合成草稿");
  expect(element.textContent).toContain("合成费用.png");
  expect(element.textContent).not.toContain("Unexpected Application Error");
  expect(toast.error).toHaveBeenCalledTimes(1);
});

it("recovers a cold deep-link failure without redirecting in a loop", async () => {
  const router = await setup("/settings");
  expect(router.state.location.pathname).toBe("/agent");
  expect(element.querySelector("textarea")).not.toBeNull();
  expect(toast.error).toHaveBeenCalledTimes(1);
});

it("does not hide application bugs as an asset update", async () => {
  const error = new Error("application bug");
  await expect(recoverRoute(async () => { throw error; }, () => "/agent").loader()).rejects.toBe(error);
  expect(toast.error).not.toHaveBeenCalled();
});

it("allows a user retry after a transient module failure", async () => {
  let calls = 0;
  const router = await setup("/agent", async () => {
    if (++calls === 1) throw new TypeError("Importing a module script failed.");
    return { Component: () => <p>设置已打开</p> };
  });
  await act(async () => router.navigate("/settings"));
  expect(router.state.location.pathname).toBe("/agent");
  await act(async () => router.navigate("/settings"));
  expect(element.textContent).toBe("设置已打开");
  expect(calls).toBe(2);
  expect(toast.dismiss).toHaveBeenCalledWith("route-failure");
});

it("does not redirect a newer navigation when an older import fails", async () => {
  let fail!: (error: Error) => void;
  const router = await setup("/agent", () => new Promise((_, reject) => { fail = reject; }));
  let pending!: Promise<void>;
  await act(async () => { pending = router.navigate("/settings"); });
  await act(async () => router.navigate("/agent"));
  await act(async () => { fail(new TypeError("Failed to fetch dynamically imported module")); await pending; });
  expect(router.state.location.pathname).toBe("/agent");
  expect(element.querySelector("textarea")).not.toBeNull();
  expect(toast.error).not.toHaveBeenCalled();
});
