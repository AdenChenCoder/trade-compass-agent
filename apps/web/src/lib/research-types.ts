export interface StrategyDefinition {
  strategy_id: string;
  name: string;
  market: "CN" | "US" | "HK";
  hypothesis: string;
  invalidation: string;
  method: "trend_breakout";
  parameters: {
    fast_window: number;
    slow_window: number;
    breakout_window: number;
    volume_window: number;
    volume_ratio: number;
  };
  sources: string[];
  knowledge_refs: string[];
}

export interface StrategyVersion {
  version: string;
  created_at: string;
  definition: StrategyDefinition;
}

export interface ResearchRun {
  run_id: string;
  created_at: string;
  strategy_id: string;
  strategy_version: string;
  strategy_snapshot: StrategyVersion;
  symbol: string;
  evidence_stage: "sample_demo" | "exploratory_backtest";
  metadata: {
    source: string;
    data_as_of: string | null;
    adjustment: string;
    is_sample: boolean;
  };
  warnings: string[];
  result: {
    start_date: string;
    end_date: string;
    initial_cash: number;
    final_value: number;
    cash: number;
    total_return_pct: number;
    max_drawdown_pct: number;
    total_trades: number;
    win_rate: number;
    daily_nav: number[];
    config: {
      position_pct: number;
      stop_loss_pct: number;
      take_profit_pct: number;
      commission_rate: number;
      min_commission: number;
      stamp_duty_rate: number;
      transfer_fee_rate: number;
      slippage_pct: number;
    };
    trades: {
      symbol: string;
      entry_date: string;
      exit_date: string;
      entry_price: number;
      exit_price: number;
      quantity: number;
      pnl: number;
      commission_total: number;
      exit_reason: string;
    }[];
    open_positions: {
      symbol: string;
      entry_date: string;
      quantity: number;
      entry_price: number;
      mark_price: number;
      mark_date: string;
      price_is_stale: boolean;
    }[];
  };
}
