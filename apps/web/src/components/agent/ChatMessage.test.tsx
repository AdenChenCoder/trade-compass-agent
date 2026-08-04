import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMessage } from "@/components/agent/ChatMessage";
import { ApiError } from "@/lib/agent-api";
import type { ChatMessage as ChatMessageType } from "@/lib/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const { fetchForecastMock } = vi.hoisted(() => ({
  fetchForecastMock: vi.fn(),
}));

vi.mock("@/lib/workbench-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/workbench-api")>()),
  fetchForecast: fetchForecastMock,
}));

vi.mock("@/components/charts/KlineMiniChart", () => ({
  KlineMiniChart: ({ forecast }: { forecast?: unknown }) => (
    <div>{forecast ? "预测叠加行情" : "仅历史行情"}</div>
  ),
}));

let root: Root | null = null;
let container: HTMLDivElement | null = null;

async function renderMessage(message: ChatMessageType) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  await act(async () => {
    root?.render(
      <QueryClientProvider client={client}>
        <ChatMessage message={message} />
      </QueryClientProvider>,
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
  fetchForecastMock.mockReset();
});

describe("ChatMessage forecast failures", () => {
  it("keeps the answer and shows the installed-user recovery command", async () => {
    fetchForecastMock.mockRejectedValue(
      new ApiError("预测引擎尚未安装。", 503, {
        code: "forecast_unavailable",
        recoveryCommand: "uv tool install forecast",
        restartRequired: true,
      }),
    );

    const view = await renderMessage({
      id: "assistant-1",
      role: "assistant",
      content: "贵州茅台（600519）预测结果将在此显示",
      timestamp: Date.now(),
    });

    await act(async () => {
      await vi.waitFor(
        () => {
          expect(view.textContent).toContain("预测暂不可用");
        },
        { timeout: 3_000 },
      );
    });
    expect(view.textContent).toContain("贵州茅台（600519）预测结果将在此显示");
    expect(view.textContent).toContain("uv tool install forecast");
    expect(view.textContent).toContain("仅历史行情");
  });

  it("ignores malformed embedded forecast data without losing section content", async () => {
    const view = await renderMessage({
      id: "assistant-2",
      role: "assistant",
      content: "",
      timestamp: Date.now(),
      sections: [
        {
          title: "旧预测记录",
          content: "保留这段历史内容",
          kind: "narrative",
          forecast_data: { forecast_bars: [{}] } as never,
        },
      ],
    });

    expect(view.textContent).toContain("保留这段历史内容");
  });
});
