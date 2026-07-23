import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, Database, FileText, TrendingUp, TrendingDown, Clock, Target, Search, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NewBadge } from "@/components/ui/new-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { fetchDecisions, curateDecisions, reflectDecision, fetchInstruments, fetchInstrumentPage, fetchMemory } from "@/lib/workbench-api";
import type { DecisionEntry, DecisionStats, MemoryResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

function CapacityBar({ used, limit }: { used: number; limit: number }) {
  const pct = Math.min(Math.round((used / limit) * 100), 100);
  return (
    <div className="flex items-center gap-3">
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <div
          className={cn(
            "h-full rounded-full transition-all",
            pct > 80 ? "bg-orange-500" : pct > 95 ? "bg-destructive" : "bg-primary",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={cn("shrink-0 text-xs tabular-nums", pct > 80 && "text-orange-500")}>
        {used}/{limit}
      </span>
    </div>
  );
}

function MemoryEntries({ target }: { target: "memory" | "user" }) {
  const { data, isLoading } = useQuery<MemoryResponse>({
    queryKey: ["memory", target],
    queryFn: () => fetchMemory(target),
  });
  const [filter, setFilter] = useState("");

  if (isLoading) return <Skeleton className="h-40 w-full" />;
  if (!data || data.entries.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        暂无记录。Agent 运行中会自动积累。
      </p>
    );
  }

  const filtered = filter
    ? data.entries.filter((e) => e.text.toLowerCase().includes(filter.toLowerCase()))
    : data.entries;

  return (
    <div className="space-y-4">
      <CapacityBar used={data.chars_used} limit={data.char_limit} />
      <div className="relative">
        <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
        <Input
          placeholder="搜索记忆..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="h-8 pl-8 text-xs"
        />
      </div>
      <div className="space-y-2">
        {filtered.map((entry, i) => (
          <div
            key={i}
            className="group rounded-md border bg-muted/30 px-3 py-2.5 text-sm transition-colors hover:bg-muted/60"
          >
            <div className="flex items-start justify-between gap-3">
              <p className="flex-1 leading-relaxed">{entry.text}</p>
              <div className="flex shrink-0 items-center gap-2">
                <NewBadge createdAt={entry.created_at} />
                <div className="flex items-center gap-2 opacity-60 transition-opacity group-hover:opacity-100">
                  <span
                    className={cn(
                      "rounded px-1.5 py-0.5 text-[10px] font-medium tabular-nums",
                      entry.confidence > 0.7
                        ? "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400"
                        : "bg-orange-50 text-orange-700 dark:bg-orange-950 dark:text-orange-400",
                    )}
                    title={`置信度: ${entry.confidence.toFixed(3)}`}
                  >
                    {Math.round(entry.confidence * 100)}%
                  </span>
                  <span className="text-[10px] text-muted-foreground" title="访问次数">
                    ×{entry.access_count}
                  </span>
                </div>
              </div>
            </div>
          </div>
        ))}
        {filtered.length === 0 && filter && (
          <p className="py-4 text-center text-xs text-muted-foreground">无匹配结果</p>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        共 {data.entries.length} 条 · 显示 {filtered.length} 条
      </p>
    </div>
  );
}

function StatsCards({ stats }: { stats: DecisionStats }) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      <div className="rounded-lg border bg-muted/30 p-3">
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">总决策</p>
        <p className="mt-1 text-2xl font-bold tabular-nums">{stats.total}</p>
      </div>
      <div className="rounded-lg border bg-muted/30 p-3">
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">待结算</p>
        <p className="mt-1 text-2xl font-bold tabular-nums text-yellow-600">
          {stats.pending}
          {(stats.partial ?? 0) > 0 && (
            <span className="ml-1 text-xs font-medium">+{stats.partial} 部分</span>
          )}
        </p>
      </div>
      <div className="rounded-lg border bg-muted/30 p-3">
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">待复盘</p>
        <p className="mt-1 text-2xl font-bold tabular-nums text-blue-600">{stats.awaiting_reflection ?? 0}</p>
      </div>
      <div className="rounded-lg border bg-muted/30 p-3">
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">已复盘</p>
        <p className="mt-1 text-2xl font-bold tabular-nums text-green-600">{stats.reflected ?? 0}</p>
      </div>
      <div className="rounded-lg border bg-muted/30 p-3">
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">胜率</p>
        <p className="mt-1 text-2xl font-bold tabular-nums">
          {stats.win_rate != null ? `${stats.win_rate}%` : "—"}
        </p>
      </div>
      <div className="rounded-lg border bg-muted/30 p-3">
        <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">均盈亏</p>
        <p className={cn(
          "mt-1 text-2xl font-bold tabular-nums",
          (stats.avg_pnl ?? 0) >= 0 ? "text-green-600" : "text-red-600",
        )}>
          {stats.avg_pnl != null ? `${stats.avg_pnl > 0 ? "+" : ""}${stats.avg_pnl}%` : "—"}
        </p>
      </div>
    </div>
  );
}

function DecisionList() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["decisions"],
    queryFn: () => fetchDecisions({ limit: 50 }),
  });
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [reflectingId, setReflectingId] = useState<string | null>(null);

  const curateMutation = useMutation({
    mutationFn: () => curateDecisions(20),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["decisions"] }),
  });

  const reflectMutation = useMutation({
    mutationFn: ({ id, reflection }: { id: string; reflection?: string }) => reflectDecision(id, reflection),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["decisions"] }),
    onSettled: () => setReflectingId(null),
  });

  if (isLoading) return <Skeleton className="h-40 w-full" />;
  if (!data) return <p className="text-sm text-muted-foreground">无数据</p>;

  const statusLabels: Record<string, string> = {
    all: "全部",
    pending: "待结算",
    partial: "部分结算",
    resolved: "待复盘",
    reflected: "已复盘",
  };

  const statusColor: Record<string, string> = {
    pending: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400",
    partial: "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400",
    resolved: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
    reflected: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  };

  const filtered = statusFilter === "all"
    ? data.decisions
    : data.decisions.filter((d) => d.status === statusFilter);

  const awaitingCount = data.stats.awaiting_reflection ?? 0;

  return (
    <div className="space-y-4">
      <StatsCards stats={data.stats} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(statusLabels).map(([key, label]) => (
            <Badge
              key={key}
              variant={statusFilter === key ? "default" : "outline"}
              className="cursor-pointer text-xs"
              onClick={() => setStatusFilter(key)}
            >
              {label}
            </Badge>
          ))}
        </div>
        {awaitingCount > 0 && (
          <Button
            size="sm"
            variant="outline"
            className="h-7 gap-1.5 text-xs"
            disabled={curateMutation.isPending}
            onClick={() => curateMutation.mutate()}
          >
            <Sparkles className="h-3.5 w-3.5" />
            {curateMutation.isPending ? "复盘中..." : `一键复盘 (${awaitingCount})`}
          </Button>
        )}
      </div>

      <div className="space-y-2">
        {filtered.map((d: DecisionEntry) => (
          <div key={d.id} className="rounded-lg border p-3 transition-colors hover:bg-muted/30">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                {d.side === "buy" ? (
                  <div className="flex h-6 w-6 items-center justify-center rounded-full bg-red-50 dark:bg-red-950">
                    <TrendingUp className="h-3.5 w-3.5 text-red-500" />
                  </div>
                ) : (
                  <div className="flex h-6 w-6 items-center justify-center rounded-full bg-green-50 dark:bg-green-950">
                    <TrendingDown className="h-3.5 w-3.5 text-green-500" />
                  </div>
                )}
                <span className="font-mono text-sm font-semibold">{d.symbol}</span>
                <NewBadge createdAt={d.decided_at} />
                <span className="text-xs text-muted-foreground">
                  {d.quantity}股 @¥{d.price}
                </span>
              </div>
              <div className="flex items-center gap-2">
                {d.outcome_pnl_pct != null && (
                  <span className={cn(
                    "text-sm font-semibold tabular-nums",
                    d.outcome_pnl_pct >= 0 ? "text-green-600" : "text-red-600",
                  )}>
                    {d.outcome_pnl_pct > 0 ? "+" : ""}{d.outcome_pnl_pct}%
                  </span>
                )}
                <Badge variant="secondary" className={cn("text-[10px]", statusColor[d.status] || "")}>
                  {statusLabels[d.status] ?? d.status}
                </Badge>
                {d.status === "resolved" && (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-6 px-2 text-[11px]"
                    disabled={reflectingId === d.id || reflectMutation.isPending}
                    onClick={() => {
                      setReflectingId(d.id);
                      reflectMutation.mutate({ id: d.id });
                    }}
                  >
                    {reflectingId === d.id ? "生成中..." : "复盘"}
                  </Button>
                )}
              </div>
            </div>

            {d.reasoning && (
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{d.reasoning}</p>
            )}
            {d.resolved_quantity > 0 && d.outcome_price != null && (
              <p className="mt-1.5 text-xs tabular-nums text-muted-foreground">
                已结算 {d.resolved_quantity}/{d.quantity} 股 · 加权卖出 ¥{d.outcome_price}
                {d.outcome_net_pnl != null && (
                  <span> · 净盈亏 ¥{d.outcome_net_pnl}</span>
                )}
              </p>
            )}
            {d.reconciliation_status === "unmatched" && (
              <p className="mt-1.5 rounded bg-orange-50 px-2 py-1 text-xs text-orange-700 dark:bg-orange-950 dark:text-orange-300">
                待核对：未找到可追溯的成交记录，不将未知结果显示为 0%。
              </p>
            )}
            {d.reflection && (
              <p className="mt-1.5 rounded bg-blue-50 px-2 py-1 text-xs leading-relaxed text-blue-700 dark:bg-blue-950 dark:text-blue-300">
                复盘: {d.reflection}
              </p>
            )}

            <div className="mt-2 flex items-center gap-3 text-[11px] text-muted-foreground">
              <span>{d.decided_at?.slice(0, 10)}</span>
              {d.holding_days != null && <span>持有 {d.holding_days} 天</span>}
              <span className="rounded bg-muted px-1.5 py-0.5">{d.account}</span>
            </div>
          </div>
        ))}
        {filtered.length === 0 && (
          <p className="py-8 text-center text-sm text-muted-foreground">
            {data.decisions.length === 0 ? "暂无交易决策记录" : "无匹配决策"}
          </p>
        )}
      </div>
    </div>
  );
}

