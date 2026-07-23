import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type FormEvent,
  type SetStateAction,
} from "react";
import { ChevronDown, Loader2, Paperclip, Send, Square, X } from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { toast } from "sonner";
import { ActivityTimeline } from "@/components/agent/ActivityTimeline";
import { ChatMessage } from "@/components/agent/ChatMessage";
import { SessionSidebar, SessionSidebarToggle } from "@/components/agent/SessionSidebar";
import { SkillSlashMenu, type SkillSlashMenuHandle } from "@/components/agent/SkillSlashMenu";
import { ToolTrace } from "@/components/agent/ToolTrace";
import { Button } from "@/components/ui/button";
import {
  agentStreamUrl,
  ApiError,
  createAgentSession,
  deleteAgentSession,
  fetchAgentSessionMessagePage,
  fetchAgentSessions,
  isSessionNotFoundError,
  patchAgentSessionTitle,
  postAgentControl,
  postAgentTurn,
  type AgentSessionSummary,
  type SessionMessage,
  type TurnAttachment,
} from "@/lib/agent-api";
import {
  isNearMessageListBottom,
  prependUniqueMessages,
  shouldFetchStoredSessionOnMount,
} from "@/lib/agent-session-load";
import {
  clearStoredSessionId,
  getStoredSessionId,
  setStoredSessionId,
} from "@/lib/agent-session-storage";
import type {
  ChatMessage as ChatMessageType,
  ToolTraceEntry,
  TurnResponse,
  TurnSection,
} from "@/lib/types";
import {
  appendActivity,
  completeActivity,
  getActivityDisplayMode,
  markStatusDone,
  nextTraceId,
} from "@/lib/activity-trace";
import { filterVisibleSections } from "@/lib/sections";
import {
  createTurnOutcomeRefs,
  hasAssistantPayload,
  markAssistantDelivered,
  markTurnSucceeded,
  parseDonePayload,
  resetTurnOutcomeRefs,
  shouldShowGlobalFailure,
  stripErrorsAfterLastUser,
  type TurnOutcomeRefs,
} from "@/lib/turn-outcome";
import { useAgentSSE } from "@/hooks/useAgentSSE";
import { cn } from "@/lib/utils";

function nextId(): string {
  return nextTraceId();
}

const URL_RE = /https?:\/\/[^\s<>"']+/gi;

function extractUrls(text: string): string[] {
  const matches = text.match(URL_RE);
  if (!matches) return [];
  return [...new Set(matches.map((url) => url.replace(/[.,;:!?)]+$/, "")))];
}

function parseSectionFromEvent(d: Record<string, unknown>): TurnSection | null {
  const nested = d.section as TurnSection | undefined;
  if (nested?.title && nested?.content) {
    return nested;
  }
  if (typeof d.title === "string" && typeof d.content === "string") {
    return {
      title: d.title,
      content: d.content,
      specialist: typeof d.specialist === "string" ? d.specialist : undefined,
      symbols: Array.isArray(d.symbols) ? (d.symbols as string[]) : undefined,
      kind:
        typeof d.kind === "string"
          ? (d.kind as TurnSection["kind"])
          : undefined,
      forecast_data:
        d.forecast_data && typeof d.forecast_data === "object"
          ? (d.forecast_data as TurnSection["forecast_data"])
          : undefined,
    };
  }
  return null;
}

const MESSAGE_PAGE_SIZE = 50;

function messagesFromSession(
  history: SessionMessage[],
  startIndex = 0,
): ChatMessageType[] {
  const result: ChatMessageType[] = [];
  for (let i = 0; i < history.length; i++) {
    const message = history[i];
    const sections = filterVisibleSections(message.sections);
    const toolCalls = message.tool_calls?.length
      ? message.tool_calls.map((tc) => ({ name: tc.name }))
      : undefined;
    if (message.role === "assistant" && !message.content.trim() && sections.length === 0) {
      continue;
    }
    result.push({
      id: `history-${startIndex + i}`,
      role: message.role,
      content: message.content,
      sections: sections.length > 0 ? sections : undefined,
      toolCalls,
      timestamp: message.timestamp ? Date.parse(message.timestamp) : Date.now(),
    });
  }
  return result;
}

