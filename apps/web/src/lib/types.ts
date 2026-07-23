export interface TurnSection {
  title: string;
  content: string;
  specialist?: string;
  symbols?: string[];
  kind?: "summary" | "narrative" | "tool" | "json" | "raw";
  forecast_data?: {
    forecast_bars: Array<{
      timestamp: string;
      open: number;
      high: number;
      low: number;
      close: number;
      volume: number;
    }>;
    confidence_band: { upper: number[]; lower: number[] };
  };
}

export interface TurnResponse {
  session_id: string;
  turn_id: string;
  summary: string;
  sections: TurnSection[];
  interrupted?: boolean;
}

export interface AgentSkill {
  name: string;
  description?: string;
  path?: string;
  source?: string;
  enabled?: boolean;
}

export interface AgentSkillsResponse {
  skills: AgentSkill[];
}

export interface AgentMcpServer {
  name: string;
  status: string;
  command?: string;
  tools?: string[];
  error?: string;
}

export interface AgentMcpResponse {
  servers: AgentMcpServer[];
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "error";
  content: string;
  sections?: TurnSection[];
  toolCalls?: { name: string }[];
  timestamp: number;
  streaming?: boolean;
}

export type ActivityKind = "tool" | "skill" | "mcp" | "specialist" | "thinking" | "status";

export interface ToolTraceEntry {
  id: string;
  tool: string;
  kind?: ActivityKind;
  label?: string;
  status: "running" | "ok" | "error";
  preview?: string;
  durationMs?: number;
  timestamp: number;
}

// --- Portfolio / Audit (workbench) ------------------------------------------

export type AccountKind = "short_stock" | "etf_rotation" | "mid_term" | "long_term" | "mixed";
export type TradeSide = "buy" | "sell";

export interface Account {
  id: string;
  kind: AccountKind;
  name: string;
  description: string;
  capital: number;
  used: number;
  utilization_pct: number;
  created_at: string;
}

export interface AccountSummary {
  account: AccountKind;
  position_count: number;
  market_value: number;
  cost_basis: number;
  unrealized_pnl: number;
  realized_pnl: number;
  fees: number;
  wins: number;
  losses: number;
  win_rate: number;
  payoff_ratio: number;
  max_drawdown: number;
}

export interface PortfolioPosition {
  symbol: string;
  name: string;
  account: AccountKind;
  quantity: number;
  avg_cost: number;
  last_price: number;
  market_value: number;
  unrealized_pnl: number;
  pnl_pct?: number;
  opened_at?: string | null;
}

export interface PaperTrade {
  trade_id: string;
  decision_id?: string | null;
  symbol: string;
  account: AccountKind;
  side: string;
  quantity: number;
  price: number;
  timestamp: string;
  reason: string;
  price_source: string;
  price_as_of?: string | null;
  requested_price?: number | null;
  previous_close?: number | null;
  suspended?: boolean;
  is_st?: boolean;
}

export interface RealizedTrade {
  account: AccountKind;
  symbol: string;
  quantity: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  fees: number;
  opened_at: string;
  closed_at: string;
}

export interface TradingCosts {
  commission_rate: number;
  min_commission: number;
  stamp_duty_rate: number;
  transfer_fee_rate: number;
  slippage_bps: number;
  min_lot_size: number;
  price_limit_pct: number;
  st_price_limit_pct: number;
}

export interface PortfolioResponse {
  accounts: AccountSummary[];
  positions_by_account: Record<AccountKind, PortfolioPosition[]>;
  trades: PaperTrade[];
  realized_trades: RealizedTrade[];
  costs: TradingCosts;
}

export interface PaperTradeCreate {
  symbol: string;
  account: AccountKind;
  side: TradeSide;
  quantity: number;
  price: number;
  reason?: string;
}

export interface AuditEvent {
  id: string;
  timestamp: string;
  event_type: string;
  summary: string;
  payload: Record<string, unknown>;
}

