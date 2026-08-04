# Chart Remount Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure an Agent conversation K-line chart renders again after its virtualized message is unmounted and remounted while bar data remains cached.

**Architecture:** Preserve the existing virtual-list resource cleanup and React Query cache. Reproduce the cached-remount lifecycle in the component test, then make the chart-rendering effect react to the existing `shouldLoad` readiness transition so it runs after the ECharts container returns to the DOM.

**Tech Stack:** React 19, TypeScript, TanStack React Query, TanStack React Virtual, ECharts 6, Vitest, jsdom, Vite, pnpm.

## Global Constraints

- Do not change bar data, timeframe, K-line limit, moving-average, Bollinger, or forecast-overlay semantics.
- Keep virtualized off-screen messages unmounted and dispose ECharts on component unmount.
- Keep the existing React Query key, `staleTime`, `gcTime`, and delayed first-load behavior.
- Do not add scroll listeners, extra bar requests, backend changes, persistence changes, or dependencies.
- Preserve source-checkout and installed-wheel Web UI behavior by completing a production frontend build and distribution check.

---

### Task 1: Restore the chart on a cached virtual-list remount

**Files:**
- Modify: `apps/web/src/components/charts/KlineMiniChart.test.tsx`
- Modify: `apps/web/src/components/charts/KlineMiniChart.tsx:132-330`
- Verify generated package assets: `src/trade_compass_agent/web_dist/` (ignored Vite build output)

**Interfaces:**
- Consumes: `KlineMiniChart({ symbol, timeframe?, limit?, height?, showMA?, showBollinger?, forecast? })`, the existing QueryClient cache, and the existing Intersection Observer readiness transition.
- Produces: the same `KlineMiniChart` props and user-visible output, with ECharts initialized after `shouldLoad` changes from `false` to `true` on a cached remount.

- [ ] **Step 1: Make the ECharts test double mirror its canvas side effect**

Replace the `echarts/core` mock initializer with a narrow renderer boundary that appends a canvas to the real component container:

```tsx
vi.mock("echarts/core", () => ({
  use: vi.fn(),
  init: vi.fn((element: HTMLDivElement) => {
    element.appendChild(document.createElement("canvas"));
    return chartMock;
  }),
}));
```

This keeps React, React Query, Intersection Observer handling, conditional rendering, and DOM mounting real. The fake replaces only ECharts' jsdom-incompatible canvas renderer.

- [ ] **Step 2: Write the failing cached-remount regression test**

Add this test to `KlineMiniChart.test.tsx`:

```tsx
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
```

Production mutation caught: omitting `shouldLoad` from the chart-rendering effect dependencies leaves the second real chart container without a canvas.

- [ ] **Step 3: Run the test and verify RED**

Run:

```bash
pnpm --dir apps/web test src/components/charts/KlineMiniChart.test.tsx
```

Expected: FAIL in `renders the cached chart again...` because the second container has no `canvas`; the first mount and existing forecast-quality test pass.

- [ ] **Step 4: Implement the minimal lifecycle fix**

In `KlineMiniChart.tsx`, add the already-existing readiness state to the chart-rendering effect dependency list:

```tsx
  }, [bars, overlays, symbol, showMA, showBollinger, forecast, shouldLoad]);
```

Do not change the effect body, the observer, query configuration, conditional UI, or unmount disposal.

- [ ] **Step 5: Run the focused test and verify GREEN**

Run:

```bash
pnpm --dir apps/web test src/components/charts/KlineMiniChart.test.tsx
```

Expected: PASS for both tests, with no React act warnings or unhandled errors. The remount test must still assert one `fetchBars` call.

- [ ] **Step 6: Run the complete frontend verification**

Run each command and require exit code 0:

```bash
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
```

Expected: all Vitest suites pass, TypeScript reports no errors, and Vite writes a production Web UI to `src/trade_compass_agent/web_dist/`.

- [ ] **Step 7: Verify packaged-asset parity**

Run:

```bash
uv build
python scripts/check_dist.py
```

Expected: wheel and sdist build successfully; `check_dist.py` prints `OK` and confirms the bundled Web UI is present.

- [ ] **Step 8: Inspect the surgical diff**

Run:

```bash
git diff --check
git diff -- apps/web/src/components/charts/KlineMiniChart.tsx apps/web/src/components/charts/KlineMiniChart.test.tsx
git status --short
```

Expected: no whitespace errors; only the ECharts test double, cached-remount regression test, one effect dependency, and this plan are changed. Ignored build and distribution outputs do not appear in the commit.

- [ ] **Step 9: Commit the verified implementation**

```bash
git add apps/web/src/components/charts/KlineMiniChart.tsx apps/web/src/components/charts/KlineMiniChart.test.tsx docs/superpowers/plans/2026-08-04-chart-remount-recovery.md
git commit -m "fix: restore charts after virtualized remount"
```