function InstrumentsList() {
  const { data, isLoading } = useQuery({
    queryKey: ["instruments"],
    queryFn: fetchInstruments,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const { data: page, isLoading: pageLoading } = useQuery({
    queryKey: ["instrument", selected],
    queryFn: () => fetchInstrumentPage(selected!),
    enabled: !!selected,
  });

  if (isLoading) return <Skeleton className="h-40 w-full" />;

  if (!data?.instruments.length) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        暂无个股档案。Agent 交易已追踪的标的后，会自动创建。
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">{data.instruments.length} 个标的</p>
      <div className="flex flex-wrap gap-2">
        {data.instruments.map((sym) => (
          <div key={sym} className="flex items-center gap-1.5">
            <Badge
              variant={selected === sym ? "default" : "outline"}
              className="cursor-pointer font-mono transition-colors"
              onClick={() => setSelected(sym === selected ? null : sym)}
            >
              {sym}
            </Badge>
            <NewBadge createdAt={data.created_at?.[sym]} />
          </div>
        ))}
      </div>
      {selected && (
        <Card className="border-primary/20">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 font-mono text-sm">
              <FileText className="h-3.5 w-3.5" />
              {selected}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {pageLoading ? (
              <Skeleton className="h-20 w-full" />
            ) : page ? (
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md bg-muted/50 p-3 text-xs leading-relaxed">
                {page.content}
              </pre>
            ) : null}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

export function MemoryPage() {
  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Brain className="h-5 w-5" />
            记忆系统
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Agent 的长期记忆。知识和用户画像由 Agent 自动提炼积累，决策日志追踪每笔交易的推理与复盘。
          </p>
        </div>

        <Tabs defaultValue="knowledge">
          <TabsList>
            <TabsTrigger value="knowledge" className="gap-1.5">
              <Database className="h-3.5 w-3.5" />
              知识库
            </TabsTrigger>
            <TabsTrigger value="user" className="gap-1.5">
              <Target className="h-3.5 w-3.5" />
              用户画像
            </TabsTrigger>
            <TabsTrigger value="decisions" className="gap-1.5">
              <Clock className="h-3.5 w-3.5" />
              决策日志
            </TabsTrigger>
            <TabsTrigger value="instruments" className="gap-1.5">
              <FileText className="h-3.5 w-3.5" />
              个股档案
            </TabsTrigger>
          </TabsList>

          <TabsContent value="knowledge" className="mt-4">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">KNOWLEDGE.md</CardTitle>
                <CardDescription>持久知识 — 经验规律、系统约束、验证过的教训</CardDescription>
              </CardHeader>
              <CardContent>
                <MemoryEntries target="memory" />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="user" className="mt-4">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">USER.md</CardTitle>
                <CardDescription>用户画像 — 交易风格、风险偏好、板块偏好</CardDescription>
              </CardHeader>
              <CardContent>
                <MemoryEntries target="user" />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="decisions" className="mt-4">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Decision Journal</CardTitle>
                <CardDescription>
                  交易决策全生命周期追踪: 待结算 → 待复盘 → 已复盘
                </CardDescription>
              </CardHeader>
              <CardContent>
                <DecisionList />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="instruments" className="mt-4">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Instrument Pages</CardTitle>
                <CardDescription>个股档案 — 关注理由、关键价位、交易历史、笔记</CardDescription>
              </CardHeader>
              <CardContent>
                <InstrumentsList />
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
