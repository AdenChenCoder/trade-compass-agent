import { act } from "react";
import { createRoot } from "react-dom/client";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { WorkspaceError } from "./WorkspaceError";

it("offers recovery when a route fails without exposing developer diagnostics", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const diagnostics = vi.spyOn(console, "error").mockImplementation(() => {});
  const view = document.createElement("div"); document.body.append(view);
  const root = createRoot(view);
  const router = createMemoryRouter([{ path: "/broken", loader: () => { throw new Error("Failed to fetch dynamically imported module: /assets/private-build.js"); }, element: <p>正常页面</p>, errorElement: <WorkspaceError /> }], { initialEntries: ["/broken"] });
  try {
    await act(async () => root.render(<RouterProvider router={router} />));
    expect(view.textContent).toContain("页面暂时无法打开");
    expect(view.querySelector("button")?.textContent).toContain("重新加载");
    expect(view.querySelector('a[href="/agent"]')).not.toBeNull();
    expect(view.textContent).not.toContain("private-build");
    expect(view.textContent).not.toContain("developer");
  } finally {
    await act(async () => root.unmount()); router.dispose(); view.remove(); diagnostics.mockRestore();
  }
});
