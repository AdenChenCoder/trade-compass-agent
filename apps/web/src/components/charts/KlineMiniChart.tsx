import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import * as echarts from "echarts/core";
import { BarChart, CandlestickChart, LineChart } from "echarts/charts";
import {
  GridComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { ECharts, SeriesOption } from "echarts";
import { Loader2 } from "lucide-react";
import { fetchBars } from "@/lib/workbench-api";
import type { ForecastBar } from "@/lib/workbench-api";
import type { Bar } from "@/lib/types";

echarts.use([
  BarChart,
  CandlestickChart,
  LineChart,
  GridComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
]);

interface ForecastOverlay {
  bars: ForecastBar[];
  upperBand?: number[];
  lowerBand?: number[];
}

interface KlineMiniChartProps {
  symbol: string;
  timeframe?: string;
  limit?: number;
  height?: number;
  showMA?: boolean;
  showBollinger?: boolean;
  forecast?: ForecastOverlay;
}

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso.slice(0, 10);
  return date.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

function computeMA(closes: number[], period: number): (number | null)[] {
  return closes.map((_, i) => {
    if (i < period - 1) return null;
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += closes[j];
    return +(sum / period).toFixed(3);
  });
}

function computeBollinger(closes: number[], period = 20, mult = 2) {
  const upper: (number | null)[] = [];
  const lower: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (i < period - 1) {
      upper.push(null);
      lower.push(null);
      continue;
    }
    const slice = closes.slice(i - period + 1, i + 1);
    const mean = slice.reduce((a, b) => a + b, 0) / period;
    const variance = slice.reduce((a, b) => a + (b - mean) ** 2, 0) / period;
    const std = Math.sqrt(variance);
    upper.push(+(mean + mult * std).toFixed(3));
    lower.push(+(mean - mult * std).toFixed(3));
  }
  return { upper, lower };
}

const MA_COLORS: Record<string, string> = {
  MA5: "#f59e0b",
  MA10: "#3b82f6",
  MA20: "#a855f7",
};

