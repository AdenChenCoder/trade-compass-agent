import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { APPEARANCE_KEY, appearances, getAppearance, initializeAppearance } from "../../../shared/appearance";
import { AppearanceSettings } from "./AppearanceSettings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
let root: Root;
let stop: () => void;

beforeEach(async () => {
  localStorage.clear();
  localStorage.setItem("conversation-draft", "未发送的研究问题");
  container = document.createElement("div");
  document.body.append(container);
  stop = initializeAppearance();
  root = createRoot(container);
  await act(async () => root.render(<AppearanceSettings />));
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  stop();
  vi.restoreAllMocks();
  localStorage.clear();
});

it("applies each palette, restores it on startup and keeps unrelated drafts", async () => {
  expect(container.querySelector('[role="status"]')?.textContent).toContain("默认配色");
  for (const theme of appearances) {
    await act(async () => container.querySelector<HTMLInputElement>(`input[value="${theme.id}"]`)!.click());
    expect(document.documentElement.dataset.theme).toBe(theme.id);
    expect(document.documentElement.classList.contains("dark")).toBe(theme.id === "glacier");
    expect(localStorage.getItem(APPEARANCE_KEY)).toBe(theme.id);
    expect(container.querySelector('[role="status"]')?.textContent).toContain(theme.name);
    stop();
    await act(async () => { stop = initializeAppearance(); });
    expect(container.querySelector<HTMLInputElement>(`input[value="${theme.id}"]`)?.checked).toBe(true);
  }
  expect(localStorage.getItem("conversation-draft")).toBe("未发送的研究问题");
  await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
  expect(getAppearance()).toBe("classic");
  expect(document.documentElement.classList.contains("dark")).toBe(false);
  expect(localStorage.getItem(APPEARANCE_KEY)).toBe("classic");
});

it("receives another tab's choice and safely handles removal or unknown saved values", async () => {
  await act(async () => window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY, newValue: "glacier", storageArea: localStorage })));
  expect(container.querySelector<HTMLInputElement>('input[value="glacier"]')?.checked).toBe(true);
  expect(document.documentElement.classList.contains("dark")).toBe(true);
  await act(async () => window.dispatchEvent(new StorageEvent("storage", { key: "conversation-draft", newValue: "berry" })));
  expect(getAppearance()).toBe("glacier");
  for (const value of ["retired-theme", null]) {
    await act(async () => window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY, newValue: value })));
    expect(getAppearance()).toBe("classic");
  }
  localStorage.setItem(APPEARANCE_KEY, "unknown");
  stop();
  await act(async () => { stop = initializeAppearance(); });
  expect(getAppearance()).toBe("classic");
});

it("still applies a choice when storage is unavailable and explains that it was not saved", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage disabled"); });
  await act(async () => container.querySelector<HTMLInputElement>('input[value="berry"]')!.click());
  expect(document.documentElement.dataset.theme).toBe("berry");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("无法保存");
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Storage disabled"); });
  stop();
  await act(async () => { stop = initializeAppearance(); });
  expect(getAppearance()).toBe("classic");
});
