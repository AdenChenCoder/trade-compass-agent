import { ApiError } from "@/lib/agent-api";

export function ForecastUnavailableNotice({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError ? error.message : "预测请求失败，请稍后重试。";
  const recoveryCommand =
    error instanceof ApiError ? error.recoveryCommand : undefined;
  const restartRequired =
    error instanceof ApiError ? error.restartRequired : false;

  return (
    <div
      role="status"
      className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-muted-foreground"
    >
      <p className="m-0 font-medium text-foreground">预测暂不可用</p>
      <p className="mb-0 mt-1">{message}</p>
      {recoveryCommand ? (
        <div className="mt-2">
          <p className="m-0">在终端运行：</p>
          <code className="mt-1 block break-all rounded bg-muted px-2 py-1 text-[11px] text-foreground">
            {recoveryCommand}
          </code>
          {restartRequired ? (
            <p className="mb-0 mt-1">安装完成后请重启 Trade Compass。</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
