import { lazy, Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import { ForecastUnavailableNotice } from "@/components/agent/ForecastFeedback";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  isForecastPayload,
  shouldRetryForecast,
  toForecastOverlay,
} from "@/lib/forecast";
import { fetchForecast } from "@/lib/workbench-api";
import type { TurnSection } from "@/lib/types";

const Markdown = lazy(async () => {
  const module = await import("@/components/ui/MarkdownRenderer");
  return { default: module.MarkdownRenderer };
});

const KlineMiniChart = lazy(async () => {
  const module = await import("@/components/charts/KlineMiniChart");
  return { default: module.KlineMiniChart };
});

function useForecast(symbol: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["forecast", symbol],
    queryFn: () => fetchForecast(symbol!, 5),
    enabled: Boolean(symbol && enabled),
    staleTime: 5 * 60_000,
    retry: shouldRetryForecast,
  });
}

export function SectionCard({ section }: { section: TurnSection }) {
  const chartSymbol = section.symbols?.[0];
  const showChart = Boolean(chartSymbol) && section.kind === "summary";

  const embeddedForecast = isForecastPayload(section.forecast_data)
    ? section.forecast_data
    : undefined;
  const hasEmbeddedForecast = Boolean(embeddedForecast);

  const hasForecastContent =
    !hasEmbeddedForecast &&
    (/预测|forecast|kline_forecast/i.test(section.content) ||
      /预测|forecast/i.test(section.title));

  const forecastQuery = useForecast(
    chartSymbol,
    showChart && hasForecastContent,
  );

  const forecastOverlay = embeddedForecast
    ? toForecastOverlay(embeddedForecast)
    : forecastQuery.data && !forecastQuery.isError
      ? toForecastOverlay(forecastQuery.data)
      : undefined;

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">{section.title}</CardTitle>
          {section.specialist ? (
            <Badge variant="secondary">{section.specialist}</Badge>
          ) : null}
          {section.symbols?.map((symbol) => (
            <Badge key={symbol} variant="outline">
              {symbol}
            </Badge>
          ))}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {showChart && chartSymbol ? (
          <Suspense fallback={<div className="h-20 rounded-md border bg-muted/10" />}>
            <KlineMiniChart symbol={chartSymbol} forecast={forecastOverlay} />
          </Suspense>
        ) : null}
        {forecastQuery.isError ? (
          <ForecastUnavailableNotice error={forecastQuery.error} />
        ) : null}
        <div className="prose prose-sm max-w-none dark:prose-invert">
          <Suspense
            fallback={<p className="whitespace-pre-wrap m-0">{section.content}</p>}
          >
            <Markdown>{section.content}</Markdown>
          </Suspense>
        </div>
      </CardContent>
    </Card>
  );
}
