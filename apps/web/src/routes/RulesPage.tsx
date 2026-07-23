import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, Save, Trash2 } from "lucide-react";
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
import { Skeleton } from "@/components/ui/skeleton";
import {
  addRuleEntry,
  deleteRuleEntry,
  fetchRules,
  replaceRules,
  updateRuleEntry,
} from "@/lib/workbench-api";

export function RulesPage() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [newRule, setNewRule] = useState("");

  const rulesQuery = useQuery({
    queryKey: ["rules"],
    queryFn: fetchRules,
  });

  useEffect(() => {
    if (rulesQuery.data) {
      setDraft(rulesQuery.data.content ?? "");
    }
  }, [rulesQuery.data]);

  const refreshRules = (message: string) => {
    toast.success(message);
    void queryClient.invalidateQueries({ queryKey: ["rules"] });
  };

  const replaceMutation = useMutation({
    mutationFn: () => replaceRules(draft, rulesQuery.data?.version),
    onSuccess: () => refreshRules("已保存规则"),
    onError: (err: unknown) => toast.error(err instanceof Error ? err.message : "保存失败"),
  });

  const addMutation = useMutation({
    mutationFn: () => addRuleEntry(newRule),
    onSuccess: () => {
      setNewRule("");
      refreshRules("已新增规则");
    },
    onError: (err: unknown) => toast.error(err instanceof Error ? err.message : "新增失败"),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string }) => updateRuleEntry(id, text),
    onSuccess: () => refreshRules("已更新规则"),
    onError: (err: unknown) => toast.error(err instanceof Error ? err.message : "更新失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteRuleEntry(id),
    onSuccess: () => refreshRules("已删除规则"),
    onError: (err: unknown) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const entries = Array.isArray(rulesQuery.data?.entries) ? rulesQuery.data.entries : [];
  const chars = rulesQuery.data?.chars_used ?? draft.length;
  const limit = rulesQuery.data?.limit ?? 4000;
  const busy =
    replaceMutation.isPending ||
    addMutation.isPending ||
    updateMutation.isPending ||
    deleteMutation.isPending;

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-5xl space-y-6">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">规则</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            仅您可编辑的顶层规则。Agent 只读注入，优先级高于技能与记忆。
          </p>
        </div>

        {rulesQuery.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载中…
          </div>
        ) : null}

        <div className="grid gap-6 lg:grid-cols-5">
          <Card className="lg:col-span-3">
            <CardHeader>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <CardTitle className="text-base">RULES.md</CardTitle>
                  <CardDescription>
                    {chars}/{limit} chars
                  </CardDescription>
                </div>
                <Button
                  size="sm"
                  onClick={() => replaceMutation.mutate()}
                  disabled={busy || rulesQuery.isLoading}
                >
                  {replaceMutation.isPending ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="mr-2 h-4 w-4" />
                  )}
                  保存
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              {rulesQuery.isLoading ? (
                <Skeleton className="h-72 w-full" />
              ) : (
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  className="min-h-72 w-full resize-y rounded-md border border-input bg-background px-3 py-2 font-mono text-sm leading-relaxed ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  placeholder={"§\n单票最大仓位不超过总资产的 20%。"}
                />
              )}
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle className="text-base">条目</CardTitle>
              <CardDescription>{entries.length} 条启用规则</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex gap-2">
                <Input
                  value={newRule}
                  onChange={(event) => setNewRule(event.target.value)}
                  placeholder="新增一条规则"
                  disabled={busy}
                />
                <Button
                  size="icon"
                  onClick={() => addMutation.mutate()}
                  disabled={busy || !newRule.trim()}
                  title="新增规则"
                >
                  {addMutation.isPending ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Plus className="h-4 w-4" />
                  )}
                </Button>
              </div>

              {rulesQuery.isLoading ? (
                <Skeleton className="h-40 w-full" />
              ) : entries.length === 0 ? (
                <p className="text-sm text-muted-foreground">暂无规则。</p>
              ) : (
                <div className="space-y-3">
                  {entries.map((entry) => (
                    <div key={entry.id} className="rounded-md border p-3">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <Badge variant="secondary" className="font-mono text-[10px]">
                            {entry.id}
                          </Badge>
                          <NewBadge createdAt={entry.created_at} />
                        </div>
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-7 w-7 text-destructive hover:text-destructive"
                          onClick={() => deleteMutation.mutate(entry.id)}
                          disabled={busy}
                          title="删除规则"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                      <textarea
                        defaultValue={entry.text}
                        onBlur={(event) => {
                          const text = event.target.value.trim();
                          if (text && text !== entry.text) {
                            updateMutation.mutate({ id: entry.id, text });
                          }
                        }}
                        className="min-h-20 w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-sm leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        disabled={busy}
                      />
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
