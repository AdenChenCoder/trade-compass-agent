import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pin, PinOff, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { NewBadge } from "@/components/ui/new-badge";
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
import { fetchSkillDetail, fetchSkills, pinSkill } from "@/lib/workbench-api";
import type { Skill, SkillDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

const STATE_LABELS: Record<string, { label: string; variant: "default" | "secondary" | "outline" | "destructive" }> = {
  active: { label: "活跃", variant: "default" },
  stale: { label: "闲置", variant: "secondary" },
  archived: { label: "已归档", variant: "outline" },
};

function stateDisplay(state: string) {
  const info = STATE_LABELS[state] ?? { label: state, variant: "secondary" as const };
  return <Badge variant={info.variant}>{info.label}</Badge>;
}

function SkillContent({ detail }: { detail: SkillDetail }) {
  return (
    <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted/50 p-3 text-xs leading-relaxed">
      {detail.content}
    </pre>
  );
}

export function SkillsPage() {
  const queryClient = useQueryClient();
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null);

  const skillsQuery = useQuery({
    queryKey: ["skills"],
    queryFn: () => fetchSkills(true),
  });

  const detailQuery = useQuery({
    queryKey: ["skill-detail", selectedSkill],
    queryFn: () => fetchSkillDetail(selectedSkill!),
    enabled: !!selectedSkill,
  });

  const pinMutation = useMutation({
    mutationFn: ({ name, pinned }: { name: string; pinned: boolean }) => pinSkill(name, pinned),
    onSuccess: () => {
      toast.success("已更新固定状态");
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (err: unknown) => {
      toast.error(err instanceof Error ? err.message : "操作失败");
    },
  });

  const skills: Skill[] = skillsQuery.data?.skills ?? [];
  const detail = detailQuery.data ?? null;

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">技能</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Agent 自主进化的技能。技能由 Agent 在交互中自动创建和改进，无需人工审批。
          </p>
        </div>

        {skillsQuery.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载中…
          </div>
        ) : null}

        <div className="grid gap-6 lg:grid-cols-5">
          <Card className="lg:col-span-3">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Sparkles className="h-4 w-4" />
                交易技能
              </CardTitle>
              <CardDescription>{skills.length} 个技能</CardDescription>
            </CardHeader>
            <CardContent>
              {skills.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  暂无技能。Agent 会在交互中自动创建。
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>名称</TableHead>
                        <TableHead>类别</TableHead>
                        <TableHead>状态</TableHead>
                        <TableHead className="text-right">使用</TableHead>
                        <TableHead />
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {skills.map((skill) => (
                        <TableRow
                          key={skill.name}
                          className={cn(
                            "cursor-pointer",
                            selectedSkill === skill.name && "bg-muted/60",
                          )}
                          onClick={() => setSelectedSkill(skill.name)}
                        >
                          <TableCell className="max-w-[180px] truncate font-medium">
                            {skill.pinned && <Pin className="mr-1 inline h-3 w-3 text-primary" />}
                            {skill.name}
                            <NewBadge createdAt={skill.created_at} className="ml-2 align-middle" />
                          </TableCell>
                          <TableCell>
                            <Badge variant="secondary">{skill.category}</Badge>
                          </TableCell>
                          <TableCell>{stateDisplay(skill.state)}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {skill.use_count}
                          </TableCell>
                          <TableCell className="w-8">
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7"
                              onClick={(e) => {
                                e.stopPropagation();
                                pinMutation.mutate({ name: skill.name, pinned: !skill.pinned });
                              }}
                              title={skill.pinned ? "取消固定" : "固定"}
                            >
                              {skill.pinned ? (
                                <PinOff className="h-3.5 w-3.5" />
                              ) : (
                                <Pin className="h-3.5 w-3.5" />
                              )}
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

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle className="text-base">技能详情</CardTitle>
            </CardHeader>
            <CardContent>
              {detail ? (
                <div className="space-y-3">
                  <div>
                    <p className="text-sm font-medium">{detail.name}</p>
                    <p className="mt-0.5 text-xs text-muted-foreground">{detail.description}</p>
                  </div>
                  <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                    {detail.created_by && <span>创建者: {detail.created_by}</span>}
                    {detail.patch_count > 0 && <span>· 修改 {detail.patch_count} 次</span>}
                    {detail.last_used_at && (
                      <span>· 最近使用: {new Date(detail.last_used_at).toLocaleDateString()}</span>
                    )}
                  </div>
                  <SkillContent detail={detail} />
                </div>
              ) : detailQuery.isLoading ? (
                <Skeleton className="h-32 w-full" />
              ) : (
                <p className="text-sm text-muted-foreground">选择一个技能查看详情。</p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