function appendAssistantMessage(
  setMessages: Dispatch<SetStateAction<ChatMessageType[]>>,
  summary: string,
  sections: TurnSection[] | undefined,
) {
  const visibleSections = sections ? filterVisibleSections(sections) : [];
  if (!hasAssistantPayload(summary, visibleSections)) {
    return;
  }
  setMessages((prev) => [
    ...stripErrorsAfterLastUser(prev),
    {
      id: nextId(),
      role: "assistant",
      content: summary,
      sections: visibleSections.length > 0 ? visibleSections : undefined,
      timestamp: Date.now(),
    },
  ]);
}

function applyTurnResponse(
  response: TurnResponse,
  setMessages: Dispatch<SetStateAction<ChatMessageType[]>>,
) {
  appendAssistantMessage(setMessages, response.summary, response.sections);
}

function hasToolErrors(entries: ToolTraceEntry[]): boolean {
  return entries.some((entry) => entry.status === "error");
}

function appendTurnError(
  setMessages: Dispatch<SetStateAction<ChatMessageType[]>>,
  message: string,
) {
  setMessages((prev) => [
    ...prev,
    { id: nextId(), role: "error", content: message, timestamp: Date.now() },
  ]);
}

export function AgentPage() {
  const [messages, setMessages] = useState<ChatMessageType[]>([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [toolTrace, setToolTrace] = useState<ToolTraceEntry[]>([]);
  const [pendingSections, setPendingSections] = useState<TurnSection[]>([]);
  const [attachments, setAttachments] = useState<TurnAttachment[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [showNewMessages, setShowNewMessages] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const slashMenuRef = useRef<SkillSlashMenuHandle>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const sseSessionRef = useRef<string | null>(null);
  const turnOutcomeRef = useRef<TurnOutcomeRefs>(createTurnOutcomeRefs());
  const streamingIdRef = useRef<string | null>(null);
  const streamingContentRef = useRef("");
  const turnIdRef = useRef<string | null>(null);
  const abortTurnRef = useRef<AbortController | null>(null);
  const historyPageAbortRef = useRef<AbortController | null>(null);
  const stopRequestedRef = useRef(false);
  const resumeWatchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sseEventReceivedRef = useRef(false);
  const turnEndedRef = useRef(false);
  const loadingOlderRef = useRef(false);
  const initialScrollPendingRef = useRef(false);
  const prependAnchorRef = useRef<{ scrollHeight: number; scrollTop: number } | null>(null);
  const isNearBottomRef = useRef(true);
  const [streamingMessage, setStreamingMessage] = useState<ChatMessageType | null>(null);
  const { connect, disconnect, onStatusChange } = useAgentSSE();
  const finalAnswerStarted = streamingMessage !== null;
  const activityDisplayMode = getActivityDisplayMode(
    streaming,
    finalAnswerStarted,
    toolTrace.length > 0,
  );

  const messageVirtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 180,
    overscan: 6,
    getItemKey: (index) => messages[index]?.id ?? index,
  });
  const virtualMessages = messageVirtualizer.getVirtualItems();

  const scrollToBottom = useCallback((force = false) => {
    if (!force && !isNearBottomRef.current) {
      setShowNewMessages(true);
      return;
    }
    requestAnimationFrame(() => {
      if (listRef.current) {
        listRef.current.scrollTop = listRef.current.scrollHeight;
        isNearBottomRef.current = true;
        setShowNewMessages(false);
      }
    });
  }, []);

  useLayoutEffect(() => {
    const element = listRef.current;
    if (!element) return;
    if (initialScrollPendingRef.current) {
      initialScrollPendingRef.current = false;
      let frameId = 0;
      let attempts = 0;
      let stableFrames = 0;
      let previousHeight = -1;
      const settleAtBottom = () => {
        element.scrollTop = element.scrollHeight;
        const currentHeight = element.scrollHeight;
        stableFrames = currentHeight === previousHeight ? stableFrames + 1 : 0;
        previousHeight = currentHeight;
        attempts += 1;
        if (stableFrames < 2 && attempts < 20) {
          frameId = requestAnimationFrame(settleAtBottom);
        } else {
          isNearBottomRef.current = true;
          setShowNewMessages(false);
        }
      };
      messageVirtualizer.scrollToIndex(messages.length - 1, { align: "end" });
      frameId = requestAnimationFrame(settleAtBottom);
      return () => cancelAnimationFrame(frameId);
    }
    const anchor = prependAnchorRef.current;
    if (anchor) {
      prependAnchorRef.current = null;
      element.scrollTop = anchor.scrollTop + (element.scrollHeight - anchor.scrollHeight);
    }
  }, [messageVirtualizer, messages.length, sessionId]);

  const refreshSessions = useCallback(async () => {
    try {
      const list = await fetchAgentSessions(30);
      setSessions(list.sessions);
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "加载对话列表失败",
      );
    } finally {
      setLoadingSessions(false);
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  useEffect(() => {
    onStatusChange((status) => {
      if (status === "reconnecting" && !turnEndedRef.current) {
        toast.warning("连接断开，正在重连…");
      }
      if (status === "connected") {
        turnEndedRef.current = false;
        toast.dismiss();
      }
    });
  }, [onStatusChange]);

  useEffect(() => () => disconnect(), [disconnect]);

  const clearMissingStoredSession = useCallback(() => {
    clearStoredSessionId();
    setSessionId(null);
    setMessages([]);
    setNextBefore(null);
    setLoadingHistory(false);
  }, []);

  const [resumeStreamSessionId, setResumeStreamSessionId] = useState<string | null>(null);

  useEffect(() => {
    const storedSessionId = getStoredSessionId();
    if (!shouldFetchStoredSessionOnMount(storedSessionId)) {
      return;
    }

    setLoadingHistory(true);
    let cancelled = false;
    const controller = new AbortController();
    historyPageAbortRef.current = controller;
    void (async () => {
      try {
        const detail = await fetchAgentSessionMessagePage(storedSessionId, {
          limit: MESSAGE_PAGE_SIZE,
          signal: controller.signal,
        });
        if (cancelled) return;
        initialScrollPendingRef.current = true;
        setSessionId(detail.session_id);
        setMessages(messagesFromSession(detail.messages, detail.page.start_index));
        setNextBefore(detail.page.next_before);
        if (detail.has_active_turn) {
          setStreaming(true);
          setResumeStreamSessionId(detail.session_id);
        }
      } catch (err) {
        if (cancelled) return;
        if (isSessionNotFoundError(err)) {
          clearMissingStoredSession();
          return;
        }
        toast.error(
          err instanceof ApiError ? err.message : "加载对话历史失败",
        );
      } finally {
        if (!cancelled) setLoadingHistory(false);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
      if (historyPageAbortRef.current === controller) {
        historyPageAbortRef.current = null;
      }
    };
  }, [clearMissingStoredSession]);

  const addFileAttachment = useCallback((file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") return;
      if (file.type.startsWith("image/")) {
        const base64 = result.includes(",") ? result.split(",")[1] : result;
        setAttachments((prev) => [
          ...prev,
          { type: "image", content: base64, mime: file.type || "image/png" },
        ]);
        return;
      }
      if (file.type === "application/pdf" || file.name.endsWith(".pdf")) {
        const base64 = result.includes(",") ? result.split(",")[1] : result;
        setAttachments((prev) => [
          ...prev,
          { type: "pdf" as "text", content: base64, mime: "application/pdf" },
        ]);
        return;
      }
      const text = result.includes(",") ? atob(result.split(",")[1]) : result;
      setAttachments((prev) => [
        ...prev,
        { type: "text", content: text.slice(0, 50000), mime: file.type || undefined },
      ]);
    };
    if (file.type.startsWith("image/") || file.type === "application/pdf" || file.name.endsWith(".pdf")) {
      reader.readAsDataURL(file);
    } else {
      reader.readAsText(file);
    }
  }, []);

  const setupSSE = useCallback(
    (sid: string) => {
      if (sseSessionRef.current === sid) return;
      disconnect();
      sseSessionRef.current = sid;

      connect(agentStreamUrl(sid), {
        status: (d) => {
          sseEventReceivedRef.current = true;
          setToolTrace((prev) => appendActivity(prev, "status", d));
          scrollToBottom();
        },
        tool_start: (d) => {
          sseEventReceivedRef.current = true;
          setToolTrace((prev) => appendActivity(prev, "tool_start", d));
          scrollToBottom();
        },
        tool_end: (d) => {
          setToolTrace((prev) => completeActivity(prev, "tool_end", d));
        },
        skill_loaded: (d) => {
          setToolTrace((prev) => appendActivity(prev, "skill_loaded", d));
        },
        turn_started: (d) => {
          sseEventReceivedRef.current = true;
          if (typeof d.turn_id === "string") {
            turnIdRef.current = d.turn_id;
          }
        },
        delta: (d) => {
          sseEventReceivedRef.current = true;
          const chunk = typeof d.text === "string" ? d.text : "";
          if (!chunk) return;
          if (!streamingIdRef.current) {
            streamingIdRef.current = nextId();
            streamingContentRef.current = chunk;
            markAssistantDelivered(turnOutcomeRef.current);
            setStreamingMessage({
              id: streamingIdRef.current,
              role: "assistant",
              content: chunk,
              timestamp: Date.now(),
              streaming: true,
            });
          } else {
            streamingContentRef.current += chunk;
            setStreamingMessage((prev) =>
              prev ? { ...prev, content: prev.content + chunk } : prev,
            );
          }
          scrollToBottom();
        },
        specialist_started: (d) => {
          setToolTrace((prev) => appendActivity(prev, "specialist_started", d));
          scrollToBottom();
        },
        specialist_done: (d) => {
          setToolTrace((prev) => completeActivity(prev, "specialist_done", d));
        },
        section: (d) => {
          const section = parseSectionFromEvent(d);
          if (section && filterVisibleSections([section]).length > 0) {
            setPendingSections((prev) => [...prev, section]);
          }
          scrollToBottom();
        },
        done: (d) => {
          sseEventReceivedRef.current = true;
          markTurnSucceeded(turnOutcomeRef.current);
          const { ok, summary, sections } = parseDonePayload(d);
          const visibleSections = sections ? filterVisibleSections(sections) : undefined;
          setPendingSections((pending) => {
            const visiblePending = filterVisibleSections(pending);
            const mergedSections =
              visibleSections && visibleSections.length > 0
                ? visibleSections
                : visiblePending.length > 0
                  ? visiblePending
                  : undefined;

            if (hasAssistantPayload(summary, mergedSections)) {
              if (!turnOutcomeRef.current.assistantDelivered) {
                markAssistantDelivered(turnOutcomeRef.current);
                appendAssistantMessage(setMessages, summary, mergedSections);
              } else if (streamingIdRef.current) {
                const draftId = streamingIdRef.current;
                const draftContent = streamingContentRef.current;
                setMessages((prev) => [
                  ...stripErrorsAfterLastUser(prev.filter((m) => m.id !== draftId)),
                  {
                    id: draftId,
                    role: "assistant",
                    content: summary || draftContent,
                    sections:
                      mergedSections && mergedSections.length > 0
                        ? mergedSections
                        : undefined,
                    timestamp: Date.now(),
                    streaming: false,
                  },
                ]);
                streamingIdRef.current = null;
                streamingContentRef.current = "";
                setStreamingMessage(null);
              } else {
                setMessages((prev) => stripErrorsAfterLastUser(prev));
              }
            } else if (
              shouldShowGlobalFailure(turnOutcomeRef.current, "done", {
                ok,
                summary,
                sections: mergedSections,
              })
            ) {
              appendTurnError(setMessages, "Agent 执行失败");
            }

            return [];
          });
          setToolTrace((prev) => {
            const next = markStatusDone(prev);
            if (turnOutcomeRef.current.succeeded && hasToolErrors(next)) {
              toast.warning("部分工具失败，回复基于可用数据生成");
            }
            return next;
          });
          setStreaming(false);
          turnEndedRef.current = true;
          scrollToBottom();
        },
        interrupted: (d) => {
          markTurnSucceeded(turnOutcomeRef.current);
          const { summary, sections } = parseDonePayload(d);
          const visibleSections = sections ? filterVisibleSections(sections) : undefined;
          setPendingSections((pending) => {
            const visiblePending = filterVisibleSections(pending);
            const mergedSections =
              visibleSections && visibleSections.length > 0
                ? visibleSections
                : visiblePending.length > 0
                  ? visiblePending
                  : undefined;

            if (hasAssistantPayload(summary, mergedSections)) {
              if (!turnOutcomeRef.current.assistantDelivered) {
                markAssistantDelivered(turnOutcomeRef.current);
                appendAssistantMessage(setMessages, summary, mergedSections);
              } else if (streamingIdRef.current) {
                const draftId = streamingIdRef.current;
                const draftContent = streamingContentRef.current;
                setMessages((prev) => [
                  ...stripErrorsAfterLastUser(prev.filter((m) => m.id !== draftId)),
                  {
                    id: draftId,
                    role: "assistant",
                    content: summary || draftContent,
                    sections:
                      mergedSections && mergedSections.length > 0
                        ? mergedSections
                        : undefined,
                    timestamp: Date.now(),
                    streaming: false,
                  },
                ]);
                streamingIdRef.current = null;
                streamingContentRef.current = "";
                setStreamingMessage(null);
              }
            }

            return [];
          });
          setToolTrace((prev) => markStatusDone(prev));
          setStreaming(false);
          turnEndedRef.current = true;
          toast.message("已停止");
          scrollToBottom();
        },
        error: (d) => {
          if (
            !shouldShowGlobalFailure(turnOutcomeRef.current, "sse_error", {
              ok: d.ok === false ? false : undefined,
              summary: typeof d.summary === "string" ? d.summary : undefined,
              sections: Array.isArray(d.sections) ? d.sections : undefined,
            })
          ) {
            setStreaming(false);
            turnEndedRef.current = true;
            return;
          }
          const msg = String(d.message ?? d.detail ?? "Agent 执行失败");
          appendTurnError(setMessages, msg);
          setStreaming(false);
          turnEndedRef.current = true;
          setToolTrace([]);
        },
      });
    },
    [connect, disconnect, scrollToBottom],
  );

  useEffect(() => {
    if (!resumeStreamSessionId) return;
    const sid = resumeStreamSessionId;
    sseEventReceivedRef.current = false;
    setupSSE(sid);
    setResumeStreamSessionId(null);

    if (resumeWatchdogRef.current) clearTimeout(resumeWatchdogRef.current);
    resumeWatchdogRef.current = setTimeout(async () => {
      resumeWatchdogRef.current = null;
      if (sseEventReceivedRef.current) return;
      try {
        const detail = await fetchAgentSessionMessagePage(sid, {
          limit: MESSAGE_PAGE_SIZE,
        });
        if (!detail.has_active_turn) {
          initialScrollPendingRef.current = true;
          setMessages(messagesFromSession(detail.messages, detail.page.start_index));
          setNextBefore(detail.page.next_before);
          setStreaming(false);
          setToolTrace([]);
        }
      } catch {
        setStreaming(false);
      }
    }, 8000);

    return () => {
      if (resumeWatchdogRef.current) {
        clearTimeout(resumeWatchdogRef.current);
        resumeWatchdogRef.current = null;
      }
    };
  }, [resumeStreamSessionId, setupSSE]);

  const handleStop = useCallback(async () => {
    if (!streaming) return;
    const sid = sessionId;
    if (!sid) return;
    stopRequestedRef.current = true;
    try {
      await postAgentControl({
        session_id: sid,
        action: "interrupt",
        turn_id: turnIdRef.current ?? undefined,
      });
    } catch {
      // Turn may have already finished.
    }
    abortTurnRef.current?.abort();
  }, [sessionId, streaming]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    let text = input.trim();
    if (!text || streaming) return;

    const skillMatch = text.match(/^\/skill\s+([\w-]+)\s*(.*)/s);
    if (skillMatch) {
      const [, skillName, rest] = skillMatch;
      const userMsg = rest.trim();
      text = userMsg
        ? `[请使用 ${skillName} 技能] ${userMsg}`
        : `请加载并使用 ${skillName} 技能`;
    }

    const urlAttachments = extractUrls(text).map((url) => ({ type: "url" as const, url }));
    const existingUrls = new Set(
      attachments.filter((a) => a.type === "url" && a.url).map((a) => a.url),
    );
    const mergedAttachments = [
      ...attachments,
      ...urlAttachments.filter((a) => a.url && !existingUrls.has(a.url)),
    ];
    const turnAttachments = mergedAttachments.length > 0 ? mergedAttachments : undefined;

    setInput("");
    setAttachments([]);
    setMessages((prev) => [
      ...prev,
      { id: nextId(), role: "user", content: text, timestamp: Date.now() },
    ]);
    setStreaming(true);
    setToolTrace([]);
    setPendingSections([]);
    resetTurnOutcomeRefs(turnOutcomeRef.current);
    turnEndedRef.current = false;
    stopRequestedRef.current = false;
    turnIdRef.current = null;
    streamingIdRef.current = null;
    streamingContentRef.current = "";
    setStreamingMessage(null);
    const abortController = new AbortController();
    abortTurnRef.current = abortController;
    scrollToBottom(true);

    try {
      let sid = sessionId;
      if (!sid) {
        const created = await createAgentSession();
        sid = created.session_id;
        setSessionId(sid);
        setStoredSessionId(sid);
      }
      setupSSE(sid);

      const response = await postAgentTurn(
        {
          message: text,
          session_id: sid,
          attachments: turnAttachments,
        },
        { signal: abortController.signal },
      );

      setSessionId(response.session_id);
      setStoredSessionId(response.session_id);
      if (response.turn_id) {
        turnIdRef.current = response.turn_id;
      }
      void refreshSessions();

      const visibleSections = filterVisibleSections(response.sections);
      if (hasAssistantPayload(response.summary, visibleSections)) {
        markTurnSucceeded(turnOutcomeRef.current);
        if (!turnOutcomeRef.current.assistantDelivered) {
          markAssistantDelivered(turnOutcomeRef.current);
          applyTurnResponse(response, setMessages);
        } else {
          setMessages((prev) => stripErrorsAfterLastUser(prev));
        }
        setToolTrace((prev) => {
          const next = markStatusDone(prev);
          if (hasToolErrors(next)) {
            toast.warning("部分工具失败，回复基于可用数据生成");
          }
          return next;
        });
      } else if (response.interrupted) {
        markTurnSucceeded(turnOutcomeRef.current);
        toast.message("已停止");
      }
      setStreaming(false);
    } catch (err) {
      if (stopRequestedRef.current) {
        setStreaming(false);
        setToolTrace((prev) => markStatusDone(prev));
        return;
      }
      if (!shouldShowGlobalFailure(turnOutcomeRef.current, "http")) {
        setStreaming(false);
        setMessages((prev) => stripErrorsAfterLastUser(prev));
        return;
      }
      setStreaming(false);
      setToolTrace([]);
      const msg =
        err instanceof ApiError
          ? err.status === 503
            ? `${err.message}（请配置 LLM API key）`
            : err.status === 502
              ? err.message || "Agent 执行失败，请稍后重试"
              : err.status >= 500
                ? err.message || `服务错误 (${err.status})`
                : err.message
          : err instanceof Error
            ? err.message
            : "发送失败";
      toast.error(msg);
      appendTurnError(setMessages, msg);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (slashMenuRef.current?.handleKey(e)) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit(e as unknown as FormEvent);
    }
  };

  const handleSkillSelect = useCallback((skillName: string) => {
    if (!skillName) {
      setInput("");
      return;
    }
    setInput(`/skill ${skillName} `);
    textareaRef.current?.focus();
  }, []);

  const resetChatState = useCallback(() => {
    disconnect();
    sseSessionRef.current = null;
    setMessages([]);
    setInput("");
    setAttachments([]);
    setToolTrace([]);
    setPendingSections([]);
    setStreaming(false);
    setNextBefore(null);
    setLoadingOlder(false);
    setShowNewMessages(false);
    loadingOlderRef.current = false;
    historyPageAbortRef.current?.abort();
    historyPageAbortRef.current = null;
    initialScrollPendingRef.current = false;
    prependAnchorRef.current = null;
    isNearBottomRef.current = true;
    streamingIdRef.current = null;
    streamingContentRef.current = "";
    setStreamingMessage(null);
    setLoadingHistory(false);
  }, [disconnect]);

  const handleNewChat = () => {
    if (streaming && sessionId) {
      void postAgentControl({ session_id: sessionId, action: "interrupt" }).catch(() => {});
      abortTurnRef.current?.abort();
    }
    clearStoredSessionId();
    setSessionId(null);
    resetChatState();
    setSidebarOpen(false);
  };

  const handleSelectSession = async (targetId: string) => {
    if (targetId === sessionId) {
      setSidebarOpen(false);
      return;
    }
    if (streaming && sessionId) {
      void postAgentControl({ session_id: sessionId, action: "interrupt" }).catch(() => {});
      abortTurnRef.current?.abort();
    }
    resetChatState();
    setLoadingHistory(true);
    setSidebarOpen(false);
    const controller = new AbortController();
    historyPageAbortRef.current = controller;
    try {
      const detail = await fetchAgentSessionMessagePage(targetId, {
        limit: MESSAGE_PAGE_SIZE,
        signal: controller.signal,
      });
      initialScrollPendingRef.current = true;
      setSessionId(detail.session_id);
      setStoredSessionId(detail.session_id);
      setMessages(messagesFromSession(detail.messages, detail.page.start_index));
      setNextBefore(detail.page.next_before);
    } catch (err) {
      if (isSessionNotFoundError(err)) {
        clearMissingStoredSession();
        void refreshSessions();
        toast.error("对话不存在或已删除");
        return;
      }
      toast.error(
        err instanceof ApiError ? err.message : "加载对话历史失败",
      );
    } finally {
      if (historyPageAbortRef.current === controller) {
        historyPageAbortRef.current = null;
      }
      setLoadingHistory(false);
    }
  };

  const loadOlderMessages = useCallback(async () => {
    if (!sessionId || nextBefore == null || loadingOlderRef.current) return;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    const controller = new AbortController();
    historyPageAbortRef.current = controller;
    try {
      const detail = await fetchAgentSessionMessagePage(sessionId, {
        limit: MESSAGE_PAGE_SIZE,
        before: nextBefore,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      const element = listRef.current;
      if (element) {
        prependAnchorRef.current = {
          scrollHeight: element.scrollHeight,
          scrollTop: element.scrollTop,
        };
      }
      const older = messagesFromSession(detail.messages, detail.page.start_index);
      setMessages((current) => prependUniqueMessages(current, older));
      setNextBefore(detail.page.next_before);
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        toast.error(err instanceof ApiError ? err.message : "加载更早消息失败");
      }
    } finally {
      if (historyPageAbortRef.current === controller) {
        historyPageAbortRef.current = null;
      }
      loadingOlderRef.current = false;
      setLoadingOlder(false);
    }
  }, [nextBefore, sessionId]);

  const handleMessageScroll = useCallback(
    (event: React.UIEvent<HTMLDivElement>) => {
      const element = event.currentTarget;
      const nearBottom = isNearMessageListBottom(
        element.scrollHeight,
        element.scrollTop,
        element.clientHeight,
      );
      isNearBottomRef.current = nearBottom;
      if (nearBottom) setShowNewMessages(false);
      if (element.scrollTop <= 120) {
        void loadOlderMessages();
      }
    },
    [loadOlderMessages],
  );

  const handleDeleteSession = async (targetId: string) => {
    if (streaming) return;
    try {
      await deleteAgentSession(targetId);
      if (targetId === sessionId) {
        clearStoredSessionId();
        resetChatState();
        setSessionId(null);
      }
      void refreshSessions();
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "删除对话失败",
      );
    }
  };

  const handleRenameSession = useCallback(
    async (targetId: string, title: string) => {
      if (streaming) return;
      try {
        await patchAgentSessionTitle(targetId, title);
        void refreshSessions();
      } catch (err) {
        toast.error(
          err instanceof ApiError ? err.message : "重命名对话失败",
        );
        throw err;
      }
    },
    [refreshSessions, streaming],
  );

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <SessionSidebar
        sessions={sessions}
        activeSessionId={sessionId}
        loading={loadingSessions}
        streaming={streaming}
        mobileOpen={sidebarOpen}
        onMobileClose={() => setSidebarOpen(false)}
        onNewChat={() => void handleNewChat()}
        onSelectSession={(id) => void handleSelectSession(id)}
        onDeleteSession={(id) => void handleDeleteSession(id)}
        onRenameSession={handleRenameSession}
      />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      <div
        ref={listRef}
        data-testid="agent-message-list"
        onScroll={handleMessageScroll}
        className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-4">
          <div className="flex justify-end md:hidden">
            <SessionSidebarToggle
              onClick={() => setSidebarOpen(true)}
              disabled={streaming}
            />
          </div>

          {loadingHistory ? (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              加载对话历史…
            </div>
          ) : null}

          {messages.length === 0 && !streaming && !loadingHistory ? (
            <div className="rounded-xl border border-dashed p-8 text-center text-sm text-muted-foreground">
              <p className="font-medium text-foreground">Agent 对话</p>
              <p className="mt-2">
                例如：「600519 短线怎么看」「今天大盘资金情况」
              </p>
            </div>
          ) : null}

          {loadingOlder ? (
            <div className="sticky top-2 z-10 flex justify-center">
              <span className="flex items-center gap-2 rounded-full border bg-background/95 px-3 py-1 text-xs text-muted-foreground shadow-sm">
                <Loader2 className="h-3 w-3 animate-spin" /> 加载更早消息…
              </span>
            </div>
          ) : null}

          {messages.length > 0 ? (
            <div
              data-testid="agent-message-virtual-space"
              className="relative w-full"
              style={{ height: messageVirtualizer.getTotalSize() }}
            >
              {virtualMessages.map((virtualMessage) => {
                const message = messages[virtualMessage.index];
                if (!message) return null;
                return (
                  <div
                    key={message.id}
                    ref={messageVirtualizer.measureElement}
                    data-index={virtualMessage.index}
                    data-message-index={virtualMessage.index}
                    className="absolute left-0 top-0 w-full pb-4"
                    style={{ transform: `translateY(${virtualMessage.start}px)` }}
                  >
                    <ChatMessage message={message} />
                  </div>
                );
              })}
            </div>
          ) : null}

          {streamingMessage &&
          !messages.some((m) => m.id === streamingMessage.id) ? (
            <ChatMessage message={streamingMessage} />
          ) : null}

          {streaming ? (
            <div className="space-y-3">
              {activityDisplayMode === "expanded" ? (
                <ActivityTimeline entries={toolTrace} />
              ) : null}
              {filterVisibleSections(pendingSections).map((section, i) => (
                <div key={`pending-${i}`} className="opacity-80">
                  <ChatMessage
                    message={{
                      id: `section-${i}`,
                      role: "assistant",
                      content: "",
                      sections: [section],
                      timestamp: Date.now(),
                    }}
                  />
                </div>
              ))}
            </div>
          ) : null}

          {activityDisplayMode === "collapsed" ? (
            <div className="flex justify-start">
              <div className="w-fit max-w-[85%]">
                <ToolTrace entries={toolTrace} />
              </div>
            </div>
          ) : null}

          {showNewMessages ? (
            <div className="sticky bottom-2 z-20 flex justify-center">
              <Button
                type="button"
                size="sm"
                variant="secondary"
                className="rounded-full shadow-md"
                onClick={() => scrollToBottom(true)}
              >
                <ChevronDown className="mr-1 h-4 w-4" /> 查看新消息
              </Button>
            </div>
          ) : null}
        </div>
      </div>

      <form
        onSubmit={handleSubmit}
        className="shrink-0 border-t bg-background/90 p-4 backdrop-blur-sm"
      >
        <div className="mx-auto max-w-3xl">
          <div
            className={cn(
              "relative rounded-xl border border-input bg-background shadow-sm",
              "focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 focus-within:ring-offset-background",
            )}
          >
            <SkillSlashMenu
              ref={slashMenuRef}
              input={input}
              onSelect={handleSkillSelect}
            />
            {attachments.length > 0 ? (
              <ul className="flex flex-wrap gap-2 border-b px-3 py-2">
                {attachments.map((att, i) => (
                  <li
                    key={`${att.type}-${i}`}
                    className="flex items-center gap-1 rounded-md border bg-muted/40 px-2 py-1 text-xs"
                  >
                    <span className="max-w-[200px] truncate">
                      {att.type === "url"
                        ? att.url
                        : att.type === "image"
                          ? "图片"
                          : att.mime === "application/pdf"
                            ? "PDF 文件"
                            : "文本文件"}
                    </span>
                    <button
                      type="button"
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => setAttachments((prev) => prev.filter((_, idx) => idx !== i))}
                      aria-label="移除附件"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}

            <div className="flex items-end gap-1 p-2">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*,.txt,.md,.csv,.json,.pdf"
                className="hidden"
                multiple
                onChange={(e) => {
                  const files = e.target.files;
                  if (!files) return;
                  for (const file of files) addFileAttachment(file);
                  e.target.value = "";
                }}
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="shrink-0 text-muted-foreground hover:text-foreground"
                disabled={streaming}
                onClick={() => fileInputRef.current?.click()}
                title="添加附件"
              >
                <Paperclip className="h-4 w-4" />
              </Button>

              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                onPaste={(e) => {
                  const items = e.clipboardData?.items;
                  if (!items) return;
                  for (const item of items) {
                    if (item.kind === "file") {
                      const file = item.getAsFile();
                      if (file) addFileAttachment(file);
                    }
                  }
                }}
                placeholder="输入问题，可粘贴链接或添加附件…"
                disabled={streaming}
                rows={1}
                className={cn(
                  "max-h-40 min-h-[2.5rem] flex-1 resize-none bg-transparent px-1 py-2 text-sm",
                  "placeholder:text-muted-foreground focus-visible:outline-none",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                )}
              />

              <Button
                type={streaming ? "button" : "submit"}
                size="icon"
                className="shrink-0"
                disabled={!streaming && !input.trim()}
                title={streaming ? "停止" : "发送"}
                onClick={streaming ? () => void handleStop() : undefined}
              >
                {streaming ? (
                  <Square className="h-4 w-4 fill-current" />
                ) : (
                  <Send className="h-4 w-4" />
                )}
              </Button>
            </div>
          </div>
        </div>
      </form>
      </div>
    </div>
  );
}
