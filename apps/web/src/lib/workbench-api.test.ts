import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/agent-api";
import { fetchForecast, saveResearchStrategy } from "@/lib/workbench-api";
import type { StrategyDefinition } from "@/lib/research-types";

afterEach(() => vi.unstubAllGlobals());

it("shows the server's research validation reason when a save is rejected", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    detail: [{ loc: ["body", "parameters"], msg: "快均线窗口必须小于慢均线窗口" }],
  }), { status: 422, statusText: "Unprocessable Entity" })));
  await expect(saveResearchStrategy({} as StrategyDefinition)).rejects.toMatchObject({
    message: "快均线窗口必须小于慢均线窗口", status: 422,
  });
});

describe("fetchForecast", () => {
  it("rejects the legacy HTTP-200 error object before render", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "PyTorch not installed" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(fetchForecast("600519", 5)).rejects.toMatchObject({
      name: "ApiError",
      code: "invalid_forecast_response",
      status: 502,
    });
  });

  it("preserves structured recovery metadata from a 503", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "forecast_unavailable",
              message: "预测引擎尚未安装。",
              recovery: {
                command: "uv tool install forecast",
                restart_required: true,
              },
            },
          }),
          {
            status: 503,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    const error = await fetchForecast("600519", 5).catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      code: "forecast_unavailable",
      recoveryCommand: "uv tool install forecast",
      restartRequired: true,
    });
  });
});
