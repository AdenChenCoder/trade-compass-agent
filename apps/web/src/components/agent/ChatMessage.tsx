import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Wrench } from "lucide-react";
import { SectionCard } from "@/components/agent/SectionCard";
import { KlineMiniChart } from "@/components/charts/KlineMiniChart";
import { fetchForecast } from "@/lib/workbench-api";
import { filterVisibleSections } from "@/lib/sections";
import { cn } from "@/lib/utils";
import type { ChatMessage as ChatMessageType } from "@/lib/types";

function ToolCallsSummary({ toolCalls }: { toolCalls: { name: string }[] }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
      <Wrench className="h-3 w-3 shrink-0" />
      <span>调用了：</span>
      {toolCalls.map((tc, i) => (
        <span key={i} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">
          {tc.name}
        </span>
      ))}
    </div>
  );
}

const SYMBOL_RE = /[（(](\d{6})[）)]/;

function useForecastChart(content: string, role: string) {
  const detected = useMemo(() => {
    if (role !== "assistant") return null;
    if (!/预测|forecast|kline_forecast/i.test(content)) return null;
    const m = content.match(SYMBOL_RE);
    return m ? m[1] : null;
  }, [content, role]);

  const query = useQuery({
    queryKey: ["forecast-chat", detected],
    queryFn: () => fetchForecast(detected!, 5),
    enabled: Boolean(detected),
    staleTime: 5 * 60_000,
    retry: 1,
  });

  if (!detected || !query.data) return null;
  return {
    symbol: detected,
    overlay: {
      bars: query.data.forecast_bars,
      upperBand: query.data.confidence_band.upper,
      lowerBand: query.data.confidence_band.lower,
    },
  };
}

export function ChatMessage({
  message,
}: {
  message: ChatMessageType;
}) {
  const isUser = message.role === "user";
  const isError = message.role === "error";
  const isStreaming = message.streaming === true;
  const visibleSections =
    message.role === "assistant" ? filterVisibleSections(message.sections) : [];

  const forecastChart = useForecastChart(message.content, message.role);

  return (
    <div
      className={cn(
        "flex w-full",
        isUser ? "justify-end" : "justify-start",
      )}
    >
      <div
        className={cn(
          "max-w-[85%] rounded-xl px-4 py-3 text-sm leading-relaxed",
          isUser && "bg-primary text-primary-foreground",
          !isUser && !isError && "border bg-card",
          isError && "border border-destructive/40 bg-destructive/10 text-destructive",
        )}
      >
        {message.role === "assistant" && (message.content || visibleSections.length > 0 || message.toolCalls?.length || isStreaming) ? (
          <div className="space-y-3">
            {message.content || isStreaming ? (
              <div className="prose prose-sm max-w-none dark:prose-invert">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                {isStreaming ? (
                  <span
                    className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-foreground/70 align-middle"
                    aria-hidden
                  />
                ) : null}
              </div>
            ) : null}
            {forecastChart ? (
              <KlineMiniChart
                symbol={forecastChart.symbol}
                height={260}
                forecast={forecastChart.overlay}
              />
            ) : null}
            {message.toolCalls?.length ? (
              <ToolCallsSummary toolCalls={message.toolCalls} />
            ) : null}
            {visibleSections.map((section, i) =>
              section.kind === "summary" && section.symbols?.length ? (
                <SectionCard key={`${section.title}-${i}`} section={section} />
              ) : section.kind === "summary" ? (
                <p
                  key={`${section.title}-${i}`}
                  className="m-0 text-xs text-muted-foreground"
                >
                  {section.content}
                </p>
              ) : (
                <SectionCard key={`${section.title}-${i}`} section={section} />
              ),
            )}
          </div>
        ) : (
          <div className={cn(!isUser && "prose prose-sm max-w-none dark:prose-invert")}>
            {isUser ? (
              <p className="whitespace-pre-wrap m-0">{message.content}</p>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
