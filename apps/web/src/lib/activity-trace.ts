import {
  activityKindFromEvent,
  activityLabelFromEvent,
} from "@/lib/activity-labels";
import type { ToolTraceEntry } from "@/lib/types";

export type ActivityDisplayMode = "expanded" | "collapsed" | "hidden";

export function getActivityDisplayMode(
  streaming: boolean,
  finalAnswerStarted: boolean,
  hasTrace: boolean,
): ActivityDisplayMode {
  if (streaming && !finalAnswerStarted) return "expanded";
  return hasTrace ? "collapsed" : "hidden";
}

export function nextTraceId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function findRunningIndex(entries: ToolTraceEntry[], matcher: (entry: ToolTraceEntry) => boolean): number {
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    if (entries[i].status === "running" && matcher(entries[i])) {
      return i;
    }
  }
  return -1;
}

export function appendActivity(
  entries: ToolTraceEntry[],
  event: string,
  data: Record<string, unknown>,
): ToolTraceEntry[] {
  const kind = activityKindFromEvent(event, data);
  const tool = String(data.tool ?? data.specialist ?? data.name ?? kind);
  const label = activityLabelFromEvent(event, data);

  if (kind === "status" || kind === "thinking") {
    const statusIdx = findRunningIndex(entries, (entry) => entry.kind === kind);
    const nextEntry: ToolTraceEntry = {
      id: nextTraceId(),
      tool,
      kind,
      label,
      status: "running",
      timestamp: Date.now(),
    };
    if (statusIdx >= 0) {
      const next = [...entries];
      next[statusIdx] = { ...next[statusIdx], label, timestamp: Date.now() };
      return next;
    }
    return [...entries, nextEntry];
  }

  return [
    ...entries,
    {
      id: nextTraceId(),
      tool,
      kind,
      label,
      status: "running",
      timestamp: Date.now(),
    },
  ];
}

export function completeActivity(
  entries: ToolTraceEntry[],
  event: string,
  data: Record<string, unknown>,
): ToolTraceEntry[] {
  const kind = activityKindFromEvent(event, data);
  const tool = String(data.tool ?? data.specialist ?? data.name ?? "");
  const label = activityLabelFromEvent(event, data);
  const status = data.status === "error" ? "error" : "ok";
  const durationMs =
    typeof data.duration_ms === "number"
      ? data.duration_ms
      : typeof data.durationMs === "number"
        ? data.durationMs
        : undefined;

  const idx = findRunningIndex(entries, (entry) => {
    if (kind === "specialist") {
      return entry.kind === "specialist" && entry.tool === tool;
    }
    if (tool) {
      return entry.tool === tool;
    }
    return entry.label === label;
  });

  if (idx < 0) {
    return [
      ...entries,
      {
        id: nextTraceId(),
        tool: tool || label,
        kind,
        label,
        status,
        preview: typeof data.preview === "string" ? data.preview : undefined,
        durationMs,
        timestamp: Date.now(),
      },
    ];
  }

  const next = [...entries];
  next[idx] = {
    ...next[idx],
    status,
    label: next[idx].label ?? label,
    preview: typeof data.preview === "string" ? data.preview : next[idx].preview,
    durationMs,
  };
  return next;
}

export function markStatusDone(entries: ToolTraceEntry[]): ToolTraceEntry[] {
  return entries.map((entry) =>
    entry.kind === "status" && entry.status === "running"
      ? { ...entry, status: "ok" as const }
      : entry,
  );
}
