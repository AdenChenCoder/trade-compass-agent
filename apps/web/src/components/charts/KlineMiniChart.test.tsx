import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { KlineMiniChart } from "@/components/charts/KlineMiniChart";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

const { fetchBarsMock, chartMock } = vi.hoisted(() => ({
  fetchBarsMock: vi.fn(),
  chartMock: {
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  },
}));

vi.mock("@/lib/workbench-api", () => ({ fetchBars: fetchBarsMock }));
vi.mock("echarts/core", () => ({
  use: vi.fn(),
  init: vi.fn((element: HTMLDivElement) => {
    element.appendChild(document.createElement("canvas"));
    return chartMock;
  }),
}));

class IntersectionObserverMock {
  constructor(private readonly callback: IntersectionObserverCallback) {}

  observe = () => {
    this.callback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  };

  disconnect = vi.fn();
  unobserve = vi.fn();
  takeRecords = vi.fn(() => []);
  root = null;
  rootMargin = "0px";
  thresholds = [0];
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

beforeEach(() => {
  vi.stubGlobal("IntersectionObserver", IntersectionObserverMock);
  fetchBarsMock.mockResolvedValue({
    bars: [
      {
        timestamp: "2026-08-03T00:00:00+08:00",
        open: 100,
        high: 102,
        low: 99,
        close: 101,
        volume: 1_000,
      },
    ],
    provider_name: "test-provider",
  });
});

afterEach(async () => {
  if (root) {
    await act(async () => root?.unmount());
  }
  container?.remove();
  root = null;
  container = null;
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("KlineMiniChart forecast quality", () => {
  it("labels the model output as research assistance rather than a trade signal", async () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    await act(async () => {
      root?.render(
        <QueryClientProvider client={client}>
          <KlineMiniChart
            symbol="600519"
            forecast={{
              bars: [
                {
                  timestamp: "2026-08-04T00:00:00+08:00",
                  open: 101,
                  high: 103,
                  low: 100,
                  close: 102,
                  volume: 1_100,
                },
              ],
              upperBand: [104],
              lowerBand: [99],
              model: "NeoQuasar/Kronos-small",
              qualityStatus: "experimental",
            }}
          />
        </QueryClientProvider>,
      );
    });

    await act(async () => {
      await vi.waitFor(() => {
        expect(container?.textContent).toContain(
          "NeoQuasar/Kronos-small 预测 1 根 · 研究辅助，非交易信号",
        );
      });
    });
  });
});

describe("KlineMiniChart virtualized remount", () => {
  it("renders the cached chart again after its message is unmounted and remounted", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    const mountChart = async () => {
      container = document.createElement("div");
      document.body.appendChild(container);
      root = createRoot(container);

      await act(async () => {
        root?.render(
          <QueryClientProvider client={client}>
            <KlineMiniChart symbol="600519" />
          </QueryClientProvider>,
        );
      });

      await act(async () => {
        await vi.waitFor(() => {
          expect(container?.querySelector("canvas")).not.toBeNull();
        });
      });
    };

    await mountChart();
    expect(fetchBarsMock).toHaveBeenCalledTimes(1);

    await act(async () => root?.unmount());
    container?.remove();
    root = null;
    container = null;

    await mountChart();
    expect(fetchBarsMock).toHaveBeenCalledTimes(1);
  });
});
