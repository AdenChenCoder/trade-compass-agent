import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KlineMiniChart } from "@/components/charts/KlineMiniChart";
import { fetchForecast } from "@/lib/workbench-api";
import type { TurnSection } from "@/lib/types";

function useForecast(symbol: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["forecast", symbol],
    queryFn: () => fetchForecast(symbol!, 5),
    enabled: Boolean(symbol && enabled),
    staleTime: 5 * 60_000,
    retry: 1,
  });
}

export function SectionCard({ section }: { section: TurnSection }) {
  const chartSymbol = section.symbols?.[0];
  const showChart = Boolean(chartSymbol) && section.kind === "summary";

  const hasEmbeddedForecast =
    section.forecast_data &&
    section.forecast_data.forecast_bars.length > 0;

  const hasForecastContent =
    !hasEmbeddedForecast &&
    (/预测|forecast|kline_forecast/i.test(section.content) ||
      /预测|forecast/i.test(section.title));

  const forecastQuery = useForecast(
    chartSymbol,
    showChart && hasForecastContent,
  );

  const forecastOverlay = hasEmbeddedForecast
    ? {
        bars: section.forecast_data!.forecast_bars,
        upperBand: section.forecast_data!.confidence_band.upper,
        lowerBand: section.forecast_data!.confidence_band.lower,
      }
    : forecastQuery.data && !forecastQuery.isError
      ? {
          bars: forecastQuery.data.forecast_bars,
          upperBand: forecastQuery.data.confidence_band.upper,
          lowerBand: forecastQuery.data.confidence_band.lower,
        }
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
          <KlineMiniChart symbol={chartSymbol} forecast={forecastOverlay} />
        ) : null}
        <div className="prose prose-sm max-w-none dark:prose-invert">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{section.content}</ReactMarkdown>
        </div>
      </CardContent>
    </Card>
  );
}
