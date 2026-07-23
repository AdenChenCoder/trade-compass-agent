import { Loader2, MessageSquarePlus, PanelLeftClose, Pencil, Trash2 } from "lucide-react";
import { useCallback, useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NewBadge } from "@/components/ui/new-badge";
import type { AgentSessionSummary } from "@/lib/agent-api";
import { cn } from "@/lib/utils";

function formatRelativeTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin} 分钟前`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} 小时前`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return `${diffDay} 天前`;
  return date.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

function sessionLabel(session: AgentSessionSummary): string {
  if (session.title?.trim()) return session.title.trim();
  if (session.preview?.trim()) return session.preview.trim();
  return "新对话";
}

type SessionBucket = "today" | "yesterday" | "earlier";

function sessionBucket(iso: string): SessionBucket {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "earlier";
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  if (date >= startOfToday) return "today";
  if (date >= startOfYesterday) return "yesterday";
  return "earlier";
}

const BUCKET_LABELS: Record<SessionBucket, string> = {
  today: "今天",
  yesterday: "昨天",
  earlier: "更早",
};

const BUCKET_ORDER: SessionBucket[] = ["today", "yesterday", "earlier"];

function groupSessions(sessions: AgentSessionSummary[]) {
  const buckets: Record<SessionBucket, AgentSessionSummary[]> = {
    today: [],
    yesterday: [],
    earlier: [],
  };
  for (const session of sessions) {
    buckets[sessionBucket(session.updated_at)].push(session);
  }
  return BUCKET_ORDER.filter((key) => buckets[key].length > 0).map((key) => ({
    key,
    label: BUCKET_LABELS[key],
    sessions: buckets[key],
  }));
}

function filterSessions(
  sessions: AgentSessionSummary[],
  query: string,
): AgentSessionSummary[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return sessions;
  return sessions.filter((session) => {
    const title = session.title?.toLowerCase() ?? "";
    const preview = session.preview?.toLowerCase() ?? "";
    const id = session.session_id.toLowerCase();
    return title.includes(needle) || preview.includes(needle) || id.includes(needle);
  });
}

interface SessionSidebarProps {
  sessions: AgentSessionSummary[];
  activeSessionId: string | null;
  loading: boolean;
  streaming: boolean;
  mobileOpen: boolean;
  onMobileClose: () => void;
  onNewChat: () => void;
  onSelectSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string) => void;
  onRenameSession?: (sessionId: string, title: string) => Promise<void>;
}

