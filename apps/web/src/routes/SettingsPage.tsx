import { useQuery } from "@tanstack/react-query";
import { Loader2, Server, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
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
import { fetchAgentMcp, fetchAgentSkills } from "@/lib/agent-api";
import { MobileConnection } from "@/components/MobileConnection";
import { AgentConnection } from "@/components/AgentConnection";

export function SettingsPage() {
  const skillsQuery = useQuery({
    queryKey: ["agent-skills"],
    queryFn: fetchAgentSkills,
  });

  const mcpQuery = useQuery({
    queryKey: ["agent-mcp"],
    queryFn: fetchAgentMcp,
  });

  const loading = skillsQuery.isLoading || mcpQuery.isLoading;
  const error = skillsQuery.error ?? mcpQuery.error;

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-3xl space-y-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">设置</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            管理 Agent 接入与手机连接，查看 Skills 和 MCP 服务状态。
          </p>
        </div>

        <AgentConnection />
        <MobileConnection />

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载中…
          </div>
        ) : null}

        {error ? (
          <Card className="border-destructive/40">
            <CardHeader>
              <CardTitle className="text-base text-destructive">无法加载配置</CardTitle>
              <CardDescription>
                {error instanceof Error ? error.message : "未知错误"}
              </CardDescription>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              请稍后刷新重试；若持续失败，请检查交易罗盘服务是否正常运行。
            </CardContent>
          </Card>
        ) : null}

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Sparkles className="h-4 w-4" />
              Skills
            </CardTitle>
            <CardDescription>
              查看可用的研究技能及启用状态。
            </CardDescription>
          </CardHeader>
          <CardContent>
            {skillsQuery.isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : (skillsQuery.data?.skills.length ?? 0) === 0 ? (
              <p className="text-sm text-muted-foreground">暂无已加载 skill。</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>名称</TableHead>
                    <TableHead>说明</TableHead>
                    <TableHead>来源</TableHead>
                    <TableHead>状态</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {skillsQuery.data?.skills.map((skill) => (
                    <TableRow key={skill.name}>
                      <TableCell className="font-mono text-xs">{skill.name}</TableCell>
                      <TableCell className="text-muted-foreground">
                        {skill.description ?? "—"}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {skill.source ?? skill.path ?? "—"}
                      </TableCell>
                      <TableCell>
                        <Badge variant={skill.enabled !== false ? "default" : "secondary"}>
                          {skill.enabled !== false ? "已启用" : "未启用"}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Server className="h-4 w-4" />
              MCP Servers
            </CardTitle>
            <CardDescription>查看外部工具服务的连接状态。</CardDescription>
          </CardHeader>
          <CardContent>
            {mcpQuery.isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : (mcpQuery.data?.servers.length ?? 0) === 0 ? (
              <p className="text-sm text-muted-foreground">未配置 MCP server。</p>
            ) : (
              <ul className="space-y-3">
                {mcpQuery.data?.servers.map((server) => (
                  <li
                    key={server.name}
                    className="rounded-lg border p-3 text-sm"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono font-medium">{server.name}</span>
                      <Badge
                        variant={
                          server.status === "connected" || server.status === "ok"
                            ? "default"
                            : "secondary"
                        }
                      >
                        {server.status}
                      </Badge>
                    </div>
                    {server.command ? (
                      <p className="mt-1 font-mono text-xs text-muted-foreground">
                        {server.command}
                      </p>
                    ) : null}
                    {server.tools && server.tools.length > 0 ? (
                      <p className="mt-2 text-xs text-muted-foreground">
                        Tools ({server.tools.length}): {server.tools.join(", ")}
                      </p>
                    ) : server.status === "connected" ? (
                      <p className="mt-2 text-xs text-muted-foreground">Tools: 0</p>
                    ) : null}
                    {server.error ? (
                      <p className="mt-2 text-xs text-destructive">{server.error}</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
