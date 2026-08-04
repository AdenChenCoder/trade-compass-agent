import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/agent-api";
import { fetchForecast } from "@/lib/workbench-api";

afterEach(() => vi.unstubAllGlobals());

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
