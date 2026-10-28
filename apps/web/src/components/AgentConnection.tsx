import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  fetchAgentConnection, saveAgentConnection, checkAgentConnection,
  type AgentConnectionSettings,
} from "@/lib/agent-api";

export function AgentConnection() {
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["agent-connection"], queryFn: fetchAgentConnection });
  const [draft, setDraft] = useState<{ provider: string; model: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const current = draft ?? query.data;
  const dirty = draft !== null && (draft.provider !== query.data?.provider || draft.model !== query.data?.model);

  async function act(work: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await work(); }
    catch (err) { setError(err instanceof Error ? err.message : "操作未完成，请重试。"); }
    finally { setBusy(false); }
  }

  return <Card>
    <CardHeader><CardTitle className="text-base">Agent 接入</CardTitle></CardHeader>
    <CardContent className="space-y-4">
      {query.isPending ? <p className="text-sm text-muted-foreground">正在读取接入设置…</p> : null}
      {query.error ? <p role="alert" className="text-sm text-destructive">无法读取接入设置，请刷新重试。</p> : null}
      {current && query.data ? <>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <label htmlFor="agent-provider" className="text-sm font-medium">接入方式</label>
            <select id="agent-provider" disabled={busy} value={current.provider}
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onChange={event => {
                const provider = event.target.value;
                const previous = query.data as AgentConnectionSettings;
                setDraft({ provider, model: provider === previous.provider ? previous.model :
                  previous.options.find(option => option.provider === provider)?.model ?? "" });
                setMessage(""); setError("");
              }}>
              {!query.data.options.some(option => option.provider === current.provider)
                ? <option value={current.provider} disabled>当前接入不可用</option> : null}
              {query.data.options.map(option => <option key={option.provider} value={option.provider}>{option.label}</option>)}
            </select>
          </div>
          <div className="space-y-2">
            <label htmlFor="agent-model" className="text-sm font-medium">模型</label>
            <Input id="agent-model" value={current.model} maxLength={200} disabled={busy || current.provider === "disabled"}
              placeholder="输入模型名称"
              onChange={event => {
                setDraft({ provider: current.provider, model: event.target.value });
                setMessage(""); setError("");
              }} />
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button disabled={busy || !dirty} onClick={() => act(async () => {
            const result = await saveAgentConnection(current.provider, current.model);
            cache.setQueryData(["agent-connection"], result);
            setDraft(null);
            setMessage("接入设置已保存，将用于后续任务。");
          })}>保存</Button>
          <Button variant="outline" disabled={busy || dirty} onClick={() => act(async () => {
            const result = await checkAgentConnection();
            setMessage(result.message);
          })}>{busy ? "处理中…" : "检查接入状态"}</Button>
        </div>
      </> : null}
      {message ? <p role="status" className="text-sm text-muted-foreground">{message}</p> : null}
      {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
    </CardContent>
  </Card>;
}
