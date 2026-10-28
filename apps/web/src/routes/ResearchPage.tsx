import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import {
  fetchResearchRuns,
  fetchResearchStrategies,
  runResearchBacktest,
  saveResearchStrategy,
} from "@/lib/workbench-api";
import type { ResearchRun, StrategyDefinition, StrategyVersion } from "@/lib/research-types";
import { cn } from "@/lib/utils";

const marketNames = { CN: "A 股", US: "美股", HK: "港股" };
const money = (value: number) =>
  value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : "请求失败，请重试。";

function newHypothesis(): StrategyDefinition {
  return {
    strategy_id: `trend-${crypto.randomUUID().slice(0, 8)}`,
    name: "趋势突破假设",
    market: "CN",
    hypothesis: "收盘放量突破近期高点后，趋势能否延续？",
    invalidation: "跌破慢均线或快均线低于慢均线。",
    method: "trend_breakout",
    parameters: {
      fast_window: 10,
      slow_window: 20,
      breakout_window: 20,
      volume_window: 10,
      volume_ratio: 1.2,
    },
    sources: [],
    knowledge_refs: ["strategy-research"],
  };
}

function NavCurve({ run }: { run: ResearchRun }) {
  const values = [run.result.initial_cash, ...run.result.daily_nav];
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const padding = (maximum - minimum) * 0.1 || Math.max(Math.abs(maximum) * 0.001, 1);
  const low = minimum - padding;
  const high = maximum + padding;
  const span = high - low;
  const points = values
    .map(
      (value, index) =>
        `${54 + (index / Math.max(values.length - 1, 1)) * 680},${142 - ((value - low) / span) * 116}`,
    )
    .join(" ");
  return (
    <figure className="border-y py-4">
      <figcaption className="mb-2 text-sm font-medium">账户净值（元，含未平仓估值）</figcaption>
      <svg
        viewBox="0 0 750 180"
        role="img"
        aria-label="回测净值曲线"
        className="w-full text-primary"
      >
        <title>
          {run.result.start_date} 至 {run.result.end_date} 的回测净值
        </title>
        <path d="M54 26H734 M54 142H734" stroke="currentColor" opacity="0.12" />
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          vectorEffect="non-scaling-stroke"
        />
        <g fill="currentColor" fontSize="11" opacity="0.7">
          <text x="0" y="29">
            {Math.round(high).toLocaleString()}
          </text>
          <text x="0" y="145">
            {Math.round(low).toLocaleString()}
          </text>
          <text x="54" y="172">
            {run.result.start_date}
          </text>
          <text x="734" y="172" textAnchor="end">
            {run.result.end_date}
          </text>
        </g>
      </svg>
    </figure>
  );
}

