import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Clock, Loader2, Pause, Play, Plus, Save, Settings2, Trash2 } from "lucide-react";
import { toast } from "sonner";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { CustomJob, JobRun, SchedulerConfig, StepRun } from "@/lib/types";
import { DELIVERY_CHANNEL_OPTIONS } from "@/lib/types";
import {
  createCustomJob,
  deleteCustomJob,
  fetchCustomJobs,
  fetchJobRunDetail,
  fetchJobRuns,
  fetchJobs,
  fetchSchedulerConfig,
  runCustomJob,
  runJob,
  updateBuiltinJob,
  updateCustomJob,
  updateSchedulerConfig,
} from "@/lib/workbench-api";

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN");
}

const TIME_FIELDS: Array<{ key: keyof SchedulerConfig; label: string }> = [
  { key: "premarket_time", label: "盘前检查" },
  { key: "morning_plan_time", label: "晨间计划" },
  { key: "close_time", label: "收盘检查" },
  { key: "eod_review_time", label: "盘后复盘" },
  { key: "postmarket_time", label: "盘后归档" },
  { key: "weekly_time", label: "周度回顾" },
];

function RunStatusBadge({ status }: { status: string }) {
  switch (status) {
    case "completed":
      return <Badge variant="outline">成功</Badge>;
    case "degraded":
      return <Badge className="border-amber-300 bg-amber-500/15 text-amber-700">降级</Badge>;
    case "running":
      return <Badge className="animate-pulse bg-blue-500/15 text-blue-600 border-blue-300">运行中</Badge>;
    case "queued":
      return <Badge variant="secondary">排队中</Badge>;
    case "skipped":
      return <Badge variant="secondary">跳过</Badge>;
    case "timed_out":
      return <Badge variant="destructive">超时</Badge>;
    default:
      return <Badge variant="destructive">失败</Badge>;
  }
}

function StepStatusDot({ status }: { status: string }) {
  const color =
    status === "completed"
      ? "bg-green-500"
      : status === "running"
        ? "bg-blue-500 animate-pulse"
        : status === "failed" || status === "timed_out"
          ? "bg-red-500"
          : "bg-gray-400";
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} />;
}

function formatStepData(step: StepRun): string | null {
  const data = step.data;
  if (!data) return null;

  // Agent analysis — the actual valuable content
  if (typeof data.analysis === "string" && data.analysis) {
    return data.analysis;
  }

  // Screening candidates
  if (Array.isArray(data.candidates) && data.candidates.length > 0) {
    const lines = (data.candidates as Array<{ symbol: string; score: number }>)
      .map((c, i) => `${i + 1}. ${c.symbol}  综合分 ${Number(c.score).toFixed(3)}`)
      .join("\n");
    return `全市场 ${data.universe ?? "?"} 只 → L1 通过 ${data.l1_passed ?? "?"} → 评分 ${data.scored ?? "?"} 只 → Top ${(data.candidates as unknown[]).length}\n\n${lines}`;
  }

  // Position details
  if (Array.isArray(data.positions)) {
    if ((data.positions as unknown[]).length === 0) return "空仓";
    return (data.positions as Array<Record<string, unknown>>)
      .map((p) => `${p.symbol}  数量${p.quantity ?? "?"}  盈亏${p.pnl_pct ?? 0}%`)
      .join("\n");
  }

  // Signals
  if (typeof data.signals_emitted === "number") {
    return `产出 ${data.signals_emitted} 条信号`;
  }

  // Alerts
  if (Array.isArray(data.alerts)) {
    return (data.alerts as string[]).length > 0
      ? (data.alerts as string[]).join("\n")
      : "无预警";
  }

  // Dreaming
  if (data.patterns !== undefined) {
    return `Patterns: ${data.patterns}, Promoted: ${data.promoted}, Insights: ${data.insights}`;
  }

  // Fallback: show raw JSON for non-empty data
  const keys = Object.keys(data).filter((k) => data[k] !== null && data[k] !== undefined);
  if (keys.length === 0) return null;
  return JSON.stringify(data, null, 2);
}

