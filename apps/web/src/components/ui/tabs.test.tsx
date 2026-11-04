import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./tabs";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("supports keyboard selection with matching labelled panels and wraps focus", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Tabs defaultValue="knowledge"><TabsList><TabsTrigger value="knowledge">知识</TabsTrigger><TabsTrigger value="history">历史</TabsTrigger></TabsList><TabsContent value="knowledge">当前知识</TabsContent><TabsContent value="history">历史仍可访问</TabsContent></Tabs>));
    const tabs = [...container.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
    tabs[0].focus();
    await act(async () => tabs[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    expect(document.activeElement).toBe(tabs[1]);
    expect(tabs[1].getAttribute("aria-selected")).toBe("true");
    expect(container.querySelector('[role="tabpanel"]')?.textContent).toBe("历史仍可访问");
    expect(container.querySelector('[role="tabpanel"]')?.getAttribute("aria-labelledby")).toBe(tabs[1].id);
    await act(async () => tabs[1].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    expect(document.activeElement).toBe(tabs[0]);
    expect(tabs[1].tabIndex).toBe(-1);
    await act(async () => tabs[0].dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })));
    expect(document.activeElement).toBe(tabs[1]);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it("keeps an unsaved field when switching mounted panels and hides inactive controls", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Tabs defaultValue="edit"><TabsList><TabsTrigger value="edit">编辑</TabsTrigger><TabsTrigger value="preview">预览</TabsTrigger></TabsList><TabsContent value="edit" forceMount><input aria-label="草稿" defaultValue="" /></TabsContent><TabsContent value="preview" forceMount>已保存的内容</TabsContent></Tabs>));
    const field = container.querySelector("input")!;
    field.value = "尚未保存的草稿";
    const tabs = container.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    await act(async () => tabs[1].click());
    expect(field.closest<HTMLElement>('[role="tabpanel"]')?.hidden).toBe(true);
    expect(container.querySelector('[role="tabpanel"]:not([hidden])')?.textContent).toBe("已保存的内容");
    await act(async () => tabs[0].click());
    expect(container.querySelector("input")).toBe(field);
    expect(field.value).toBe("尚未保存的草稿");
    expect(field.closest<HTMLElement>('[role="tabpanel"]')?.hidden).toBe(false);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
