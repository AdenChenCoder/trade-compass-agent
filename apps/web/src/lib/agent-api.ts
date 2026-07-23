import type {
  AgentMcpResponse,
  AgentSkillsResponse,
  TurnResponse,
} from "@/lib/types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function isSessionNotFoundError(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  if (err.status === 404 || Number(err.status) === 404) return true;
  const msg = err.message.trim().toLowerCase();
  return msg === "not found" || msg === "session not found";
}

async function parseJson<T>(res: Response): Promise<T> {
  const text = await res.text();
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const body = JSON.parse(text) as { detail?: string | { msg?: string } };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail && typeof body.detail === "object" && "msg" in body.detail) {
        detail = String(body.detail.msg);
      }
    } catch {
      const trimmed = text.trim();
      if (trimmed) detail = trimmed.slice(0, 500);
    }
    throw new ApiError(detail, res.status);
  }
  return JSON.parse(text) as T;
}

export function agentStreamUrl(sessionId: string): string {
  const params = new URLSearchParams({ session_id: sessionId });
  return `/api/agent/stream?${params.toString()}`;
}

export interface TurnAttachment {
  type: "text" | "url" | "image";
  content?: string;
  url?: string;
  mime?: string;
}

export async function postAgentTurn(
  body: {
    message: string;
    session_id?: string;
    attachments?: TurnAttachment[];
  },
  options?: { signal?: AbortSignal },
): Promise<TurnResponse> {
  const res = await fetch("/api/agent/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options?.signal,
  });
  return parseJson<TurnResponse>(res);
}

export async function postAgentControl(body: {
  session_id: string;
  action: "interrupt";
  turn_id?: string;
}): Promise<{ ok: boolean; session_id: string; turn_id?: string | null }> {
  const res = await fetch("/api/agent/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseJson(res);
}

export async function fetchAgentSkills(): Promise<AgentSkillsResponse> {
  const res = await fetch("/api/agent/skills");
  return parseJson<AgentSkillsResponse>(res);
}

export async function fetchAgentMcp(): Promise<AgentMcpResponse> {
  const res = await fetch("/api/agent/mcp");
  return parseJson<AgentMcpResponse>(res);
}

interface SessionToolCall {
  name: string;
  arguments?: string;
}

export interface SessionMessage {
  role: "user" | "assistant";
  content: string;
  timestamp?: string | null;
  sections?: TurnResponse["sections"];
  tool_calls?: SessionToolCall[] | null;
}

export interface AgentSessionDetail {
  session_id: string;
  title?: string | null;
  updated_at: string;
  messages: SessionMessage[];
  has_active_turn?: boolean;
}

export interface AgentSessionMessagePage extends AgentSessionDetail {
  page: {
    start_index: number;
    total_messages: number;
    next_before: number | null;
  };
}

export interface AgentSessionCreated {
  session_id: string;
  updated_at: string;
}

export interface AgentSessionSummary {
  session_id: string;
  title?: string | null;
  created_at: string;
  updated_at: string;
  message_count: number;
  preview?: string | null;
}

export interface AgentSessionsList {
  sessions: AgentSessionSummary[];
}

export async function createAgentSession(): Promise<AgentSessionCreated> {
  const res = await fetch("/api/agent/sessions", { method: "POST" });
  return parseJson<AgentSessionCreated>(res);
}

export async function fetchAgentSession(sessionId: string): Promise<AgentSessionDetail> {
  const trimmed = sessionId.trim();
  if (!trimmed) {
    throw new ApiError("session id required", 400);
  }
  const res = await fetch(`/api/agent/sessions/${encodeURIComponent(trimmed)}`);
  return parseJson<AgentSessionDetail>(res);
}

export async function fetchAgentSessionMessagePage(
  sessionId: string,
  options: { limit?: number; before?: number; signal?: AbortSignal } = {},
): Promise<AgentSessionMessagePage> {
  const trimmed = sessionId.trim();
  if (!trimmed) {
    throw new ApiError("session id required", 400);
  }
  const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.before != null) params.set("before", String(options.before));
  const res = await fetch(
    `/api/agent/sessions/${encodeURIComponent(trimmed)}/messages?${params.toString()}`,
    { signal: options.signal },
  );
  return parseJson<AgentSessionMessagePage>(res);
}

export async function fetchAgentSessions(limit = 20): Promise<AgentSessionsList> {
  const params = new URLSearchParams({ limit: String(limit) });
  const res = await fetch(`/api/agent/sessions?${params.toString()}`);
  return parseJson<AgentSessionsList>(res);
}

export async function deleteAgentSession(sessionId: string): Promise<void> {
  const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    await parseJson<never>(res);
  }
}

export async function patchAgentSessionTitle(
  sessionId: string,
  title: string,
): Promise<AgentSessionDetail> {
  const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  return parseJson<AgentSessionDetail>(res);
}