export function SessionSidebar({
  sessions,
  activeSessionId,
  loading,
  streaming,
  mobileOpen,
  onMobileClose,
  onNewChat,
  onSelectSession,
  onDeleteSession,
  onRenameSession,
}: SessionSidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [savingTitle, setSavingTitle] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const filteredSessions = filterSessions(sessions, searchQuery);
  const grouped = groupSessions(filteredSessions);

  const startEditing = useCallback((session: AgentSessionSummary) => {
    setEditingId(session.session_id);
    setEditTitle(sessionLabel(session));
  }, []);

  const cancelEditing = useCallback(() => {
    setEditingId(null);
    setEditTitle("");
  }, []);

  const commitRename = useCallback(
    async (sessionId: string) => {
      const trimmed = editTitle.trim();
      if (!trimmed || !onRenameSession) {
        cancelEditing();
        return;
      }
      setSavingTitle(true);
      try {
        await onRenameSession(sessionId, trimmed);
        cancelEditing();
      } finally {
        setSavingTitle(false);
      }
    },
    [cancelEditing, editTitle, onRenameSession],
  );

  const handleEditKeyDown = useCallback(
    (event: KeyboardEvent<HTMLInputElement>, sessionId: string) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void commitRename(sessionId);
      }
      if (event.key === "Escape") {
        cancelEditing();
      }
    },
    [cancelEditing, commitRename],
  );

  const panel = (
    <aside
      className={cn(
        "flex min-h-0 w-[260px] shrink-0 flex-col border-r bg-muted/20",
        "md:relative md:h-full md:translate-x-0",
        mobileOpen
          ? "fixed bottom-0 left-0 top-14 z-40 translate-x-0 shadow-lg"
          : "fixed bottom-0 left-0 top-14 z-40 -translate-x-full md:static md:z-auto md:h-full",
      )}
    >
      <div className="flex items-center justify-between gap-2 border-b px-3 py-3">
        <Button
          type="button"
          variant="default"
          size="sm"
          className="flex-1 justify-start"
          onClick={onNewChat}
        >
          <MessageSquarePlus className="mr-1.5 h-4 w-4" />
          新对话
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="shrink-0 md:hidden"
          onClick={onMobileClose}
          aria-label="关闭历史"
        >
          <PanelLeftClose className="h-4 w-4" />
        </Button>
      </div>

      {sessions.length > 0 ? (
        <div className="border-b px-3 py-2">
          <Input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜索对话…"
            className="h-8 text-sm"
            disabled={loading}
          />
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载历史…
          </div>
        ) : filteredSessions.length === 0 ? (
          <p className="px-2 py-4 text-center text-xs text-muted-foreground">
            {searchQuery.trim() ? "没有匹配的对话" : "暂无历史对话"}
          </p>
        ) : (
          <div className="space-y-4">
            {grouped.map((group) => (
              <section key={group.key}>
                <p className="px-2 pb-1 text-xs font-medium text-muted-foreground">
                  {group.label}
                </p>
                <ul className="space-y-1">
                  {group.sessions.map((session) => {
                    const active = session.session_id === activeSessionId;
                    const editing = editingId === session.session_id;
                    return (
                      <li key={session.session_id}>
                        <div
                          className={cn(
                            "group relative flex items-start gap-2 rounded-lg px-2 py-2 transition-colors",
                            active ? "bg-muted" : "hover:bg-muted/60",
                          )}
                        >
                          {active ? (
                            <span
                              className="absolute bottom-2 left-0 top-2 w-0.5 rounded-full bg-primary"
                              aria-hidden
                            />
                          ) : null}
                          {editing ? (
                            <div className="min-w-0 flex-1 pl-1">
                              <Input
                                value={editTitle}
                                onChange={(e) => setEditTitle(e.target.value)}
                                onKeyDown={(e) =>
                                  handleEditKeyDown(e, session.session_id)
                                }
                                onBlur={() => void commitRename(session.session_id)}
                                disabled={savingTitle}
                                className="h-8 text-sm"
                                autoFocus
                              />
                            </div>
                          ) : (
                            <button
                              type="button"
                              className="min-w-0 flex-1 pl-1 text-left"
                              onClick={() => onSelectSession(session.session_id)}
                            >
                              <p className="flex items-center gap-1.5 text-sm font-medium leading-snug">
                                <span className="truncate">{sessionLabel(session)}</span>
                                <NewBadge createdAt={session.created_at} className="shrink-0" />
                              </p>
                              <p className="mt-0.5 truncate text-xs text-muted-foreground">
                                {formatRelativeTime(session.updated_at)}
                                {session.message_count > 0
                                  ? ` · ${session.message_count} 条`
                                  : ""}
                              </p>
                            </button>
                          )}
                          {!editing && onRenameSession ? (
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
                              disabled={streaming}
                              onClick={() => startEditing(session)}
                              aria-label="重命名对话"
                            >
                              <Pencil className="h-3.5 w-3.5 text-muted-foreground" />
                            </Button>
                          ) : null}
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
                            disabled={streaming}
                            onClick={() => onDeleteSession(session.session_id)}
                            aria-label="删除对话"
                          >
                            <Trash2 className="h-3.5 w-3.5 text-muted-foreground" />
                          </Button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
        )}
      </div>
    </aside>
  );

  return (
    <>
      {mobileOpen ? (
        <button
          type="button"
          className="fixed inset-0 z-30 bg-background/60 backdrop-blur-sm md:hidden"
          aria-label="关闭历史面板"
          onClick={onMobileClose}
        />
      ) : null}
      {panel}
    </>
  );
}

export function SessionSidebarToggle({
  onClick,
  disabled,
}: {
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="md:hidden"
      onClick={onClick}
      disabled={disabled}
      aria-label="打开历史对话"
    >
      <PanelLeftClose className="mr-1.5 h-4 w-4" />
      历史
    </Button>
  );
}
