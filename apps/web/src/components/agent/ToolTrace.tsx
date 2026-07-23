import {
  ChevronRight,
  FileText,
  Loader2,
  Plug,
  UserRound,
  Wrench,
} from "lucide-react";
import { formatDuration } from "@/lib/activity-labels";
import { Badge } from "@/components/ui/badge";
import type { ActivityKind, ToolTraceEntry } from "@/lib/types";

const KIND_ICONS: Record<ActivityKind, typeof Wrench> = {
  tool: Wrench,
  skill: FileText,
  mcp: Plug,
  specialist: UserRound,
  thinking: Wrench,
  status: Wrench,
};

export function ToolTrace({ entries }: { entries: ToolTraceEntry[] }) {
  const traceEntries = entries.filter((entry) => entry.kind !== "status");
  if (traceEntries.length === 0) return null;

  return (
    <details className="group rounded-xl border bg-card text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 font-medium text-muted-foreground [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3.5 w-3.5 shrink-0 transition-transform group-open:rotate-90" />
        <Wrench className="h-3.5 w-3.5 shrink-0" />
        工具调用
        <span className="text-muted-foreground/80">({traceEntries.length})</span>
      </summary>
      <ul className="space-y-2 border-t px-3 py-2">
        {traceEntries.map((entry) => {
          const kind = entry.kind ?? "tool";
          const Icon = KIND_ICONS[kind];
          const duration = formatDuration(entry.durationMs);

          return (
            <li key={entry.id} className="space-y-1">
              <div className="flex items-start justify-between gap-2">
                <span className="flex min-w-0 items-start gap-1.5 font-mono text-foreground">
                  <Icon className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
                  <span className="truncate">{entry.label ?? entry.tool}</span>
                </span>
                <span className="flex shrink-0 items-center gap-1">
                  {duration ? (
                    <span className="tabular-nums text-muted-foreground">{duration}</span>
                  ) : null}
                  {entry.status === "running" ? (
                    <Loader2 className="h-3 w-3 animate-spin text-primary" />
                  ) : null}
                  <Badge
                    variant={
                      entry.status === "error"
                        ? "destructive"
                        : entry.status === "running"
                          ? "secondary"
                          : "outline"
                    }
                  >
                    {entry.status}
                  </Badge>
                </span>
              </div>
              {entry.preview ? (
                <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-background/60 p-2 font-mono text-[10px] text-muted-foreground">
                  {entry.preview}
                </pre>
              ) : null}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