export interface RuleEntry {
  id: string;
  text: string;
  enabled: boolean;
  updated_by: string;
  created_at: string;
  updated_at: string;
  content_hash: string;
}

export interface RulesResponse {
  content: string;
  entries: RuleEntry[];
  chars_used: number;
  limit: number;
  version: string;
}

// --- Skills & Memory ----------------------------------------------------------

export interface Skill {
  name: string;
  description: string;
  category: string;
  state: "active" | "stale" | "archived";
  pinned: boolean;
  use_count: number;
  patch_count: number;
  last_used_at: string | null;
  created_at: string | null;
  created_by: string | null;
}

export interface SkillDetail extends Skill {
  content: string;
}

export interface SkillsResponse {
  skills: Skill[];
  total: number;
}

export interface MemoryEntry {
  index: number;
  text: string;
  confidence: number;
  access_count: number;
  source: string;
  created_at: string;
  last_accessed: string | null;
}

export interface MemoryResponse {
  target: string;
  entries: MemoryEntry[];
  chars_used: number;
  char_limit: number;
}

export interface ScheduledJob {
  id: string;
  name: string;
  cadence: string;
  enabled: boolean;
  delivery_channels: string[];
}

export interface SchedulerConfig {
  enabled: boolean;
  timezone: string;
  premarket_time: string;
  morning_plan_time: string;
  close_time: string;
  eod_review_time: string;
  postmarket_time: string;
  weekly_day: string;
  weekly_time: string;
}

export interface SchedulerConfigUpdateResponse {
  config: SchedulerConfig;
  reloaded: boolean;
  message: string;
}

export interface CustomJob {
  id: string;
  name: string;
  prompt: string;
  schedule: string;
  enabled: boolean;
  trading_day_only: boolean;
  delivery_channels: string[];
  created_by: string;
  created_at: string | null;
}

export interface JobRun {
  id: string;
  job_id: string;
  status: "queued" | "running" | "completed" | "degraded" | "failed" | "skipped" | "timed_out";
  started_at: string;
  finished_at?: string | null;
  ok: boolean;
  message: string;
  artifact?: string | null;
  error?: string | null;
}

export interface StepRun {
  id: string;
  step_id: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  output?: string | null;
  error?: string | null;
  data?: Record<string, unknown> | null;
}

export interface JobRunDetail extends JobRun {
  step_runs: StepRun[];
  job_type?: "custom" | "builtin";
  job_name?: string;
  analysis?: string;
}

export const DELIVERY_CHANNEL_OPTIONS = [
  { id: "web_log", label: "Web 通知" },
  { id: "feishu", label: "飞书" },
  { id: "wecom", label: "企业微信" },
  { id: "weixin", label: "微信" },
] as const;

export interface Bar {
  symbol: string;
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount?: number | null;
  adjusted?: boolean;
}

export interface BarsResponse {
  symbol: string;
  timeframe: string;
  limit: number;
  bars: Bar[];
  quality_warnings?: string[];
  provider_name?: string | null;
}

export interface DecisionEntry {
  id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  account: string;
  reasoning: string;
  status: string;
  decided_at: string;
  outcome_price: number | null;
  outcome_pnl_pct: number | null;
  resolved_quantity: number;
  outcome_cost_basis: number | null;
  outcome_proceeds: number | null;
  outcome_fees: number | null;
  outcome_net_pnl: number | null;
  outcome_net_pnl_pct: number | null;
  outcome_trade_ids: string[];
  outcome_source: string | null;
  reconciliation_status: string | null;
  reflection_stale: boolean;
  holding_days: number | null;
  reflection: string | null;
  resolved_at: string | null;
}

export interface DecisionStats {
  total: number;
  pending: number;
  partial?: number;
  awaiting_reflection?: number;
  reflected?: number;
  resolved: number;
  win_rate?: number;
  avg_pnl?: number;
  avg_holding_days?: number;
}
