import { ApiError } from "@/lib/agent-api";
import type {
  Account,
  AuditEvent,
  BarsResponse,
  CustomJob,
  DecisionEntry,
  DecisionStats,
  JobRun,
  JobRunDetail,
  MemoryResponse,
  PaperTradeCreate,
  PortfolioResponse,
  RulesResponse,
  ScheduledJob,
  SchedulerConfig,
  SchedulerConfigUpdateResponse,
  SkillDetail,
  SkillsResponse,
} from "@/lib/types";

async function parseJson<T>(res: Response): Promise<T> {
  const text = await res.text();
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const body = JSON.parse(text) as { detail?: string; error?: string };
      if (typeof body.error === "string") detail = body.error;
      else if (typeof body.detail === "string") detail = body.detail;
    } catch {
      const trimmed = text.trim();
      if (trimmed) detail = trimmed.slice(0, 500);
    }
    throw new ApiError(detail, res.status);
  }
  return JSON.parse(text) as T;
}

export async function fetchPortfolio(): Promise<PortfolioResponse> {
  const res = await fetch("/api/portfolio");
  return parseJson<PortfolioResponse>(res);
}

export async function postTrade(body: PaperTradeCreate): Promise<PortfolioResponse> {
  const res = await fetch("/api/portfolio/trades", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseJson<PortfolioResponse>(res);
}

// --- Account CRUD ---

export async function fetchAccounts(): Promise<Account[]> {
  const res = await fetch("/api/accounts");
  return parseJson<Account[]>(res);
}

export async function createAccount(body: {
  kind: string;
  name: string;
  description?: string;
  capital: number;
}): Promise<Account> {
  const res = await fetch("/api/accounts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseJson<Account>(res);
}

export async function updateAccount(
  accountId: string,
  body: { name?: string; description?: string; capital?: number; kind?: string },
): Promise<Account> {
  const res = await fetch(`/api/accounts/${encodeURIComponent(accountId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseJson<Account>(res);
}

export async function deleteAccount(accountId: string): Promise<void> {
  const res = await fetch(`/api/accounts/${encodeURIComponent(accountId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(body.error || `HTTP ${res.status}`, res.status);
  }
}

export async function fetchAudit(limit = 50): Promise<AuditEvent[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  const res = await fetch(`/api/audit?${params.toString()}`);
  return parseJson<AuditEvent[]>(res);
}

export async function fetchAuditEvent(eventId: string): Promise<AuditEvent> {
  const res = await fetch(`/api/audit/${encodeURIComponent(eventId)}`);
  return parseJson<AuditEvent>(res);
}

export async function fetchBars(
  symbol: string,
  timeframe = "1d",
  limit = 120,
  signal?: AbortSignal,
): Promise<BarsResponse> {
  const params = new URLSearchParams({
    symbol,
    timeframe,
    limit: String(limit),
  });
  const res = await fetch(`/api/bars?${params.toString()}`, { signal });
  return parseJson<BarsResponse>(res);
}

export interface ForecastBar {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface ForecastResponse {
  symbol: string;
  model: string;
  lookback_used: number;
  horizon: number;
  current_close: number;
  forecast_bars: ForecastBar[];
  confidence_band: { upper: number[]; lower: number[] };
  change_pct: number;
  error?: string;
}

export async function fetchForecast(
  symbol: string,
  horizon = 10,
  modelSize = "small",
): Promise<ForecastResponse> {
  const params = new URLSearchParams({
    symbol,
    horizon: String(horizon),
    model_size: modelSize,
  });
  const res = await fetch(`/api/forecast?${params.toString()}`);
  return parseJson<ForecastResponse>(res);
}

export async function fetchRules(): Promise<RulesResponse> {
  const res = await fetch("/api/rules");
  const body = await parseJson<RulesResponse & { rules?: Array<{ content?: string }> }>(res);
  if (Array.isArray(body.entries)) return body;

  const legacyContent = Array.isArray(body.rules)
    ? body.rules.map((rule) => rule.content?.trim()).filter(Boolean).join("\n§\n")
    : "";
  return {
    content: legacyContent,
    entries: [],
    chars_used: legacyContent.length,
    limit: 4000,
    version: "",
  };
}

export async function replaceRules(content: string, version?: string): Promise<RulesResponse> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (version) headers["If-Match"] = version;
  const res = await fetch("/api/rules", {
    method: "PUT",
    headers,
    body: JSON.stringify({ content }),
  });
  return parseJson<RulesResponse>(res);
}

export async function addRuleEntry(text: string): Promise<RulesResponse> {
  const res = await fetch("/api/rules/entries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return parseJson<RulesResponse>(res);
}

export async function updateRuleEntry(id: string, text: string): Promise<RulesResponse> {
  const res = await fetch(`/api/rules/entries/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return parseJson<RulesResponse>(res);
}

export async function deleteRuleEntry(id: string): Promise<RulesResponse> {
  const res = await fetch(`/api/rules/entries/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  return parseJson<RulesResponse>(res);
}

// --- Skills & Memory ----------------------------------------------------------

export async function fetchSkills(includeStale = true): Promise<SkillsResponse> {
  const params = new URLSearchParams({ include_stale: String(includeStale) });
  const res = await fetch(`/api/skills?${params.toString()}`);
  return parseJson<SkillsResponse>(res);
}

export async function fetchSkillDetail(name: string): Promise<SkillDetail> {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}`);
  return parseJson<SkillDetail>(res);
}

export async function pinSkill(name: string, pinned: boolean): Promise<void> {
  await fetch(`/api/skills/${encodeURIComponent(name)}/pin`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pinned }),
  });
}

export async function fetchMemory(target: "memory" | "user" = "memory"): Promise<MemoryResponse> {
  const res = await fetch(`/api/memory/${target}`);
  return parseJson<MemoryResponse>(res);
}

export async function fetchDecisions(params?: { symbol?: string; status?: string; limit?: number }) {
  const qs = new URLSearchParams();
  if (params?.symbol) qs.set("symbol", params.symbol);
  if (params?.status) qs.set("status", params.status);
  if (params?.limit) qs.set("limit", String(params.limit));
  const res = await fetch(`/api/decisions?${qs}`);
  return parseJson<{ decisions: DecisionEntry[]; stats: DecisionStats }>(res);
}

export async function curateDecisions(maxReflect = 20) {
  const res = await fetch("/api/decisions/curate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_reflect: maxReflect }),
  });
  return parseJson<{ ok: boolean; reflected_count: number; reflected_ids: string[]; stats: DecisionStats }>(res);
}

export async function reflectDecision(decisionId: string, reflection?: string) {
  const res = await fetch(`/api/decisions/${encodeURIComponent(decisionId)}/reflect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(reflection ? { reflection } : {}),
  });
  return parseJson<{ ok: boolean; decision: DecisionEntry; stats: DecisionStats }>(res);
}

export async function fetchInstruments(): Promise<{
  instruments: string[];
  created_at: Record<string, string>;
}> {
  const res = await fetch("/api/instruments");
  return parseJson<{ instruments: string[]; created_at: Record<string, string> }>(res);
}

export async function fetchInstrumentPage(symbol: string): Promise<{ symbol: string; content: string }> {
  const res = await fetch(`/api/instruments/${symbol}`);
  return parseJson<{ symbol: string; content: string }>(res);
}

export async function fetchJobs(): Promise<ScheduledJob[]> {
  const res = await fetch("/api/jobs");
  return parseJson<ScheduledJob[]>(res);
}

export async function fetchJobRuns(limit = 20, jobId?: string): Promise<JobRun[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (jobId) params.set("job_id", jobId);
  const res = await fetch(`/api/jobs/runs?${params.toString()}`);
  return parseJson<JobRun[]>(res);
}

export async function runJob(jobId: string): Promise<JobRun> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/run`, {
    method: "POST",
  });
  return parseJson<JobRun>(res);
}

export async function updateBuiltinJob(
  jobId: string,
  patch: { delivery_channels: string[] },
): Promise<{ id: string; delivery_channels: string[] }> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return parseJson(res);
}

export async function fetchJobRunDetail(runId: string): Promise<JobRunDetail> {
  const res = await fetch(`/api/jobs/runs/${encodeURIComponent(runId)}`);
  return parseJson<JobRunDetail>(res);
}

// --- Custom Prompt Jobs ---

export async function fetchCustomJobs(): Promise<CustomJob[]> {
  const res = await fetch("/api/jobs/custom");
  return parseJson<CustomJob[]>(res);
}

export async function createCustomJob(body: {
  name: string;
  prompt: string;
  schedule: string;
  trading_day_only?: boolean;
  delivery_channels?: string[];
}): Promise<{ id: string; name: string; schedule: string; delivery_channels: string[] }> {
  const res = await fetch("/api/jobs/custom", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseJson(res);
}

export async function updateCustomJob(
  jobId: string,
  patch: Partial<CustomJob>,
): Promise<{ id: string; name: string; enabled: boolean }> {
  const res = await fetch(`/api/jobs/custom/${encodeURIComponent(jobId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return parseJson(res);
}

export async function deleteCustomJob(jobId: string): Promise<{ ok: boolean }> {
  const res = await fetch(`/api/jobs/custom/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
  return parseJson(res);
}

export async function runCustomJob(jobId: string): Promise<JobRun> {
  const res = await fetch(`/api/jobs/custom/${encodeURIComponent(jobId)}/run`, {
    method: "POST",
  });
  return parseJson<JobRun>(res);
}

export async function fetchSchedulerConfig(): Promise<SchedulerConfig> {
  const res = await fetch("/api/config/scheduler");
  return parseJson<SchedulerConfig>(res);
}

export async function updateSchedulerConfig(
  patch: Partial<SchedulerConfig>,
): Promise<SchedulerConfigUpdateResponse> {
  const res = await fetch("/api/config/scheduler", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return parseJson<SchedulerConfigUpdateResponse>(res);
}