function ResearchResult({ run }: { run: ResearchRun }) {
  const result = run.result;
  const config = result.config;
  return (
    <section aria-label="回测结果" className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-base font-semibold">
          {run.strategy_snapshot.definition.name} · {run.symbol}
        </h2>
        <Badge variant="secondary">
          {run.evidence_stage === "sample_demo" ? "示例演示" : "探索性回测"}
        </Badge>
      </div>
      <p className="text-sm text-muted-foreground">
        {result.start_date} 至 {result.end_date} · 来源 {run.metadata.source} · 行情截至{" "}
        {run.metadata.data_as_of?.slice(0, 10) ?? "未知"} ·{" "}
        {run.metadata.adjustment === "adjusted" ? "复权价格" : "未复权价格"}
      </p>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
        {[
          ["区间收益", `${result.total_return_pct.toFixed(2)}%`],
          ["最大回撤", `${result.max_drawdown_pct.toFixed(2)}%`],
          ["期末净值", money(result.final_value)],
          ["期末现金", money(result.cash)],
          ["已平仓交易", String(result.total_trades)],
          ["平仓胜率", result.total_trades ? `${(result.win_rate * 100).toFixed(1)}%` : "暂无平仓"],
          ["期末持仓", `${result.open_positions.length} 只`],
          ["初始资金", money(result.initial_cash)],
        ].map(([label, value]) => (
          <div key={label}>
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="mt-1 font-medium tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <NavCurve run={run} />
      <details className="text-sm">
        <summary className="cursor-pointer font-medium">本次规则和费用</summary>
        <div className="mt-3 space-y-2 leading-relaxed text-muted-foreground">
          <p>{run.strategy_snapshot.definition.hypothesis}</p>
          <p>研究失效条件：{run.strategy_snapshot.definition.invalidation}</p>
          <p>
            快/慢均线 {run.strategy_snapshot.definition.parameters.fast_window}/
            {run.strategy_snapshot.definition.parameters.slow_window} 日，箱体{" "}
            {run.strategy_snapshot.definition.parameters.breakout_window} 日，均量{" "}
            {run.strategy_snapshot.definition.parameters.volume_window} 日，量能{" "}
            {run.strategy_snapshot.definition.parameters.volume_ratio}{" "}
            倍。跌破慢均线或快均线低于慢均线形成退出信号。
          </p>
          <p>收盘确认后，下一完整交易日开盘尝试成交；未平仓保留至期末估值。</p>
          <p>
            每次使用可用现金的 {(config.position_pct * 100).toFixed(1)}%；止损{" "}
            {(config.stop_loss_pct * 100).toFixed(1)}%，止盈{" "}
            {(config.take_profit_pct * 100).toFixed(1)}%。
          </p>
          <p>
            佣金 {(config.commission_rate * 100).toFixed(3)}%，最低 {money(config.min_commission)}{" "}
            元；卖出印花税 {(config.stamp_duty_rate * 100).toFixed(3)}%；过户费{" "}
            {(config.transfer_fee_rate * 100).toFixed(4)}%；单边滑点{" "}
            {(config.slippage_pct * 100).toFixed(3)}%。
          </p>
        </div>
      </details>
      {result.open_positions.length ? (
        <div className="text-sm">
          <h3 className="font-medium">期末未平仓</h3>
          {result.open_positions.map((position) => (
            <p key={position.symbol} className="mt-2 text-muted-foreground">
              {position.symbol} · {position.quantity} 股 · 买入 {position.entry_date} @{" "}
              {position.entry_price.toFixed(3)} · 估值 {position.mark_price.toFixed(3)}（
              {position.mark_date}
              {position.price_is_stale ? "，沿用旧价格" : ""}）
            </p>
          ))}
        </div>
      ) : null}
      {result.trades.length ? (
        <div className="overflow-x-auto">
          <table className="w-full whitespace-nowrap text-left text-sm">
            <caption className="mb-2 text-left font-medium">已平仓交易</caption>
            <thead>
              <tr className="border-b text-muted-foreground">
                {["买入日", "卖出日", "股数", "买价", "卖价", "总费用", "净盈亏"].map((label) => (
                  <th key={label} className="px-2 py-2 font-normal">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.trades.map((trade, index) => (
                <tr key={`${trade.entry_date}-${index}`} className="border-b tabular-nums">
                  {[
                    trade.entry_date,
                    trade.exit_date,
                    trade.quantity,
                    trade.entry_price.toFixed(3),
                    trade.exit_price.toFixed(3),
                    money(trade.commission_total),
                    money(trade.pnl),
                  ].map((value, column) => (
                    <td key={column} className="px-2 py-2">
                      {value}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">本区间没有已平仓交易。</p>
      )}
      <ul className="space-y-1 rounded-md bg-muted/60 p-3 text-sm leading-relaxed text-muted-foreground">
        {run.warnings.map((warning) => (
          <li key={warning}>{warning}</li>
        ))}
      </ul>
    </section>
  );
}

export function ResearchPage() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(newHypothesis);
  const [selected, setSelected] = useState<StrategyVersion | null>(null);
  const [symbol, setSymbol] = useState("");
  const [limit, setLimit] = useState(250);
  const [currentRun, setCurrentRun] = useState<ResearchRun | null>(null);
  const strategies = useQuery({
    queryKey: ["research-strategies"],
    queryFn: fetchResearchStrategies,
  });
  const runs = useQuery({ queryKey: ["research-runs"], queryFn: fetchResearchRuns });
  const save = useMutation({
    mutationFn: saveResearchStrategy,
    onSuccess: async (version) => {
      setSelected(version);
      setDraft(version.definition);
      await queryClient.invalidateQueries({ queryKey: ["research-strategies"] });
    },
  });
  const backtest = useMutation({
    mutationFn: runResearchBacktest,
    onSuccess: async (run) => {
      setCurrentRun(run);
      await queryClient.invalidateQueries({ queryKey: ["research-runs"] });
    },
  });
  const dirty = !selected || JSON.stringify(selected.definition) !== JSON.stringify(draft);
  const change = <K extends keyof StrategyDefinition>(key: K, value: StrategyDefinition[K]) =>
    setDraft((previous) => ({ ...previous, [key]: value }));
  const selectVersion = (version: StrategyVersion) => {
    setSelected(version);
    setDraft(version.definition);
    save.reset();
    backtest.reset();
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate(draft);
  };

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-6xl space-y-6">
        <div className="flex items-center justify-between gap-4">
          <h1 className="text-xl font-semibold tracking-tight">策略研究</h1>
          <Button
            variant="outline"
            disabled={save.isPending}
            onClick={() => {
              setSelected(null);
              setDraft(newHypothesis());
              save.reset();
              backtest.reset();
            }}
          >
            新建假设
          </Button>
        </div>
        <div className="grid gap-8 lg:grid-cols-[220px_minmax(0,1fr)]">
          <aside aria-label="研究记录" className="space-y-6">
            <section>
              <h2 className="mb-3 text-sm font-medium">已保存的假设</h2>
              {strategies.isPending ? (
                <Skeleton className="h-20" />
              ) : strategies.error ? (
                <p role="alert" className="text-sm text-destructive">
                  {errorText(strategies.error)}
                  <Button variant="link" onClick={() => strategies.refetch()}>
                    重试
                  </Button>
                </p>
              ) : strategies.data?.length ? (
                <div className="space-y-1">
                  {strategies.data.map((version) => (
                    <button
                        disabled={save.isPending}
                      key={`${version.definition.strategy_id}-${version.version}`}
                      onClick={() => selectVersion(version)}
                      className={cn(
                        "w-full rounded-md p-3 text-left text-sm hover:bg-muted focus-visible:outline focus-visible:outline-2",
                        selected?.version === version.version && "bg-muted",
                      )}
                    >
                      <span className="block font-medium">{version.definition.name}</span>
                      <span className="mt-1 block text-xs text-muted-foreground">
                        {marketNames[version.definition.market]} ·{" "}
                        {new Date(version.created_at).toLocaleString("zh-CN")}
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-sm leading-relaxed text-muted-foreground">
                  先保存一个想验证的交易想法，再选择标的回测。
                </p>
              )}
            </section>
            <section>
              <h2 className="mb-3 text-sm font-medium">回测记录</h2>
              {runs.isPending ? (
                <Skeleton className="h-20" />
              ) : runs.error ? (
                <p role="alert" className="text-sm text-destructive">
                  {errorText(runs.error)}
                  <Button variant="link" onClick={() => runs.refetch()}>
                    重试
                  </Button>
                </p>
              ) : runs.data?.length ? (
                <div className="space-y-1">
                  {runs.data.map((run) => (
                    <button
                      key={run.run_id}
                      onClick={() => setCurrentRun(run)}
                      className={cn(
                        "w-full rounded-md p-3 text-left text-sm hover:bg-muted focus-visible:outline focus-visible:outline-2",
                        currentRun?.run_id === run.run_id && "bg-muted",
                      )}
                    >
                      <span className="block font-medium">
                        {run.symbol} · {run.result.total_return_pct.toFixed(2)}%
                      </span>
                      <span className="mt-1 block text-xs text-muted-foreground">
                        {run.strategy_snapshot.definition.name} ·{" "}
                        {run.evidence_stage === "sample_demo" ? "示例" : "探索性回测"}
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">还没有回测记录。</p>
              )}
            </section>
          </aside>
          <div className="min-w-0 space-y-8">
            <form onSubmit={submit} aria-label="策略假设">
              <fieldset disabled={save.isPending} className="space-y-4">
                <div className="grid gap-4 sm:grid-cols-[1fr_140px]">
                  <label className="space-y-2 text-sm">
                    假设名称
                    <Input
                      value={draft.name}
                      onChange={(event) => change("name", event.target.value)}
                      required
                      maxLength={120}
                    />
                  </label>
                  <label className="space-y-2 text-sm">
                    适用市场
                    <select
                      value={draft.market}
                      onChange={(event) =>
                        change("market", event.target.value as StrategyDefinition["market"])
                      }
                      className="h-10 w-full rounded-md border bg-background px-3 focus-visible:outline focus-visible:outline-2"
                    >
                      {Object.entries(marketNames).map(([key, label]) => (
                        <option key={key} value={key}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <label className="block space-y-2 text-sm">
                  待验证的想法
                  <textarea
                    value={draft.hypothesis}
                    onChange={(event) => change("hypothesis", event.target.value)}
                    required
                    maxLength={4000}
                    rows={2}
                    className="w-full rounded-md border bg-background px-3 py-2 focus-visible:outline focus-visible:outline-2"
                  />
                </label>
                <p className="text-sm leading-relaxed text-muted-foreground">
                  趋势突破：收盘价突破此前箱体高点，快均线高于慢均线，且成交量达到此前均量的指定倍数。跌破慢均线或快均线转弱时退出。
                </p>
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
                  {(
                    [
                      ["fast_window", "快均线（日）"],
                      ["slow_window", "慢均线（日）"],
                      ["breakout_window", "箱体窗口（日）"],
                      ["volume_window", "均量窗口（日）"],
                      ["volume_ratio", "成交量倍数"],
                    ] as const
                  ).map(([key, label]) => (
                    <label key={key} className="space-y-2 text-sm">
                      {label}
                      <Input
                        type="number"
                        min={key === "volume_ratio" ? 0.1 : key === "slow_window" ? 3 : 2}
                        max={key === "volume_ratio" ? 10 : key === "slow_window" ? 500 : 250}
                        step={key === "volume_ratio" ? 0.1 : 1}
                        value={draft.parameters[key]}
                        required
                        onChange={(event) =>
                          change("parameters", {
                            ...draft.parameters,
                            [key]: Number(event.target.value),
                          })
                        }
                      />
                    </label>
                  ))}
                </div>
                <label className="block space-y-2 text-sm">
                  其他失效条件与风险
                  <textarea
                    value={draft.invalidation}
                    onChange={(event) => change("invalidation", event.target.value)}
                    required
                    maxLength={4000}
                    rows={2}
                    className="w-full rounded-md border bg-background px-3 py-2 focus-visible:outline focus-visible:outline-2"
                  />
                </label>
                <p className="text-xs text-muted-foreground">
                  文字风险用于研究复盘，回测只执行上面的均线、突破规则及本次止损止盈。
                </p>
                <label className="block space-y-2 text-sm">
                  来源（每行一条）
                  <textarea
                    value={draft.sources.join("\n")}
                    onChange={(event) => change("sources", event.target.value.split("\n"))}
                    rows={2}
                    className="w-full rounded-md border bg-background px-3 py-2 focus-visible:outline focus-visible:outline-2"
                  />
                </label>
                {save.error ? (
                  <p role="alert" className="text-sm text-destructive">
                    {errorText(save.error)}
                  </p>
                ) : null}
                <div className="flex items-center gap-3">
                  <Button type="submit" disabled={save.isPending}>
                    {save.isPending ? "保存中…" : selected ? "保存新版本" : "保存假设"}
                  </Button>
                  {save.isSuccess && !dirty ? (
                    <p role="status" className="text-sm text-muted-foreground">
                      假设已保存
                    </p>
                  ) : null}
                </div>
              </fieldset>
            </form>
            <form
              aria-label="运行回测"
              onSubmit={(event) => {
                event.preventDefault();
                if (selected && !dirty)
                  backtest.mutate({
                    strategy_id: selected.definition.strategy_id,
                    version: selected.version,
                    symbol,
                    limit,
                  });
              }}
              className="space-y-3 border-t pt-6"
            >
              <h2 className="text-base font-semibold">运行回测</h2>
              <div className="flex flex-wrap items-end gap-3">
                <label className="w-40 space-y-2 text-sm">
                  A 股代码
                  <Input
                    inputMode="numeric"
                    pattern="[0-9]{6}"
                    placeholder="6 位证券代码"
                    value={symbol}
                    onChange={(event) => setSymbol(event.target.value)}
                    required
                  />
                </label>
                <label className="w-32 space-y-2 text-sm">
                  请求日线数
                  <Input
                    type="number"
                    min={30}
                    max={1000}
                    value={limit}
                    onChange={(event) => setLimit(Number(event.target.value))}
                    required
                  />
                </label>
                <Button
                  type="submit"
                  disabled={dirty || selected?.definition.market !== "CN" || backtest.isPending}
                >
                  {backtest.isPending ? "回测中…" : "开始回测"}
                </Button>
              </div>
              <p className="text-sm text-muted-foreground">
                {dirty
                  ? "先保存当前假设，再进行回测。"
                  : selected?.definition.market !== "CN"
                    ? "当前回测仅支持 A 股，美股与港股假设可先保存。"
                    : "使用完整日线及当前费用配置；回测不会下单。"}
              </p>
              {backtest.error ? (
                <p role="alert" className="text-sm text-destructive">
                  {errorText(backtest.error)}
                </p>
              ) : null}
              {backtest.isSuccess ? (
                <p role="status" className="text-sm">
                  回测已保存，结果在下方。
                </p>
              ) : null}
            </form>
            {currentRun ? <ResearchResult key={currentRun.run_id} run={currentRun} /> : null}
          </div>
        </div>
      </div>
    </div>
  );
}
