import { EmptyState, QueryNotice } from "@/components/layout/WorkspaceState";
import { PageHeading } from "@/components/layout/PageHeading";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, ListChecks, Loader2, Plus, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
    <div className="workspace-page rules-page">
      <div className="page-container">
        <PageHeading icon={BookOpen} title="规则" description="规则优先于技能与记忆。" />

        {rulesQuery.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载中…
          </div>
        ) : null}

        {rulesQuery.error ? <QueryNotice title="无法读取规则" error={rulesQuery.error} retry={() => void rulesQuery.refetch()} /> : null}
        {!rulesQuery.error ? <Tabs defaultValue="entries" className="rules-tabs"><TabsList><TabsTrigger value="entries"><ListChecks size={16} />逐条管理</TabsTrigger><TabsTrigger value="document"><BookOpen size={16} />全文编辑{draft !== (rulesQuery.data?.content ?? "") ? <span className="unsaved-dot" aria-label="有未保存的更改" /> : null}</TabsTrigger></TabsList><TabsContent value="document" forceMount>
          <Card className="rule-editor">
            <CardHeader>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <CardTitle className="text-base">规则全文</CardTitle>
                  <CardDescription>
                    已保存 {chars} / {limit} 字符
                  </CardDescription>
                </div>
                <Button
                  size="sm"
                  onClick={() => replaceMutation.mutate()}
                  disabled={busy || rulesQuery.isLoading || !!rulesQuery.error}
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
                  aria-label="规则全文"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  className="rule-document min-h-72 w-full resize-y rounded-md border border-input bg-background px-3 py-2 font-mono text-sm leading-relaxed ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                  placeholder={"§\n单票最大仓位不超过总资产的 20%。"}
                />
              )}
              <div className="editor-status"><span>{draft !== (rulesQuery.data?.content ?? "") ? "有未保存的更改" : "编辑后点击保存"}</span><span>{draft.length} 字符</span></div>
            </CardContent>
          </Card>

          </TabsContent><TabsContent value="entries" forceMount><Card className="rule-entries">
            <CardHeader>
              <CardTitle className="text-base">逐条管理</CardTitle>
              <CardDescription>{entries.length} 条规则，修改条目后离开输入框即可保存</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex gap-2">
                <Input
                  aria-label="新增规则"
                  value={newRule}
                  onChange={(event) => setNewRule(event.target.value)}
                  placeholder="新增一条规则"
                  disabled={busy || rulesQuery.isLoading}
                />
                <Button
                  size="icon"
                  onClick={() => addMutation.mutate()}
                  disabled={busy || rulesQuery.isLoading || !newRule.trim()}
                  title="新增规则"
                  aria-label="新增规则"
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
                <EmptyState icon={BookOpen} title="还没有规则" description="添加你的第一条投资约束。" />
              ) : (
                <div className="rule-entry-list">
                  {entries.map((entry) => (
                    <div key={entry.id} className="rule-entry">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <Badge variant="secondary" className="font-mono text-[10px]">
                            {entry.id}
                          </Badge>
                          <Badge variant="outline">{entry.enabled ? "启用" : "停用"}</Badge>
                          <NewBadge createdAt={entry.created_at} />
                        </div>
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-7 w-7 text-destructive hover:text-destructive"
                          onClick={() => deleteMutation.mutate(entry.id)}
                          disabled={busy}
                          title="删除规则"
                          aria-label={`删除规则 ${entry.id}`}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                      <textarea
                        aria-label={`规则 ${entry.id}`}
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
          </TabsContent></Tabs> : null}
      </div>
    </div>
  );
}
