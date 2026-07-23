import type { ActivityKind } from "@/lib/types";

export function activityLabelFromEvent(
  event: string,
  data: Record<string, unknown>,
): string {
  const specialist =
    typeof data.specialist === "string" && data.specialist.trim()
      ? data.specialist
      : null;

  if (typeof data.label === "string" && data.label.trim()) {
    if (specialist && !data.label.startsWith(`${specialist} ›`)) {
      return `${specialist} › ${data.label}`;
    }
    return data.label;
  }

  const tool = String(data.tool ?? data.name ?? "");
  if (tool) {
    return tool;
  }

  if (event === "specialist_started" || event === "specialist_done") {
    const specialist = String(data.specialist ?? "specialist");
    const task = typeof data.task === "string" ? data.task.trim() : "";
    return task ? `${specialist}: ${truncate(task, 56)}` : specialist;
  }

  if (event === "skill_loaded") {
    return `load_skill(${String(data.name ?? "")})`;
  }

  if (event === "status" || event === "thinking") {
    return String(data.text ?? "处理中…");
  }

  return "处理中…";
}

export function activityKindFromEvent(
  event: string,
  data: Record<string, unknown>,
): ActivityKind {
  const kind = data.kind;
  if (
    kind === "tool" ||
    kind === "skill" ||
    kind === "mcp" ||
    kind === "specialist" ||
    kind === "thinking" ||
    kind === "status"
  ) {
    return kind;
  }

  if (event === "skill_loaded") return "skill";
  if (event === "specialist_started" || event === "specialist_done") return "specialist";
  if (event === "thinking") return "thinking";
  if (event === "status") return "status";

  const tool = String(data.tool ?? "");
  if (tool === "load_skill") return "skill";
  if (tool.startsWith("mcp_")) return "mcp";
  if (tool === "dispatch_specialists") return "specialist";
  return "tool";
}

export function formatDuration(ms?: number): string | null {
  if (ms == null || ms < 0) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}
