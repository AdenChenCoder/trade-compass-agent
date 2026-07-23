import type { ChatMessage, TurnSection } from "@/lib/types";

/**
 * Turn outcome state machine
 * --------------------------
 * A turn is **successful** when either path delivers assistant content:
 *   - SSE `done` with `ok !== false` and non-empty summary/sections
 *   - POST /turn returns 2xx with non-empty summary/sections
 *
 * Show the global red failure banner only when:
 *   - POST is non-2xx AND no assistant content was delivered for this turn, OR
 *   - SSE `done` has `ok: false` with empty summary/sections, OR
 *   - SSE `error` fires AND no assistant content was delivered
 *
 * Tool-level `tool_end` with `status: "error"` never triggers the global banner;
 * those stay in ActivityTimeline / ToolTrace only.
 *
 * On success, strip any error bubbles after the triggering user message and
 * optionally surface a yellow warning when tools failed but the turn completed.
 */

export interface TurnOutcomeRefs {
  succeeded: boolean;
  assistantDelivered: boolean;
}

export function createTurnOutcomeRefs(): TurnOutcomeRefs {
  return { succeeded: false, assistantDelivered: false };
}

export function resetTurnOutcomeRefs(refs: TurnOutcomeRefs): void {
  refs.succeeded = false;
  refs.assistantDelivered = false;
}

export interface DonePayload {
  ok?: boolean;
  summary?: unknown;
  sections?: unknown;
}

export function hasAssistantPayload(
  summary: string | undefined,
  sections: TurnSection[] | undefined,
): boolean {
  const text = (summary ?? "").trim();
  return text.length > 0 || (sections?.length ?? 0) > 0;
}

export function parseDonePayload(data: Record<string, unknown>): {
  ok: boolean;
  summary: string;
  sections: TurnSection[] | undefined;
} {
  const ok = data.ok !== false;
  const summary = typeof data.summary === "string" ? data.summary : "";
  const sections = Array.isArray(data.sections)
    ? (data.sections as TurnSection[])
    : undefined;
  return { ok, summary, sections };
}

export function shouldShowGlobalFailure(
  refs: TurnOutcomeRefs,
  reason: "http" | "sse_error" | "done",
  payload?: DonePayload,
): boolean {
  if (refs.succeeded || refs.assistantDelivered) {
    return false;
  }
  if (reason === "done") {
    const summary = typeof payload?.summary === "string" ? payload.summary : "";
    const sections = Array.isArray(payload?.sections) ? payload.sections : [];
    return payload?.ok === false && summary.trim().length === 0 && sections.length === 0;
  }
  return true;
}

export function markTurnSucceeded(refs: TurnOutcomeRefs): void {
  refs.succeeded = true;
}

export function markAssistantDelivered(refs: TurnOutcomeRefs): void {
  refs.assistantDelivered = true;
  refs.succeeded = true;
}

export function stripErrorsAfterLastUser(messages: ChatMessage[]): ChatMessage[] {
  let lastUserIdx = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "user") {
      lastUserIdx = i;
      break;
    }
  }
  if (lastUserIdx < 0) {
    return messages;
  }
  return messages.filter((message, index) => index <= lastUserIdx || message.role !== "error");
}
