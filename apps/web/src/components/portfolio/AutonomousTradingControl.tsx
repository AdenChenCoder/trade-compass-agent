import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { fetchAutonomousTrading, updateAutonomousTrading } from "@/lib/workbench-api";

const queryKey = ["autonomous-trading"];

export function AutonomousTradingControl() {
  const client = useQueryClient();
  const settings = useQuery({
    queryKey,
    queryFn: fetchAutonomousTrading,
    refetchInterval: 30_000,
  });
  const update = useMutation({
    mutationFn: updateAutonomousTrading,
    onMutate: () => client.cancelQueries({ queryKey }),
    onSuccess: (data) => {
      client.setQueryData(queryKey, data);
      toast.success(data.enabled ? "已开启 Agent 自主交易" : "已关闭 Agent 自主交易");
    },
  });
  const error = update.error ?? settings.error;
  const enabled = settings.data?.enabled ?? false;
  const unavailable = !settings.data || !!settings.error;

  return (
    <div className="rounded-lg border bg-card px-4 py-3">
      <div className="flex items-center justify-between gap-4">
        <div className="space-y-1">
          <p id="autonomous-trading-label" className="text-sm font-medium">Agent 自主交易</p>
          <p id="autonomous-trading-description" className="text-xs leading-relaxed text-muted-foreground">
            开启后，每个交易日盘中分析 8 轮，按决策自主买卖模拟持仓；对话和定时任务也可自主交易。
            关闭后仍可按你的明确指令交易。
          </p>
        </div>
        <Button
          role="switch"
          aria-checked={enabled}
          aria-labelledby="autonomous-trading-label"
          aria-describedby="autonomous-trading-description"
          aria-busy={update.isPending || settings.isPending}
          variant={enabled ? "default" : "outline"}
          size="sm"
          className="shrink-0"
          disabled={unavailable || update.isPending}
          onClick={() => update.mutate(!enabled)}
        >
          {update.isPending || settings.isPending ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
          {settings.isPending ? "读取中" : unavailable ? "不可用" : enabled ? "已开启" : "已关闭"}
        </Button>
      </div>
      {error ? <p role="alert" className="mt-2 text-xs text-destructive">{error.message}</p> : null}
    </div>
  );
}
