import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResearchPage } from "./ResearchPage";
import * as api from "@/lib/workbench-api";
import type { ResearchRun } from "@/lib/research-types";

vi.mock("@/lib/workbench-api", () => ({
  fetchResearchStrategies: vi.fn(),
  fetchResearchRuns: vi.fn(),
  saveResearchStrategy: vi.fn(),
  runResearchBacktest: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root | null;
let view: HTMLDivElement;
const settle = () => new Promise((resolve) => setTimeout(resolve, 15));

async function render() {
  vi.mocked(api.fetchResearchStrategies).mockResolvedValue([]);
  vi.mocked(api.fetchResearchRuns).mockResolvedValue([]);
  view = document.createElement("div");
  document.body.appendChild(view);
  root = createRoot(view);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root?.render(
      <QueryClientProvider client={client}>
        <ResearchPage />
      </QueryClientProvider>,
    );
  });
  await act(settle);
}

afterEach(async () => {
  await act(async () => root?.unmount());
  view?.remove();
  root = null;
  vi.resetAllMocks();
});

describe("strategy research journey", () => {
  it("saves a version before running and presents sample results with open holdings", async () => {
    vi.mocked(api.saveResearchStrategy).mockImplementation(async (definition) => ({
      definition,
      version: "a".repeat(16),
      created_at: "2026-10-20T08:00:00Z",
    }));
    vi.mocked(api.runResearchBacktest).mockImplementation(
      async () =>
        ({
          run_id: "b".repeat(32),
          created_at: "2026-10-20T09:00:00Z",
          symbol: "600519",
          evidence_stage: "sample_demo",
          strategy_id: "test",
          strategy_version: "a".repeat(16),
          strategy_snapshot: {
            definition: vi.mocked(api.saveResearchStrategy).mock.calls[0][0],
            version: "a".repeat(16),
            created_at: "2026-10-20T08:00:00Z",
          },
          metadata: {
            source: "sample",
            data_as_of: "2026-10-19T15:00:00+08:00",
            adjustment: "adjusted",
            is_sample: true,
          },
          warnings: ["示例行情不作为策略收益证据。"],
          result: {
            start_date: "2026-09-01",
            end_date: "2026-10-19",
            initial_cash: 100000,
            final_value: 100100,
            cash: 99000,
            total_return_pct: 0.1,
            max_drawdown_pct: -0.2,
            total_trades: 0,
            win_rate: 0,
            daily_nav: [100000, 100100],
            trades: [],
            open_positions: [
              {
                symbol: "600519",
                quantity: 100,
                entry_date: "2026-10-10",
                entry_price: 10,
                mark_price: 11,
                mark_date: "2026-10-19",
                price_is_stale: false,
              },
            ],
            config: {
              position_pct: 0.1,
              stop_loss_pct: 0.08,
              take_profit_pct: 0.2,
              commission_rate: 0.0003,
              min_commission: 5,
              stamp_duty_rate: 0.0005,
              transfer_fee_rate: 0.00001,
              slippage_pct: 0.0005,
            },
          },
        }) as ResearchRun,
    );
    await render();
    const submit = view.querySelector<HTMLButtonElement>(
      'form[aria-label="运行回测"] button[type="submit"]',
    )!;
    expect(submit.disabled).toBe(true);
    await act(async () => {
      view
        .querySelector('form[aria-label="策略假设"]')!
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await settle();
    });
    expect(view.querySelector('[role="status"]')?.textContent).toContain("假设已保存");
    expect(submit.disabled).toBe(false);
    const input = view.querySelector<HTMLInputElement>('input[pattern="[0-9]{6}"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(
        input,
        "600519",
      );
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      view
        .querySelector('form[aria-label="运行回测"]')!
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await settle();
    });
    expect(api.runResearchBacktest).toHaveBeenCalledWith(
      expect.objectContaining({ symbol: "600519", version: "a".repeat(16) }),
      expect.anything(),
    );
    expect(view.textContent).toContain("示例演示");
    expect(view.textContent).toContain("暂无平仓");
    expect(view.textContent).toContain("期末未平仓");
    expect(view.textContent).toContain("100 股");
    expect(view.textContent).toContain("不作为策略收益证据");
  });

  it("keeps a failed save editable without reporting success or enabling a run", async () => {
    vi.mocked(api.saveResearchStrategy).mockRejectedValue(new Error("无法保存，请重试"));
    await render();
    await act(async () => {
      view
        .querySelector('form[aria-label="策略假设"]')!
        .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await settle();
    });
    expect(view.querySelector('[role="alert"]')?.textContent).toContain("无法保存");
    expect(view.querySelector('[role="status"]')).toBeNull();
    expect(
      view.querySelector<HTMLButtonElement>('form[aria-label="运行回测"] button[type="submit"]')!
        .disabled,
    ).toBe(true);
    expect(view.querySelector<HTMLTextAreaElement>("textarea")?.value).toContain("趋势能否延续");
  });
});