export function KlineMiniChart({
  symbol,
  timeframe = "1d",
  limit = 60,
  height = 220,
  showMA = true,
  showBollinger = false,
  forecast,
}: KlineMiniChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);
  const visibilityRef = useRef<HTMLDivElement>(null);
  const [shouldLoad, setShouldLoad] = useState(false);

  useEffect(() => {
    const element = visibilityRef.current;
    if (!element) return;
    if (!("IntersectionObserver" in window)) {
      setShouldLoad(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setShouldLoad(true);
          observer.disconnect();
        }
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const barsQuery = useQuery({
    queryKey: ["bars", symbol, timeframe, limit],
    queryFn: ({ signal }) => fetchBars(symbol, timeframe, limit, signal),
    enabled: shouldLoad && Boolean(symbol.trim()),
    retry: false,
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
  });

  const bars = barsQuery.data?.bars;

  const overlays = useMemo(() => {
    if (!bars?.length) return null;
    const closes = bars.map((b: Bar) => b.close);
    const ma5 = showMA ? computeMA(closes, 5) : [];
    const ma10 = showMA ? computeMA(closes, 10) : [];
    const ma20 = showMA ? computeMA(closes, 20) : [];
    const boll = showBollinger ? computeBollinger(closes) : null;
    return { ma5, ma10, ma20, boll };
  }, [bars, showMA, showBollinger]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !bars?.length || !overlays) return;

    if (!chartRef.current) {
      chartRef.current = echarts.init(container, undefined, { renderer: "canvas" });
    }
    const chart = chartRef.current;
    const categories = bars.map((bar: Bar) => formatDate(bar.timestamp));
    const values = bars.map((bar: Bar) => [bar.open, bar.close, bar.low, bar.high]);
    const volumes = bars.map((bar: Bar) => ({
      value: bar.volume,
      itemStyle: {
        color: bar.close >= bar.open ? "rgba(220,38,38,0.5)" : "rgba(22,163,74,0.5)",
      },
    }));

    const series: SeriesOption[] = [
      {
        name: symbol,
        type: "candlestick",
        data: values,
        itemStyle: {
          color: "#dc2626",
          color0: "#16a34a",
          borderColor: "#dc2626",
          borderColor0: "#16a34a",
        },
        markLine: forecast?.bars.length ? {
          silent: true,
          symbol: "none",
          lineStyle: { color: "#3b82f6", type: "dashed", width: 1 },
          data: [{ xAxis: bars.length - 1 }],
          label: { formatter: "预测→", position: "insideStartTop", fontSize: 9, color: "#3b82f6" },
        } : undefined,
      },
    ];

    if (showMA) {
      const maEntries: [string, (number | null)[]][] = [
        ["MA5", overlays.ma5],
        ["MA10", overlays.ma10],
        ["MA20", overlays.ma20],
      ];
      for (const [name, data] of maEntries) {
        series.push({
          name,
          type: "line",
          data,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1, color: MA_COLORS[name] },
          z: 1,
        });
      }
    }

    if (showBollinger && overlays.boll) {
      series.push({
        name: "BOLL Upper",
        type: "line",
        data: overlays.boll.upper,
        showSymbol: false,
        lineStyle: { width: 0.8, color: "#94a3b8", type: "dashed" },
        z: 0,
      });
      series.push({
        name: "BOLL Lower",
        type: "line",
        data: overlays.boll.lower,
        showSymbol: false,
        lineStyle: { width: 0.8, color: "#94a3b8", type: "dashed" },
        areaStyle: { color: "rgba(148,163,184,0.06)" },
        z: 0,
      });
    }

    // Forecast overlay
    if (forecast?.bars.length) {
      const histLen = bars.length;
      const fcBars = forecast.bars;
      for (const fb of fcBars) {
        categories.push(formatDate(fb.timestamp));
      }

      // Extend candlestick data with forecast bars (different color)
      // ECharts candlestick uses "-" for empty slots (null causes crash)
      const fcValues = fcBars.map((fb) => [fb.open, fb.close, fb.low, fb.high]);
      series.push({
        name: "预测K线",
        type: "candlestick",
        data: [
          ...Array(histLen).fill("-"),
          ...fcValues,
        ],
        itemStyle: {
          color: "rgba(220,38,38,0.45)",
          color0: "rgba(22,163,74,0.45)",
          borderColor: "rgba(220,38,38,0.7)",
          borderColor0: "rgba(22,163,74,0.7)",
        },
        z: 2,
      });

      // Confidence band
      if (forecast.upperBand?.length && forecast.lowerBand?.length) {
        const upperData = [...Array(histLen).fill(null), ...forecast.upperBand];
        const lowerData = [...Array(histLen).fill(null), ...forecast.lowerBand];
        series.push({
          name: "置信上界",
          type: "line",
          data: upperData,
          showSymbol: false,
          lineStyle: { width: 0.6, color: "#3b82f6", type: "dashed" },
          z: 0,
        });
        series.push({
          name: "置信下界",
          type: "line",
          data: lowerData,
          showSymbol: false,
          lineStyle: { width: 0.6, color: "#3b82f6", type: "dashed" },
          areaStyle: { color: "rgba(59,130,246,0.06)" },
          z: 0,
        });
      }

      // Forecast divider line
      const fcVolumes = fcBars.map((fb) => ({
        value: fb.volume,
        itemStyle: { color: "rgba(59,130,246,0.3)" },
      }));
      volumes.push(...fcVolumes);
    }

    series.push({
      name: "Volume",
      type: "bar",
      xAxisIndex: 1,
      yAxisIndex: 1,
      data: volumes,
    });

    chart.setOption({
      animation: false,
      grid: [
        { left: 8, right: 8, top: 16, height: "58%" },
        { left: 8, right: 8, top: "72%", height: "18%" },
      ],
      axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
      },
      xAxis: [
        {
          type: "category",
          data: categories,
          boundaryGap: true,
          axisLine: { onZero: false },
          splitLine: { show: false },
          axisLabel: { show: false },
        },
        {
          type: "category",
          gridIndex: 1,
          data: categories,
          boundaryGap: true,
          axisLine: { onZero: false },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          splitArea: { show: false },
          axisLabel: { fontSize: 10 },
        },
        {
          scale: true,
          gridIndex: 1,
          splitNumber: 2,
          axisLabel: { show: false },
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: false },
        },
      ],
      series,
    }, true);

    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
    };
  }, [bars, overlays, symbol, showMA, showBollinger, forecast]);

  useEffect(() => {
    return () => {
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, []);

  if (!shouldLoad) {
    return (
      <div
        ref={visibilityRef}
        className="flex items-center justify-center rounded-md border bg-muted/10 px-3 text-xs text-muted-foreground"
        style={{ height: 80 }}
      >
        滚动到此处加载 {symbol} K 线
      </div>
    );
  }

  if (barsQuery.isLoading) {
    return (
      <div
        ref={visibilityRef}
        className="flex items-center justify-center rounded-md border bg-muted/20 text-xs text-muted-foreground"
        style={{ height }}
      >
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        加载 K 线…
      </div>
    );
  }

  if (barsQuery.error || !bars?.length) {
    return (
      <div
        ref={visibilityRef}
        className="flex items-center justify-center rounded-md border bg-muted/20 px-3 text-xs text-muted-foreground"
        style={{ height: 80 }}
      >
        暂无 {symbol} 行情数据
      </div>
    );
  }

  return (
    <div ref={visibilityRef} className="overflow-hidden rounded-md border bg-background">
      <div className="border-b px-2 py-1 text-xs text-muted-foreground">
        {symbol} · {timeframe} · {bars.length} 根
        {showMA && <span className="ml-2">
          <span style={{ color: MA_COLORS.MA5 }}>MA5</span>{" "}
          <span style={{ color: MA_COLORS.MA10 }}>MA10</span>{" "}
          <span style={{ color: MA_COLORS.MA20 }}>MA20</span>
        </span>}
        {forecast?.bars.length ? (
          <span className="ml-2 text-blue-500">+ 预测 {forecast.bars.length} 根</span>
        ) : null}
        {barsQuery.data?.provider_name ? ` · ${barsQuery.data.provider_name}` : ""}
      </div>
      <div ref={containerRef} style={{ width: "100%", height }} />
    </div>
  );
}
