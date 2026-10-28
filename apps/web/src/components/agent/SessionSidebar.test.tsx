import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionSidebar } from "@/components/agent/SessionSidebar";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

async function renderSidebar() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  await act(async () => {
    root?.render(
      <SessionSidebar
        sessions={[
          {
            session_id: "session-1",
            title: "你能做什么",
            created_at: "2026-09-16T08:00:00Z",
            updated_at: "2026-09-16T08:05:00Z",
            message_count: 801,
          },
        ]}
        activeSessionId="session-1"
        loading={false}
        streaming={false}
        mobileOpen={false}
        onMobileClose={vi.fn()}
        onNewChat={vi.fn()}
        onSelectSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onRenameSession={vi.fn()}
      />,
    );
  });

  return container;
}

afterEach(async () => {
  if (root) {
    await act(async () => root?.unmount());
  }
  container?.remove();
  root = null;
  container = null;
});

describe("SessionSidebar actions", () => {
  it("reveals hidden actions from the session row hover boundary", async () => {
    const view = await renderSidebar();

    const renameButton = view.querySelector<HTMLButtonElement>(
      'button[aria-label="重命名对话"]',
    );
    const deleteButton = view.querySelector<HTMLButtonElement>(
      'button[aria-label="删除对话"]',
    );

    expect(renameButton).not.toBeNull();
    expect(deleteButton).not.toBeNull();
    expect(renameButton?.className).toContain("session-action");
    expect(deleteButton?.className).toContain("session-action");
    expect(renameButton?.className).toContain("opacity-0");
    expect(deleteButton?.className).toContain("opacity-0");
    expect(renameButton?.parentElement?.className).toContain(
      "[&:hover_.session-action]:opacity-100",
    );
    expect(renameButton?.parentElement?.className).toContain(
      "[&:focus-within_.session-action]:opacity-100",
    );
  });
});
