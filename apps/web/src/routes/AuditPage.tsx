import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ClipboardList, Loader2, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { fetchAudit, fetchAuditEvent } from "@/lib/workbench-api";

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function AuditPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const auditQuery = useQuery({
    queryKey: ["audit"],
    queryFn: () => fetchAudit(100),
  });

  const detailQuery = useQuery({
    queryKey: ["audit", selectedId],
    queryFn: () => fetchAuditEvent(selectedId!),
    enabled: Boolean(selectedId),
  });

  const selectedFromList = auditQuery.data?.find((event) => event.id === selectedId);
  const detail = detailQuery.data ?? selectedFromList;

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-6xl space-y-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">审计回放</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            推荐与决策事件列表，点击行查看摘要与完整 payload。
          </p>
        </div>

        {auditQuery.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载中…
          </div>
        ) : null}

        {auditQuery.error ? (
          <Card className="border-destructive/40">
            <CardHeader>
              <CardTitle className="text-base text-destructive">无法加载审计记录</CardTitle>
              <CardDescription>
                {auditQuery.error instanceof Error ? auditQuery.error.message : "未知错误"}
              </CardDescription>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              请确认 FastAPI 已启动且 <code>/api/audit</code> 可用。
            </CardContent>
          </Card>
        ) : null}

        <div className="grid gap-6 lg:grid-cols-[1fr,min(420px,40%)]">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <ClipboardList className="h-4 w-4" />
                事件列表
              </CardTitle>
              <CardDescription>按时间倒序（最新在前）</CardDescription>
            </CardHeader>
            <CardContent>
              {auditQuery.isLoading ? (
                <Skeleton className="h-48 w-full" />
              ) : (auditQuery.data?.length ?? 0) === 0 ? (
                <p className="text-sm text-muted-foreground">暂无审计事件。</p>
              ) : (
                <div className="-mx-6 overflow-x-auto px-6">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>时间</TableHead>
                        <TableHead>类型</TableHead>
                        <TableHead>摘要</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {auditQuery.data?.map((event) => (
                        <TableRow
                          key={event.id}
                          className={cn(
                            "cursor-pointer",
                            selectedId === event.id && "bg-muted",
                          )}
                          onClick={() => setSelectedId(event.id)}
                        >
                          <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                            {formatTime(event.timestamp)}
                          </TableCell>
                          <TableCell>
                            <Badge variant="secondary">{event.event_type}</Badge>
                          </TableCell>
                          <TableCell className="max-w-[240px] truncate">{event.summary}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>

          <Card className={cn(!selectedId && "hidden lg:block")}>
            <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0">
              <div>
                <CardTitle className="text-base">事件详情</CardTitle>
                <CardDescription>
                  {selectedId ? "摘要与 JSON payload" : "选择左侧一行查看详情"}
                </CardDescription>
              </div>
              {selectedId ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="shrink-0 lg:hidden"
                  aria-label="关闭详情"
                  onClick={() => setSelectedId(null)}
                >
                  <X className="h-4 w-4" />
                </Button>
              ) : null}
            </CardHeader>
            <CardContent>
              {!selectedId ? (
                <p className="text-sm text-muted-foreground">尚未选择事件。</p>
              ) : detailQuery.isLoading && !detail ? (
                <Skeleton className="h-32 w-full" />
              ) : detailQuery.error ? (
                <p className="text-sm text-destructive">
                  {detailQuery.error instanceof Error
                    ? detailQuery.error.message
                    : "加载详情失败"}
                </p>
              ) : detail ? (
                <div className="space-y-4">
                  <div className="space-y-2 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge>{detail.event_type}</Badge>
                      <span className="font-mono text-xs text-muted-foreground">{detail.id}</span>
                    </div>
                    <p className="text-xs text-muted-foreground">{formatTime(detail.timestamp)}</p>
                    <p>{detail.summary}</p>
                  </div>
                  <div>
                    <p className="mb-2 text-xs font-medium text-muted-foreground">Payload</p>
                    <pre className="max-h-[min(60vh,480px)] overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
                      {JSON.stringify(detail.payload, null, 2)}
                    </pre>
                  </div>
                </div>
              ) : null}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
