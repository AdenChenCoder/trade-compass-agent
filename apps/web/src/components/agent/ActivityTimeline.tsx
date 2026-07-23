import type { ReactNode } from "react";
import {
  Brain,
  Check,
  Circle,
  Loader2,
  Plug,
  UserRound,
  Wrench,
  FileText,
  X,
} from "lucide-react";
import { formatDuration } from "@/lib/activity-labels";
import { cn } from "@/lib/utils";
import type { ActivityKind, ToolTraceEntry } from "@/lib/types";

const KIND_META: Record<
  ActivityKind,
  { icon: typeof Wrench; className: string }
> = {
  tool: { icon: Wrench, className: "text-blue-600 dark:text-blue-400" },
  skill: { icon: FileText, className: "text-amber-600 dark:text-amber-400" },
  mcp: { icon: Plug, className: "text-violet-600 dark:text-violet-400" },
  specialist: { icon: UserRound, className: "text-emerald-600 dark:text-emerald-400" },
  thinking: { icon: Brain, className: "text-muted-foreground" },
  status: { icon: Circle, className: "text-muted-foreground" },
};

function StatusIcon({ status }: { status: ToolTraceEntry["status"] }) {
  if (status === "running") {
    return <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-primary" />;
  }
  if (status === "error") {
    return <X className="h-3.5 w-3.5 shrink-0 text-destructive" />;
  }
  return <Check className="h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />;
}

function ActivityBubble({ children }: { children: ReactNode }) {
  return (
    <div className="flex w-full justify-start">
      <div className="max-w-[85%] rounded-xl border bg-card px-4 py-3 text-sm leading-relaxed">
        {children}
      </div>
    </div>
  );
}

export function ActivityTimeline({ entries }: { entries: ToolTraceEntry[] }) {
  const visible = entries.filter((entry) => entry.kind !== "status" || entry.status === "running");
  if (visible.length === 0) {
    return (
      <ActivityBubble>
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
          <span>处理中…</span>
        </div>
      </ActivityBubble>
    );
  }

  return (
    <ActivityBubble>
      <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
        <span>执行中</span>
      </div>
      <ul className="mt-1.5 space-y-1">
        {visible.map((entry) => {
          const kind = entry.kind ?? "tool";
          const meta = KIND_META[kind];
          const Icon = meta.icon;
          const duration = formatDuration(entry.durationMs);
          const showAsStatus = kind === "status" || kind === "thinking";

          return (
            <li
              key={entry.id}
              className="flex items-center gap-1.5 text-xs"
            >
              <Icon className={cn("h-3 w-3 shrink-0", meta.className)} />
              <span
                className={cn(
                  "min-w-0 truncate",
                  showAsStatus ? "text-muted-foreground" : "font-mono text-foreground",
                )}
              >
                {entry.label ?? entry.tool}
              </span>
              <span className="ml-auto flex shrink-0 items-center gap-1 text-muted-foreground">
                {duration ? <span className="tabular-nums">{duration}</span> : null}
                <StatusIcon status={entry.status} />
              </span>
            </li>
          );
        })}
      </ul>
    </ActivityBubble>
  );
}