function StepRunCard({ step }: { step: StepRun }) {
  const [showData, setShowData] = useState(false);
  const elapsed =
    step.started_at && step.finished_at
      ? `${((new Date(step.finished_at).getTime() - new Date(step.started_at).getTime()) / 1000).toFixed(1)}s`
      : step.started_at
        ? "运行中…"
        : "—";

  const richContent = formatStepData(step);
  const hasContent = !!richContent || !!step.error;

  return (
    <div className="rounded border bg-background text-xs">
      <div
        className={`flex items-center gap-3 px-3 py-2 ${hasContent ? "cursor-pointer hover:bg-muted/40" : ""}`}
        onClick={() => hasContent && setShowData(!showData)}
      >
        <span className="inline-flex items-center gap-1.5 min-w-[100px]">
          <StepStatusDot status={step.status} />
          <span className="font-mono font-medium">{step.step_id}</span>
        </span>
        <span className="text-muted-foreground">{elapsed}</span>
        <span className="flex-1 truncate text-muted-foreground">{step.output || ""}</span>
        {hasContent && (
          showData
            ? <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
            : <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
        )}
      </div>
      {showData && hasContent && (
        <div className="border-t px-3 py-2 space-y-2">
          {step.error && (
            <pre className="whitespace-pre-wrap rounded bg-destructive/5 border border-destructive/20 p-2 text-destructive">
              {step.error}
            </pre>
          )}
          {richContent && (
            <pre className="whitespace-pre-wrap rounded bg-muted/50 p-2 leading-relaxed max-h-[500px] overflow-y-auto">
              {richContent}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function formatJobLabel(jobId: string, customJobs: CustomJob[]): string {
  if (!jobId.startsWith("custom:")) return jobId;
  const customId = jobId.slice("custom:".length);
  const job = customJobs.find((j) => j.id === customId);
  return job ? `${job.name} (自建)` : `custom:${customId.slice(0, 8)}…`;
}

function ChannelBadges({ channels }: { channels: string[] }) {
  if (!channels.length) return <span className="text-xs text-muted-foreground">—</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {channels.map((ch) => {
        const label = DELIVERY_CHANNEL_OPTIONS.find((o) => o.id === ch)?.label ?? ch;
        return (
          <Badge key={ch} variant="outline" className="text-[10px] font-normal">
            {label}
          </Badge>
        );
      })}
    </div>
  );
}

function RunRow({
  run,
  jobLabel,
}: {
  run: JobRun;
  jobLabel: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const detailQuery = useQuery({
    queryKey: ["job-run-detail", run.id],
    queryFn: () => fetchJobRunDetail(run.id),
    enabled: expanded,
    refetchInterval: expanded && (run.status === "running" || run.status === "queued") ? 3_000 : false,
  });
  const detail = detailQuery.data ?? null;
  const currentRun = detail ?? run;

  function handleToggle() {
    setExpanded((value) => !value);
  }

  return (
    <>
      <TableRow
        className="cursor-pointer hover:bg-muted/50"
        onClick={handleToggle}
      >
        <TableCell className="w-8 pr-0">
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
          )}
        </TableCell>
        <TableCell className="font-mono text-xs">{jobLabel}</TableCell>
        <TableCell className="whitespace-nowrap text-xs">
          {formatTime(currentRun.started_at)}
        </TableCell>
        <TableCell>
          <RunStatusBadge status={currentRun.status} />
        </TableCell>
        <TableCell className="max-w-xs truncate text-xs text-muted-foreground">
          {currentRun.error || currentRun.message}
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow>
          <TableCell colSpan={5} className="bg-muted/30 p-0">
            <div className="px-6 py-3 space-y-3">
              {/* Summary */}
              <div className="grid gap-x-8 gap-y-1 text-xs sm:grid-cols-2">
                <div>
                  <span className="text-muted-foreground">状态：</span>
                  {currentRun.status}
                </div>
                <div>
                  <span className="text-muted-foreground">开始：</span>
                  {formatTime(currentRun.started_at)}
                </div>
                {currentRun.finished_at && (
                  <div>
                    <span className="text-muted-foreground">结束：</span>
                    {formatTime(currentRun.finished_at)}
                  </div>
                )}
                {currentRun.artifact && (
                  <div className="sm:col-span-2">
                    <span className="text-muted-foreground">产物：</span>
                    <span className="font-mono break-all">{currentRun.artifact}</span>
                  </div>
                )}
              </div>

              {/* Full message */}
              {currentRun.message && (
                <div className="text-xs">
                  <p className="font-medium text-muted-foreground mb-1">消息</p>
                  <p className="whitespace-pre-wrap rounded border bg-background p-2">
                    {currentRun.message}
                  </p>
                </div>
              )}

              {currentRun.error && (
                <div className="text-xs">
                  <p className="font-medium text-destructive mb-1">错误</p>
                  <p className="whitespace-pre-wrap rounded border border-destructive/30 bg-destructive/5 p-2 text-destructive">
                    {currentRun.error}
                  </p>
                </div>
              )}

              {detail && detail.analysis && (
                <div className="text-xs">
                  <p className="font-medium text-muted-foreground mb-1">Agent 输出</p>
                  <pre className="whitespace-pre-wrap rounded border bg-background p-2 leading-relaxed max-h-[500px] overflow-y-auto">
                    {detail.analysis}
                  </pre>
                </div>
              )}

              {/* Step runs */}
              {detailQuery.isLoading && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  加载步骤详情…
                </div>
              )}
              {detail && detail.step_runs.length > 0 && (
                <div className="text-xs space-y-2">
                  <p className="font-medium text-muted-foreground">步骤详情</p>
                  {detail.step_runs.map((sr: StepRun) => (
                    <StepRunCard key={sr.id} step={sr} />
                  ))}
                </div>
              )}
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

export function JobsPage() {
  const queryClient = useQueryClient();
  const [schedulerDraft, setSchedulerDraft] = useState<SchedulerConfig | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [runFilter, setRunFilter] = useState<string>("all");
  const [newJob, setNewJob] = useState({
    name: "",
    prompt: "",
    schedule: "",
    trading_day_only: false,
    delivery_channels: ["web_log"] as string[],
  });

  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: fetchJobs,
  });

  const runsQuery = useQuery({
    queryKey: ["job-runs", runFilter],
    queryFn: () =>
      fetchJobRuns(
        20,
        runFilter === "all" ? undefined : runFilter,
      ),
    refetchInterval: 15_000,
  });

  const schedulerQuery = useQuery({
    queryKey: ["scheduler-config"],
    queryFn: fetchSchedulerConfig,
  });

  const customJobsQuery = useQuery({
    queryKey: ["custom-jobs"],
    queryFn: fetchCustomJobs,
  });

  useEffect(() => {
    if (schedulerQuery.data) {
      setSchedulerDraft(schedulerQuery.data);
    }
  }, [schedulerQuery.data]);

  const runMutation = useMutation({
    mutationFn: runJob,
    onSuccess: (result) => {
      if (result.status === "running" || result.status === "queued") {
        toast.success("任务已触发，正在后台运行");
      } else if (result.ok) {
        toast.success(result.message || "任务已执行");
      } else {
        toast.error(result.message || "任务执行失败");
      }
      void queryClient.invalidateQueries({ queryKey: ["job-runs"] });
    },
    onError: (err: unknown) => {
      toast.error(err instanceof Error ? err.message : "触发失败");
    },
  });

  const saveSchedulerMutation = useMutation({
    mutationFn: updateSchedulerConfig,
    onSuccess: (result) => {
      setSchedulerDraft(result.config);
      toast.success(result.message);
      void queryClient.invalidateQueries({ queryKey: ["scheduler-config"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (err: unknown) => {
      toast.error(err instanceof Error ? err.message : "保存失败");
    },
  });

  const createMutation = useMutation({
    mutationFn: createCustomJob,
    onSuccess: (result) => {
      toast.success(`已创建: ${result.name}`);
      setShowAddForm(false);
      setNewJob({
        name: "",
        prompt: "",
        schedule: "",
        trading_day_only: false,
        delivery_channels: ["web_log"],
      });
      void queryClient.invalidateQueries({ queryKey: ["custom-jobs"] });
    },
    onError: (err: unknown) => {
      toast.error(err instanceof Error ? err.message : "创建失败");
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      updateCustomJob(id, { enabled }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["custom-jobs"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteCustomJob,
    onSuccess: () => {
      toast.success("已删除");
      void queryClient.invalidateQueries({ queryKey: ["custom-jobs"] });
    },
  });

  const runCustomMutation = useMutation({
    mutationFn: runCustomJob,
    onSuccess: (result) => {
      if (result.status === "running" || result.status === "queued") {
        toast.success("自建任务已触发，正在后台运行");
      } else if (result.ok) {
        toast.success(result.message || "任务已执行");
      } else {
        toast.error(result.error || result.message || "执行失败");
      }
      void queryClient.invalidateQueries({ queryKey: ["job-runs"] });
    },
    onError: (err: unknown) => {
      toast.error(err instanceof Error ? err.message : "触发失败");
    },
  });

  const channelsMutation = useMutation({
    mutationFn: ({ id, delivery_channels }: { id: string; delivery_channels: string[] }) =>
      updateCustomJob(id, { delivery_channels }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["custom-jobs"] });
    },
  });

  const builtinChannelsMutation = useMutation({
    mutationFn: ({ id, delivery_channels }: { id: string; delivery_channels: string[] }) =>
      updateBuiltinJob(id, { delivery_channels }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (err: unknown) => {
      toast.error(err instanceof Error ? err.message : "更新推送渠道失败");
    },
  });

  const jobs = jobsQuery.data ?? [];
  const customJobs = customJobsQuery.data ?? [];
  const runs = runsQuery.data ?? [];
  const schedulerDirty =
    schedulerDraft !== null &&
    schedulerQuery.data !== undefined &&
    JSON.stringify(schedulerDraft) !== JSON.stringify(schedulerQuery.data);

  function handleSaveScheduler() {
    if (!schedulerDraft || !schedulerQuery.data) return;
    const patch: Partial<SchedulerConfig> = {};
    for (const key of Object.keys(schedulerDraft) as Array<keyof SchedulerConfig>) {
      if (schedulerDraft[key] !== schedulerQuery.data[key]) {
        patch[key] = schedulerDraft[key] as never;
      }
    }
    if (Object.keys(patch).length === 0) {
      toast.message("没有变更");
      return;
    }
    saveSchedulerMutation.mutate(patch);
  }

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">定时任务</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            查看调度配置与最近运行记录；可编辑 cron 时间并手动触发任意已启用任务。
          </p>
        </div>

        {(jobsQuery.isLoading || runsQuery.isLoading || schedulerQuery.isLoading) && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载中…
          </div>
        )}

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Settings2 className="h-4 w-4" />
              调度配置
            </CardTitle>
            <CardDescription>
              写入 config YAML；若 serve 进程内 scheduler 在运行，保存后会热重载
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {schedulerDraft ? (
              <>
                <div className="flex items-center justify-between gap-4 rounded-lg border p-3">
                  <div>
                    <p className="text-sm font-medium">启用调度器</p>
                    <p className="text-xs text-muted-foreground">
                      时区 {schedulerDraft.timezone}
                    </p>
                  </div>
                  <input
                    type="checkbox"
                    checked={schedulerDraft.enabled}
                    className="h-4 w-4 accent-primary"
                    onChange={(e) =>
                      setSchedulerDraft({ ...schedulerDraft, enabled: e.target.checked })
                    }
                  />
                </div>

                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {TIME_FIELDS.map(({ key, label }) => (
                    <div key={key} className="space-y-1.5">
                      <label htmlFor={key} className="text-sm font-medium">
                        {label}
                      </label>
                      <Input
                        id={key}
                        type="time"
                        value={String(schedulerDraft[key])}
                        onChange={(e) =>
                          setSchedulerDraft({ ...schedulerDraft, [key]: e.target.value })
                        }
                      />
                    </div>
                  ))}
                  <div className="space-y-1.5">
                    <label htmlFor="weekly_day" className="text-sm font-medium">
                      周度回顾（星期）
                    </label>
                    <Input
                      id="weekly_day"
                      value={schedulerDraft.weekly_day}
                      placeholder="sat"
                      onChange={(e) =>
                        setSchedulerDraft({
                          ...schedulerDraft,
                          weekly_day: e.target.value.toLowerCase(),
                        })
                      }
                    />
                  </div>
                </div>

                <div className="flex justify-end">
                  <Button
                    type="button"
                    disabled={!schedulerDirty || saveSchedulerMutation.isPending}
                    onClick={handleSaveScheduler}
                  >
                    {saveSchedulerMutation.isPending ? (
                      <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    ) : (
                      <Save className="mr-1 h-4 w-4" />
                    )}
                    保存配置
                  </Button>
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">无法加载调度配置。</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Clock className="h-4 w-4" />
              已注册任务
            </CardTitle>
            <CardDescription>6 个内置 Job，由 TickScheduler 按上方时间触发</CardDescription>
          </CardHeader>
          <CardContent>
            {jobs.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无任务。</p>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>名称</TableHead>
                      <TableHead>ID</TableHead>
                      <TableHead>周期</TableHead>
                      <TableHead>推送渠道</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead className="text-right">操作</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {jobs.map((job) => (
                      <TableRow key={job.id}>
                        <TableCell className="font-medium">{job.name}</TableCell>
                        <TableCell className="font-mono text-xs">{job.id}</TableCell>
                        <TableCell>{job.cadence}</TableCell>
                        <TableCell>
                          <div className="space-y-2">
                            <ChannelBadges channels={job.delivery_channels ?? []} />
                            <div className="flex flex-wrap gap-2">
                              {DELIVERY_CHANNEL_OPTIONS.map((opt) => {
                                const channels = job.delivery_channels ?? [];
                                const checked = channels.includes(opt.id);
                                return (
                                  <label key={opt.id} className="flex items-center gap-1 text-[11px] text-muted-foreground">
                                    <input
                                      type="checkbox"
                                      className="h-3 w-3 accent-primary"
                                      checked={checked}
                                      disabled={builtinChannelsMutation.isPending}
                                      onChange={() => {
                                        const next = checked
                                          ? channels.filter((ch) => ch !== opt.id)
                                          : [...channels, opt.id];
                                        if (next.length === 0) {
                                          toast.error("至少保留一个推送渠道");
                                          return;
                                        }
                                        builtinChannelsMutation.mutate({ id: job.id, delivery_channels: next });
                                      }}
                                    />
                                    {opt.label}
                                  </label>
                                );
                              })}
                            </div>
                          </div>
                        </TableCell>
                        <TableCell>
                          {job.enabled ? (
                            <Badge variant="outline">启用</Badge>
                          ) : (
                            <Badge variant="secondary">停用</Badge>
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            type="button"
                            size="sm"
                            variant="secondary"
                            disabled={!job.enabled || runMutation.isPending}
                            onClick={() => runMutation.mutate(job.id)}
                          >
                            {runMutation.isPending ? (
                              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                            ) : (
                              <Play className="mr-1 h-3 w-3" />
                            )}
                            运行
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Plus className="h-4 w-4" />
              自建任务
            </CardTitle>
            <CardDescription>用户创建的 Prompt Job，由 AgentLoop 定时执行</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {customJobsQuery.isError && (
              <p className="text-sm text-destructive">
                加载自建任务失败：{customJobsQuery.error instanceof Error ? customJobsQuery.error.message : "未知错误"}
              </p>
            )}

            {customJobs.length > 0 && (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>名称</TableHead>
                      <TableHead>调度</TableHead>
                      <TableHead>推送渠道</TableHead>
                      <TableHead>来源</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead className="text-right">操作</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {customJobs.map((cj) => (
                      <TableRow key={cj.id}>
                        <TableCell>
                          <div>
                            <span className="inline-flex items-center gap-2 font-medium">
                              {cj.name}
                              <NewBadge createdAt={cj.created_at} />
                            </span>
                            <p className="mt-0.5 max-w-xs truncate text-xs text-muted-foreground">
                              {cj.prompt}
                            </p>
                          </div>
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-sm">{cj.schedule}</TableCell>
                        <TableCell>
                          <div className="space-y-2">
                            <ChannelBadges channels={cj.delivery_channels} />
                            <div className="flex flex-wrap gap-2">
                              {DELIVERY_CHANNEL_OPTIONS.map((opt) => {
                                const checked = cj.delivery_channels.includes(opt.id);
                                return (
                                  <label key={opt.id} className="flex items-center gap-1 text-[11px] text-muted-foreground">
                                    <input
                                      type="checkbox"
                                      className="h-3 w-3 accent-primary"
                                      checked={checked}
                                      disabled={channelsMutation.isPending}
                                      onChange={() => {
                                        const next = checked
                                          ? cj.delivery_channels.filter((ch) => ch !== opt.id)
                                          : [...cj.delivery_channels, opt.id];
                                        if (next.length === 0) {
                                          toast.error("至少保留一个推送渠道");
                                          return;
                                        }
                                        channelsMutation.mutate({ id: cj.id, delivery_channels: next });
                                      }}
                                    />
                                    {opt.label}
                                  </label>
                                );
                              })}
                            </div>
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">{cj.created_by}</Badge>
                        </TableCell>
                        <TableCell>
                          {cj.enabled ? (
                            <Badge variant="outline">启用</Badge>
                          ) : (
                            <Badge variant="secondary">暂停</Badge>
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex items-center justify-end gap-1">
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              title={cj.enabled ? "暂停" : "恢复"}
                              onClick={() =>
                                toggleMutation.mutate({ id: cj.id, enabled: !cj.enabled })
                              }
                            >
                              <Pause className="h-3 w-3" />
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              disabled={!cj.enabled || runCustomMutation.isPending}
                              onClick={() => runCustomMutation.mutate(cj.id)}
                            >
                              <Play className="h-3 w-3" />
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              className="text-destructive hover:text-destructive"
                              onClick={() => deleteMutation.mutate(cj.id)}
                            >
                              <Trash2 className="h-3 w-3" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}

            {customJobs.length === 0 && !showAddForm && !customJobsQuery.isLoading && (
              <p className="text-sm text-muted-foreground">暂无自建任务。</p>
            )}

            {showAddForm ? (
              <div className="space-y-3 rounded-lg border p-4">
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium">任务名称</label>
                    <Input
                      placeholder="盘中技术面检查"
                      value={newJob.name}
                      onChange={(e) => setNewJob({ ...newJob, name: e.target.value })}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium">调度表达式</label>
                    <Input
                      placeholder="trading_day 14:30"
                      value={newJob.schedule}
                      onChange={(e) => setNewJob({ ...newJob, schedule: e.target.value })}
                    />
                  </div>
                </div>
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">执行指令</label>
                  <textarea
                    className="flex min-h-[80px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    placeholder="分析 300750 和 600519 的技术面，如有异常信号请提醒"
                    value={newJob.prompt}
                    onChange={(e) => setNewJob({ ...newJob, prompt: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <p className="text-sm font-medium">推送渠道</p>
                  <div className="flex flex-wrap gap-4">
                    {DELIVERY_CHANNEL_OPTIONS.map((opt) => (
                      <label key={opt.id} className="flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          className="h-4 w-4 accent-primary"
                          checked={newJob.delivery_channels.includes(opt.id)}
                          onChange={(e) => {
                            const next = e.target.checked
                              ? [...newJob.delivery_channels, opt.id]
                              : newJob.delivery_channels.filter((ch) => ch !== opt.id);
                            if (next.length === 0) {
                              toast.error("至少选择一个推送渠道");
                              return;
                            }
                            setNewJob({ ...newJob, delivery_channels: next });
                          }}
                        />
                        {opt.label}
                      </label>
                    ))}
                  </div>
                </div>
                <div className="flex items-center gap-4">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={newJob.trading_day_only}
                      className="h-4 w-4 accent-primary"
                      onChange={(e) =>
                        setNewJob({ ...newJob, trading_day_only: e.target.checked })
                      }
                    />
                    仅交易日执行
                  </label>
                </div>
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setShowAddForm(false)}
                  >
                    取消
                  </Button>
                  <Button
                    type="button"
                    disabled={
                      !newJob.name || !newJob.prompt || !newJob.schedule || createMutation.isPending
                    }
                    onClick={() => createMutation.mutate(newJob)}
                  >
                    {createMutation.isPending ? (
                      <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    ) : (
                      <Plus className="mr-1 h-4 w-4" />
                    )}
                    创建
                  </Button>
                </div>
              </div>
            ) : (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setShowAddForm(true)}
              >
                <Plus className="mr-1 h-4 w-4" />
                新建任务
              </Button>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">最近运行</CardTitle>
            <CardDescription>展开行可查看步骤详情或 Agent 完整输出</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <label className="text-sm text-muted-foreground" htmlFor="run-filter">
                筛选任务
              </label>
              <select
                id="run-filter"
                className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                value={runFilter}
                onChange={(e) => setRunFilter(e.target.value)}
              >
                <option value="all">全部</option>
                {jobs.map((job) => (
                  <option key={job.id} value={job.id}>
                    {job.name}（内置）
                  </option>
                ))}
                {customJobs.map((job) => (
                  <option key={job.id} value={`custom:${job.id}`}>
                    {job.name}（自建）
                  </option>
                ))}
              </select>
            </div>
            {runs.length === 0 ? (
              <p className="text-sm text-muted-foreground">尚无运行记录。</p>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-8" />
                      <TableHead>任务</TableHead>
                      <TableHead>开始</TableHead>
                      <TableHead>结果</TableHead>
                      <TableHead>消息</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {runs.map((run) => (
                      <RunRow
                        key={run.id}
                        run={run}
                        jobLabel={formatJobLabel(run.job_id, customJobs)}
                      />
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
