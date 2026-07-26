from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from trade_compass_agent import __version__
from trade_compass_agent.command_catalog import (
    command_catalog,
    command_help,
    render_command_catalog,
)
from trade_compass_agent.config import (
    ensure_runtime_dirs,
    initialize_runtime_files,
    invalidate_config_cache,
    load_app_config,
    load_project_dotenv,
    settings_from_config,
)
from trade_compass_agent.logging_config import setup_logging
from trade_compass_agent.data import (
    AkshareProvider,
    BaostockProvider,
    ChainProvider,
    LocalBarCacheProvider,
    SinaMinuteProvider,
    create_market_data_provider,
)
from trade_compass_agent.evaluation import FollowThroughEvaluator
from trade_compass_agent.domain import Notification
from trade_compass_agent.memory.rules_store import RulesStore
from trade_compass_agent.ops.audit import JsonAuditLog
from trade_compass_agent.ops.notifications import JsonNotificationStore, NotificationCenter
from trade_compass_agent.ops.tick_scheduler import TickScheduler
from trade_compass_agent.runtime.exceptions import AgentUnavailableError
from trade_compass_agent.runtime.loop import AgentLoop
from trade_compass_agent.runtime.market_stack import MarketStack

DEFAULT_PORT = 19704


def run_market_pulse() -> None:
    pulse = MarketStack.from_config().market_pulse_provider.get_market_pulse()
    print(f"Provider: {pulse.provider_name}")
    print(f"Time: {pulse.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    print("Notes:")
    for note in pulse.notes:
        print(f"  - {note}")
    print("Top sectors:")
    for sector in pulse.sectors[:8]:
        leader = f", leader={sector.leader}" if sector.leader else ""
        print(f"  - {sector.name}: {sector.change_pct:.2f}%{leader}")
    print(
        "Limit-up: "
        f"{pulse.limit_up.count}, strong={pulse.limit_up.strong_count}, "
        f"industries={', '.join(pulse.limit_up.top_industries) or 'n/a'}"
    )
    if pulse.warnings:
        print("Warnings:")
        for warning in pulse.warnings:
            print(f"  - {warning}")


def run_agent(message: str) -> None:
    try:
        result = AgentLoop.from_config().run_turn(message)
    except AgentUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print(result.summary)
    for section in result.sections:
        print(f"\n## {section.title}")
        print(section.content[:2000])


def run_compress(session_id: str | None = None, *, focus: str | None = None) -> None:
    """Manually compress a session's conversation context.

    Loads the session from the store, applies Phase 1 trimming + Phase 2
    LLM summarization, and saves the compressed version back.
    """
    config = load_app_config()
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
    from trade_compass_agent.runtime.compression import trim_tool_results, summarize_middle_turns
    from trade_compass_agent.runtime.context import _sanitize_tool_calls
    from trade_compass_agent.runtime.session import SessionMessageRecord

    ss = config.context_compression
    client = create_chat_client(config)
    session_store = __import__(
        "trade_compass_agent.runtime.session", fromlist=["SessionStore"]
    ).SessionStore(config.data_dir / "agent_sessions")

    # Resolve session
    if session_id:
        session = session_store.get(session_id)
        if not session:
            print(f"Session not found: {session_id}", file=sys.stderr)
            raise SystemExit(1)
    else:
        sessions = session_store.list_recent(limit=1)
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            raise SystemExit(1)
        session = sessions[0]

    print(f"Session: {session.session_id}")
    history = _sanitize_tool_calls(
        [m.to_chat_message() for m in session_store.load_context(session)]
    )
    print(f"Messages before: {len(history)}")

    # Phase 1: trim
    trimmed, trimmed_count, trimmed_saved = trim_tool_results(
        history,
        protect_recent_count=ss.protect_recent_count,
        protect_recent_tokens=ss.protect_recent_tokens,
    )
    if trimmed_count:
        print(f"  Phase 1 trim: {trimmed_count} tool results pruned (~{trimmed_saved} chars)")

    # Phase 2: LLM summarize
    def _llm(sys_prompt: str, user_prompt: str) -> str:
        msgs = [
            ChatMessage(role="system", content=sys_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]
        return client.complete(msgs).content or ""

    compressed, summary, summarized, saved = summarize_middle_turns(
        trimmed,
        llm_call=_llm,
        protect_recent_count=ss.protect_recent_count,
        protect_recent_tokens=ss.protect_recent_tokens,
    )
    if summarized:
        print(
            f"  Phase 2 summary: {summarized} messages → {len(summary)} chars (~{saved} chars saved)"
        )
        if focus:
            print(f"  Focus: {focus}")
    else:
        print("  Phase 2: nothing to summarize (middle section too small)")

    print(f"Messages after: {len(compressed)}")
    saving_pct = (1 - len(compressed) / max(len(history), 1)) * 100
    print(f"Reduction: {saving_pct:.0f}%")

    # Show the summary
    if summary:
        print(f"\n--- Summary ---\n{summary[:2000]}")
        if len(summary) > 2000:
            print("…[truncated]")

    if summarized:
        archive_path = session_store.replace_context(
            session,
            [
                SessionMessageRecord(
                    role=message.role,
                    content=message.content or "",
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                    tool_calls=list(message.tool_calls or []),
                )
                for message in _sanitize_tool_calls(compressed)
                if message.role != "system"
            ],
        )
        print(f"Saved compressed session (archive: {archive_path})")


def run_ask(message: str) -> None:
    run_agent(message)


def run_data_check(
    symbols: list[str],
    timeframe: str = "1d",
    *,
    provider: str | None = None,
) -> None:
    config = load_app_config()
    symbols = symbols or config.watchlists.premarket_symbols()
    names = [provider] if provider else ["tushare", "akshare", "sina", "baostock", "auto"]
    for provider_name in names:
        print(f"\n=== {provider_name} ===")
        try:
            if provider_name == "akshare":
                prov = AkshareProvider()
            elif provider_name == "sina":
                prov = SinaMinuteProvider()
            elif provider_name == "baostock":
                prov = BaostockProvider()
            elif provider_name == "tushare":
                from trade_compass_agent.data.tushare_provider import TushareProvider

                prov = TushareProvider(token_env=config.data.tushare_token_env)
            else:
                prov = create_market_data_provider(
                    provider_name,
                    cache_dir=config.data_dir / "market_cache",
                    data=config.data,
                )
        except Exception as exc:
            print(f"  provider init failed: {exc}")
            continue
        for symbol in symbols:
            try:
                bars = prov.get_bars(symbol, timeframe=timeframe, limit=5)
                last_close = bars[-1].close if bars else "n/a"
                print(f"  {symbol}: ok ({len(bars)} {timeframe} bars, last close={last_close})")
            except Exception as exc:
                print(f"  {symbol}: FAIL - {exc}")
        warnings = getattr(prov, "last_warnings", None)
        if warnings:
            for warning in warnings:
                print(f"  warn: {warning}")


def run_job(job_id: str) -> None:
    scheduler = TickScheduler(reap_on_init=False)
    scheduler.run_job_now(job_id, trigger="cli")
    runs = scheduler.run_store.recent_runs(limit=1, job_id=job_id)
    if runs:
        run = runs[0]
        status = "OK" if run.ok else run.status.upper()
        print(f"{run.job_id}: [{status}] {run.message}")
        if run.artifact:
            print(f"artifact: {run.artifact}")
        if run.error:
            print(f"error: {run.error}")
    else:
        print(f"{job_id}: completed")


def run_scheduler_list() -> None:
    scheduler = TickScheduler(reap_on_init=False)
    print("Built-in jobs:")
    for job in scheduler.list_jobs():
        status = "enabled" if job.enabled else "disabled"
        print(f"  {job.id}: {job.name} ({job.schedule}) [{status}]")

    custom = scheduler.prompt_store.list_all()
    if custom:
        print("\nCustom prompt jobs:")
        for j in custom:
            status = "enabled" if j.enabled else "paused"
            print(f"  {j.id[:8]}…: {j.name} ({j.schedule}) [{status}] by {j.created_by}")
    else:
        print("\nNo custom prompt jobs.")


def run_scheduler_add(
    name: str, prompt: str, schedule: str, *, trading_day_only: bool = False
) -> None:
    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore
    from trade_compass_agent.ops.tick_scheduler import reload_active_scheduler

    store = PromptJobStore(config.data_dir / "scheduler.db")
    job = store.create(
        name=name,
        prompt=prompt,
        schedule=schedule,
        trading_day_only=trading_day_only,
        created_by="cli",
    )
    reload_active_scheduler()
    print(f"Created: {job.id} — {job.name} ({job.schedule})")


def run_scheduler_pause(job_id: str) -> None:
    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore

    store = PromptJobStore(config.data_dir / "scheduler.db")
    if store.set_enabled(job_id, False):
        print(f"Paused: {job_id}")
    else:
        print(f"Not found: {job_id}")


def run_scheduler_resume(job_id: str) -> None:
    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore

    store = PromptJobStore(config.data_dir / "scheduler.db")
    if store.set_enabled(job_id, True):
        print(f"Resumed: {job_id}")
    else:
        print(f"Not found: {job_id}")


def run_scheduler_remove(job_id: str) -> None:
    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore

    store = PromptJobStore(config.data_dir / "scheduler.db")
    if store.delete(job_id):
        print(f"Removed: {job_id}")
    else:
        print(f"Not found: {job_id}")


def run_scheduler_runs(limit: int = 20) -> None:
    config = load_app_config()
    from trade_compass_agent.ops.run_store import SqliteRunStore

    store = SqliteRunStore(config.data_dir / "scheduler.db")
    runs = store.recent_runs(limit)
    for run in runs:
        status = "OK" if run.ok else run.status.upper()
        ts = run.finished_at or run.started_at or run.created_at
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
        print(f"{ts_str} | {run.job_id} | {status} | {run.message}")
        if run.error:
            print(f"  error: {run.error}")


def run_notifications_recent(limit: int = 20) -> None:
    config = load_app_config()
    store = JsonNotificationStore(config.data_dir / "notifications.jsonl")
    for item in store.recent(limit):
        print(f"[{item.severity}] {item.channel} | {item.title} | {item.message}")


def run_notifications_test() -> None:
    config = load_app_config()
    store = JsonNotificationStore(
        config.data_dir / "notifications.jsonl",
        max_records=config.notifications.max_records,
    )
    center = NotificationCenter(config, store=store)
    center.send(
        Notification(
            channel="manual",
            title="测试通知",
            message="Trade Compass 通知链路正常。",
            severity="info",
        )
    )
    print("notification sent")


def run_scheduler_start() -> None:
    scheduler = TickScheduler()
    print("Starting Trade Compass scheduler. Press Ctrl+C to stop.")
    for job in scheduler.list_jobs():
        print(f"  - {job.id}: {job.schedule}")
    scheduler.start_blocking()


def run_rules_list() -> None:
    config = load_app_config()
    store = RulesStore(config.memory_dir, char_limit=config.rules.char_limit)
    entries = store.list_entries()
    print(f"RULES.md: {store.chars_used()}/{store.char_limit} chars")
    if not entries:
        print("No rules.")
        return
    for entry in entries:
        status = "enabled" if entry.enabled else "disabled"
        print(f"  - {entry.id} [{status}] {entry.text}")


def run_rules_show() -> None:
    config = load_app_config()
    store = RulesStore(config.memory_dir, char_limit=config.rules.char_limit)
    print(store.read_for_prompt())


def run_rules_add(text: str) -> None:
    config = load_app_config()
    result = RulesStore(config.memory_dir, char_limit=config.rules.char_limit).add(
        text, actor="cli"
    )
    if not result.get("ok"):
        print(result.get("error", "Failed to add rule"), file=sys.stderr)
        raise SystemExit(1)
    print(f"Added rule ({result['entries']} total)")


def run_rules_edit(entry_id: str, text: str) -> None:
    config = load_app_config()
    result = RulesStore(config.memory_dir, char_limit=config.rules.char_limit).replace(
        entry_id, text, actor="cli"
    )
    if not result.get("ok"):
        print(result.get("error", "Failed to edit rule"), file=sys.stderr)
        raise SystemExit(1)
    print(f"Updated {entry_id}")


def run_rules_remove(entry_id: str) -> None:
    config = load_app_config()
    result = RulesStore(config.memory_dir, char_limit=config.rules.char_limit).remove(
        entry_id, actor="cli"
    )
    if not result.get("ok"):
        print(result.get("error", "Failed to remove rule"), file=sys.stderr)
        raise SystemExit(1)
    print(f"Removed {entry_id}")


def run_evaluate(limit: int = 100) -> None:
    config = load_app_config()
    audit = JsonAuditLog(config.data_dir / "audit.jsonl")
    provider = ChainProvider([LocalBarCacheProvider(config.data_dir / "market_cache")])
    report = FollowThroughEvaluator(provider).evaluate(audit.events, limit=limit)
    print("Follow-through metrics:")
    for metric in report.metrics:
        print(f"  - {metric.name}: {metric.value} {metric.unit}")
    print("Recent results:")
    for item in report.results[-10:]:
        print(
            f"  - {item.symbol} {item.action} {item.signal_date}: "
            f"1d={_fmt_pct(item.return_1d)} 3d={_fmt_pct(item.return_3d)} "
            f"5d={_fmt_pct(item.return_5d)} status={item.status}"
        )
    if report.warnings:
        print("Warnings:")
        for warning in report.warnings[:10]:
            print(f"  - {warning}")


def run_memory_bootstrap(dry_run: bool = False, max_promote: int = 5) -> None:
    config = load_app_config()
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.observation_store import ObservationStore
    from trade_compass_agent.memory.promotion import (
        apply_promotions,
        rank_promotion_candidates,
    )
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.memory.skill_store import SkillStore

    skill_store = SkillStore(config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    mem_store = MemoryStore(config.memory_dir, write_gate=gate)
    obs_store = ObservationStore(config.data_dir / "observations.db")

    obs_count = obs_store.count()
    mem_count = len(mem_store.memory_entries)
    print(f"Observations: {obs_count}  |  KNOWLEDGE.md entries: {mem_count}")

    if obs_count == 0:
        print("No observations to bootstrap from.")
        return

    candidates = rank_promotion_candidates(
        obs_store, bootstrap=True, limit=100, skill_store=skill_store
    )
    if not candidates:
        print("No promotion candidates found.")
        return

    print(f"\nTop {min(max_promote, len(candidates))} candidates:")
    for i, c in enumerate(candidates[:max_promote]):
        obs = c.observation
        print(f"  {i + 1}. [score={c.score:.3f} signal={obs.total_signal}] {obs.summary[:80]}")

    if dry_run:
        print(
            f"\n(dry-run) Would promote up to {max_promote} entries. Run without --dry-run to apply."
        )
        return

    from trade_compass_agent.memory.promotion import BOOTSTRAP_THRESHOLD

    promoted = apply_promotions(
        candidates, mem_store, obs_store, max_promote=max_promote, threshold=BOOTSTRAP_THRESHOLD
    )
    print(f"\nPromoted {len(promoted)} entries to KNOWLEDGE.md")
    for c in promoted:
        print(f"  ✓ [{c.verdict}] {c.refined_text[:80]}")


def run_contradiction_scan(apply: bool = False) -> None:
    """Scan active KNOWLEDGE for conflicts; optionally apply SUPERSEDE/ARCHIVE."""
    config = load_app_config()
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
    from trade_compass_agent.memory.contradiction import (
        apply_conflict_reports,
        scan_active_conflicts,
    )
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.skill_store import SkillStore
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.runtime.bootstrap import GROUNDING_RULES

    skill_store = SkillStore(config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    gov = config.memory.governance
    mem_store = MemoryStore(
        config.memory_dir,
        write_gate=gate,
        min_inject_confidence=gov.min_inject_confidence,
    )
    active = mem_store.list_active("memory", min_confidence=gov.min_inject_confidence)
    print(f"Active KNOWLEDGE entries (conf >= {gov.min_inject_confidence}): {len(active)}")
    if not active:
        return

    def _llm_call(system_prompt: str, user_content: str) -> str:
        client = create_chat_client(config)
        msgs = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_content),
        ]
        return client.complete(msgs).content or ""

    skills = skill_store.list_skills(include_stale=False)
    skills_summary = (
        "\n".join(f"- {s.name}: {s.description or ''}" for s in skills[:20]) if skills else ""
    )
    reports = scan_active_conflicts(active, GROUNDING_RULES, skills_summary, _llm_call)

    if not reports:
        print("No conflicts detected.")
        return

    print(f"\n{len(reports)} conflict(s) found:")
    for i, r in enumerate(reports, 1):
        print(f"  {i}. [{r.verdict}] {r.entry_prefix[:60]}")
        print(f"     reason: {r.reason}")
        if r.verdict == "SUPERSEDE":
            print(f"     refined: {r.refined_text[:80]}")
            print(f"     conflicts_with: {r.conflicts_with[:60]}")

    if apply:
        applied = apply_conflict_reports(reports, mem_store)
        print(f"\nApplied {len(applied)} action(s).")
    else:
        print("\n(dry-run) Run with --apply to SUPERSEDE/archive.")


def run_memory_merge() -> None:
    config = load_app_config()
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.semantic_merge import merge_similar_entries
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.memory.skill_store import SkillStore

    skill_store = SkillStore(config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    mem_store = MemoryStore(config.memory_dir, write_gate=gate)

    def _llm_call(system_prompt: str, user_content: str) -> str:
        client = create_chat_client(config)
        msgs = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_content),
        ]
        return client.complete(msgs).content or ""

    print(f"KNOWLEDGE.md: {len(mem_store.memory_entries)} entries")
    merged = merge_similar_entries(mem_store, _llm_call, force=True)
    print(f"Merged {merged} cluster(s)")


def run_memory_pin(text: str, target: str = "memory") -> None:
    config = load_app_config()
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.skill_store import SkillStore
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.runtime.tools.self_improve import tool_memory_write

    skill_store = SkillStore(config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    store = MemoryStore(
        config.memory_dir,
        write_gate=gate,
        min_inject_confidence=config.memory.governance.min_inject_confidence,
    )
    result = json.loads(
        tool_memory_write(
            store,
            action="pin",
            content=text,
            target=target,
            actor="user",
            governance=config.memory.governance,
        )
    )
    if not result.get("ok"):
        raise SystemExit(f"memory pin failed: {result.get('error', 'unknown error')}")
    print(f"Pinned {target}: {text[:80]}")


def run_memory_forget(text: str, target: str = "memory") -> None:
    config = load_app_config()
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.skill_store import SkillStore
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.runtime.tools.self_improve import tool_memory_write

    skill_store = SkillStore(config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    store = MemoryStore(
        config.memory_dir,
        write_gate=gate,
        min_inject_confidence=config.memory.governance.min_inject_confidence,
    )
    result = json.loads(
        tool_memory_write(
            store,
            action="forget",
            content=text,
            target=target,
            actor="user",
            governance=config.memory.governance,
        )
    )
    if not result.get("ok"):
        raise SystemExit(f"memory forget failed: {result.get('error', 'unknown error')}")
    print(f"Forgot {target}: {text[:80]}")


def run_memory_reindex() -> None:
    config = load_app_config()
    from trade_compass_agent.memory.tree.search import reindex_memory_vault

    count = reindex_memory_vault(
        config.memory_dir,
        index_knowledge=config.memory.recall.index_knowledge_in_fts,
    )
    print(f"Reindexed {count} file(s) into {config.memory_dir / 'tree' / 'chunks.db'}")


def run_audit_recent(limit: int = 20) -> None:
    config = load_app_config()
    audit = JsonAuditLog(config.data_dir / "audit.jsonl")
    for event in audit.recent(limit):
        print(
            f"{event.id[:8]} | {event.timestamp.strftime('%Y-%m-%d %H:%M')} | "
            f"[{event.event_type}] {event.summary[:80]}"
        )


def run_audit_show(event_id: str) -> None:
    config = load_app_config()
    audit = JsonAuditLog(config.data_dir / "audit.jsonl")
    event = audit.get(event_id)
    if event is None:
        print(f"Audit event not found: {event_id}")
        return
    print(f"ID: {event.id}")
    print(f"Time: {event.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Type: {event.event_type}")
    print(f"Summary: {event.summary}")
    payload = event.payload
    for key in [
        "provider",
        "symbol",
        "grade_in",
        "grade_out",
        "horizon",
        "confidence",
        "position_limit_pct",
    ]:
        if key in payload:
            print(f"{key}: {payload[key]}")
    for section in ["source_rules", "evidence", "risks"]:
        values = payload.get(section) or []
        if values:
            print(f"{section}:")
            for value in values:
                print(f"  - {value}")
    if payload.get("trigger"):
        print(f"trigger: {payload['trigger']}")
    if payload.get("invalidation"):
        print(f"invalidation: {payload['invalidation']}")


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2%}"


def _resolve_port(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    env_port = os.getenv("TRADE_COMPASS_PORT")
    if env_port:
        return int(env_port)
    return DEFAULT_PORT


def _open_browser_later(url: str) -> None:
    time.sleep(1)
    webbrowser.open(url)


def run_serve(
    host: str,
    port: int,
    *,
    dev: bool,
    open_browser: bool,
    no_scheduler: bool,
) -> None:
    from trade_compass_agent.web.security import is_loopback_host

    if not is_loopback_host(host):
        print(
            "Error: remote listening is not supported by this local-first release.\n"
            "  Use --host 127.0.0.1 (default) or --host ::1.\n"
            "  Put an authenticated reverse proxy in front only after enabling a future remote mode.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if dev:
        os.environ["TRADE_COMPASS_DEV_CORS"] = "true"
    else:
        from trade_compass_agent.web.dist import resolve_web_dist

        if resolve_web_dist() is None:
            print(
                "Error: no static web bundle found.\n"
                "  Build it:  pnpm --dir apps/web build\n"
                "  Or use dev mode:  trade-compass serve --dev\n",
                file=sys.stderr,
            )
            raise SystemExit(1)

    from trade_compass_agent.daemon.log_rotation import start_launchd_log_rotation

    start_launchd_log_rotation()

    if no_scheduler:
        os.environ["TRADE_COMPASS_NO_SCHEDULER"] = "true"
        print("Scheduler: disabled (--no-scheduler)")
    else:
        config = load_app_config()
        if config.scheduler.enabled:
            print("Scheduler: will start via lifespan")
        else:
            print("Scheduler: disabled in config")

    url = f"http://{host}:{port}"
    if open_browser:
        threading.Thread(target=_open_browser_later, args=(url,), daemon=True).start()

    if dev:
        print(f"API dev server: {url}")
        print("Start the Vite dev server in another terminal:")
        print("  pnpm --dir apps/web dev")
        print("  (proxies /api/* to this server; UI at http://127.0.0.1:3000/agent)")
    else:
        print(f"Trade Compass: {url}/agent")

    import uvicorn

    uvicorn.run(
        "trade_compass_agent.web.app:app",
        host=host,
        port=port,
        reload=dev,
        reload_dirs=["src"] if dev else None,
    )


def run_setup(*, force: bool = False) -> None:
    config_path, env_path = initialize_runtime_files(force=force)
    invalidate_config_cache()
    config = load_app_config(config_path)
    ensure_runtime_dirs(settings_from_config(config))
    print("Trade Compass setup complete")
    print(f"  config: {config_path}")
    print(f"  env:    {env_path}")
    print(f"  data:   {config.data_dir}")
    print(f"  memory: {config.memory_dir}")
    print("Next: add your LLM API key to the env file, then run: trade-compass doctor")


def run_doctor() -> None:
    from trade_compass_agent.diagnostics import collect_doctor_checks, doctor_exit_code

    checks = collect_doctor_checks()
    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")
    code = doctor_exit_code(checks)
    print("Doctor: ready" if code == 0 else "Doctor: action required")
    if code:
        raise SystemExit(code)


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def run_backup(output: str | None = None) -> None:
    from trade_compass_agent.recovery import RecoveryError, create_backup

    try:
        summary = create_backup(Path(output) if output else None)
    except RecoveryError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Backup complete")
    print(f"  archive: {summary.path}")
    print(f"  files:   {summary.file_count}")
    print(f"  size:    {_format_bytes(summary.total_bytes)}")
    print(f"  roots:   {', '.join(summary.roots) or 'none'}")
    print("Keep this file private: it may contain API keys and account data.")


def run_backup_inspect(path: str) -> None:
    from trade_compass_agent.recovery import BackupValidationError, inspect_backup

    try:
        summary = inspect_backup(Path(path))
    except BackupValidationError as exc:
        print(f"Backup invalid: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Backup valid")
    print(f"  archive: {summary.path}")
    print(f"  created: {summary.created_at}")
    print(f"  app:     {summary.app_version}")
    print(f"  files:   {summary.file_count}")
    print(f"  size:    {_format_bytes(summary.total_bytes)}")
    print(f"  roots:   {', '.join(summary.roots) or 'none'}")


def run_restore(path: str, *, force: bool = False) -> None:
    from trade_compass_agent.recovery import BackupValidationError, RecoveryError, restore_backup

    try:
        plan = restore_backup(Path(path), force=force)
    except (BackupValidationError, RecoveryError) as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not force:
        print("Restore preview (no files changed)")
        print(f"  archive:   {plan.archive}")
        print(f"  files:     {len(plan.files)}")
        print(f"  overwrite: {plan.overwrite_count}")
        print(f"  create:    {plan.create_count}")
        print(f"  size:      {_format_bytes(plan.total_bytes)}")
        for archive_path, destination in plan.files[:20]:
            action = "overwrite" if destination.exists() else "create"
            print(f"  {action}: {archive_path} -> {destination}")
        if len(plan.files) > 20:
            print(f"  ... and {len(plan.files) - 20} more files")
        print("Run again with --force to create a recovery backup and apply this merge restore.")
        return
    print("Restore complete")
    print(f"  files:    {len(plan.files)}")
    print(f"  rollback: {plan.recovery_backup}")
    print("Files absent from the selected backup were preserved.")


def run_export(output: str | None = None) -> None:
    from trade_compass_agent.portability import create_portable_export
    from trade_compass_agent.recovery import RecoveryError

    try:
        summary = create_portable_export(Path(output) if output else None)
    except RecoveryError as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Portable export complete")
    print(f"  archive:  {summary.path}")
    print(f"  files:    {summary.file_count}")
    print(f"  size:     {_format_bytes(summary.total_bytes)}")
    print(f"  excluded: {summary.excluded_count}")
    print(f"  redacted: {len(summary.redacted_config_keys)} config values")
    print("This is a private migration archive, not a share-safe export.")
    print("Free-text sessions, audit records, and memory may still contain sensitive content.")


def run_export_inspect(path: str) -> None:
    from trade_compass_agent.portability import inspect_portable_export
    from trade_compass_agent.recovery import BackupValidationError

    try:
        summary = inspect_portable_export(Path(path))
    except BackupValidationError as exc:
        print(f"Portable export invalid: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Portable export valid")
    print(f"  archive:  {summary.path}")
    print(f"  created:  {summary.created_at}")
    print(f"  app:      {summary.app_version}")
    print(f"  files:    {summary.file_count}")
    print(f"  size:     {_format_bytes(summary.total_bytes)}")
    print(f"  excluded: {summary.excluded_count}")
    print(f"  redacted: {len(summary.redacted_config_keys)} config values")
    print("Privacy: private migration archive; do not publish or commit it.")


def run_import(path: str, *, force: bool = False) -> None:
    from trade_compass_agent.portability import import_portable_export
    from trade_compass_agent.recovery import BackupValidationError, RecoveryError

    try:
        plan = import_portable_export(Path(path), force=force)
    except (BackupValidationError, RecoveryError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not force:
        print("Portable import preview (no files changed)")
        print(f"  archive:   {plan.archive}")
        print(f"  files:     {len(plan.files)}")
        print(f"  overwrite: {plan.overwrite_count}")
        print(f"  create:    {plan.create_count}")
        print(f"  size:      {_format_bytes(plan.total_bytes)}")
        for archive_path, destination in plan.files[:20]:
            action = "overwrite" if destination.exists() else "create"
            print(f"  {action}: {archive_path} -> {destination}")
        if len(plan.files) > 20:
            print(f"  ... and {len(plan.files) - 20} more files")
        print("Run again with --force to back up current state and apply the merge import.")
        return
    print("Portable import complete")
    print(f"  files:    {len(plan.files)}")
    print(f"  rollback: {plan.recovery_backup}")
    print("Existing files absent from the archive and local .env/MCP credentials were preserved.")


def _configure_data_check_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("symbols", nargs="*", default=[])
    parser.add_argument(
        "--timeframe",
        default="1d",
        choices=["1d", "1m", "5m", "15m", "30m", "60m"],
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=["tushare", "akshare", "sina", "baostock", "auto"],
        help="Test a single provider (default: all including tushare when token set)",
    )


def _configure_jobs_parser(parser: argparse.ArgumentParser) -> None:
    job_sub = parser.add_subparsers(dest="scheduler_command")
    job_sub.add_parser("start", help=command_help("jobs", "start"))
    job_sub.add_parser("list", help=command_help("jobs", "list"))
    runs = job_sub.add_parser("runs", help=command_help("jobs", "runs"))
    runs.add_argument("--limit", type=int, default=20)
    run = job_sub.add_parser("run", aliases=["run-once"], help=command_help("jobs", "run"))
    run.add_argument("job_id")
    add = job_sub.add_parser("add", help=command_help("jobs", "add"))
    add.add_argument("--name", required=True)
    add.add_argument("--prompt", required=True)
    add.add_argument("--schedule", required=True)
    add.add_argument("--trading-day-only", action="store_true")
    pause = job_sub.add_parser("pause", help=command_help("jobs", "pause"))
    pause.add_argument("job_id")
    resume = job_sub.add_parser("resume", help=command_help("jobs", "resume"))
    resume.add_argument("job_id")
    remove = job_sub.add_parser("remove", help=command_help("jobs", "remove"))
    remove.add_argument("job_id")


def _configure_memory_parser(parser: argparse.ArgumentParser) -> None:
    memory_sub = parser.add_subparsers(dest="memory_command", required=True)
    memory_sub.add_parser("merge", help=command_help("memory", "merge"))
    pin = memory_sub.add_parser("pin", help=command_help("memory", "pin"))
    pin.add_argument("text")
    pin.add_argument("--target", default="memory", choices=["memory", "user"])
    forget = memory_sub.add_parser("forget", help=command_help("memory", "forget"))
    forget.add_argument("text")
    forget.add_argument("--target", default="memory", choices=["memory", "user"])
    contradictions = memory_sub.add_parser(
        "contradictions",
        help=command_help("memory", "contradictions"),
    )
    contradictions.add_argument(
        "--apply",
        action="store_true",
        help="Apply SUPERSEDE/ARCHIVE actions",
    )
    memory_sub.add_parser("reindex", help=command_help("memory", "reindex"))
    bootstrap = memory_sub.add_parser(
        "bootstrap",
        help=command_help("memory", "bootstrap"),
    )
    bootstrap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show candidates without writing",
    )
    bootstrap.add_argument(
        "--max",
        type=int,
        default=5,
        help="Max entries to promote (default: 5)",
    )


def run_commands(*, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"commands": command_catalog()}, ensure_ascii=False, indent=2))
    else:
        print(render_command_catalog())


def main() -> None:
    load_project_dotenv()
    parser = argparse.ArgumentParser(prog="trade-compass")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("market-pulse", help=command_help("market-pulse"))

    p_agent = sub.add_parser("agent", help=command_help("agent"))
    p_agent.add_argument("message")

    p_ask = sub.add_parser("ask", help="Alias for `agent`")
    p_ask.add_argument("message")

    p_data = sub.add_parser("data-check", help="Legacy alias for `data check`")
    _configure_data_check_parser(p_data)
    p_data_group = sub.add_parser("data", help="Inspect market-data providers")
    p_data_sub = p_data_group.add_subparsers(dest="data_command", required=True)
    _configure_data_check_parser(p_data_sub.add_parser("check", help=command_help("data", "check")))

    p_job = sub.add_parser("run-job", help="Legacy alias for `jobs run`")
    p_job.add_argument("job_id")

    p_scheduler = sub.add_parser("scheduler", help="Legacy alias for `jobs`")
    _configure_jobs_parser(p_scheduler)
    p_jobs = sub.add_parser("jobs", help="Manage scheduled jobs")
    _configure_jobs_parser(p_jobs)

    p_notify = sub.add_parser("notifications", help="Inspect/send local notifications")
    p_notify_sub = p_notify.add_subparsers(dest="notify_command")
    p_notify_recent = p_notify_sub.add_parser("recent")
    p_notify_recent.add_argument("--limit", type=int, default=20)
    p_notify_sub.add_parser("test")

    p_rules = sub.add_parser("rules", help="Manage human-owned RULES.md")
    p_rules_sub = p_rules.add_subparsers(dest="rules_command")
    p_rules_sub.add_parser("list")
    p_rules_sub.add_parser("show")
    p_rules_add = p_rules_sub.add_parser("add")
    p_rules_add.add_argument("text")
    p_rules_edit = p_rules_sub.add_parser("edit")
    p_rules_edit.add_argument("entry_id")
    p_rules_edit.add_argument("text")
    p_rules_remove = p_rules_sub.add_parser("remove")
    p_rules_remove.add_argument("entry_id")

    p_eval = sub.add_parser("evaluate", help="Evaluate 1/3/5 day signal follow-through")
    p_eval.add_argument("--limit", type=int, default=100)

    p_audit = sub.add_parser("audit", help="Inspect audit replay records")
    p_audit_sub = p_audit.add_subparsers(dest="audit_command")
    p_audit_recent = p_audit_sub.add_parser("recent")
    p_audit_recent.add_argument("--limit", type=int, default=20)
    p_audit_show = p_audit_sub.add_parser("show")
    p_audit_show.add_argument("event_id")

    p_memory = sub.add_parser("memory", help="Manage reusable memory")
    _configure_memory_parser(p_memory)

    sub.add_parser("memory-merge", help="Legacy alias for `memory merge`")
    p_mem_pin = sub.add_parser("memory-pin", help="Legacy alias for `memory pin`")
    p_mem_pin.add_argument("text")
    p_mem_pin.add_argument("--target", default="memory", choices=["memory", "user"])
    p_mem_forget = sub.add_parser("memory-forget", help="Legacy alias for `memory forget`")
    p_mem_forget.add_argument("text")
    p_mem_forget.add_argument("--target", default="memory", choices=["memory", "user"])
    p_contradiction = sub.add_parser(
        "contradiction-scan",
        help="Legacy alias for `memory contradictions`",
    )
    p_contradiction.add_argument(
        "--apply", action="store_true", help="Apply SUPERSEDE/ARCHIVE actions"
    )
    sub.add_parser("memory-reindex", help="Legacy alias for `memory reindex`")

    p_bootstrap = sub.add_parser("memory-bootstrap", help="Legacy alias for `memory bootstrap`")
    p_bootstrap.add_argument(
        "--dry-run", action="store_true", help="Show candidates without writing"
    )
    p_bootstrap.add_argument(
        "--max", type=int, default=5, help="Max entries to promote (default: 5)"
    )

    p_setup = sub.add_parser("setup", help=command_help("setup"))
    p_setup.add_argument("--force", action="store_true", help="Replace the config template")
    sub.add_parser("doctor", help=command_help("doctor"))

    p_backup = sub.add_parser("backup", help="Create or inspect a local recovery archive")
    p_backup.add_argument(
        "--output", default=None, help="Backup ZIP path (default: user backup dir)"
    )
    p_backup_sub = p_backup.add_subparsers(dest="backup_command")
    p_backup_create = p_backup_sub.add_parser("create", help=command_help("backup", "create"))
    p_backup_create.add_argument(
        "--output",
        dest="create_output",
        default=None,
        help="Backup ZIP path (default: user backup dir)",
    )
    p_backup_inspect = p_backup_sub.add_parser("inspect", help="Validate manifest and checksums")
    p_backup_inspect.add_argument("archive")

    p_restore = sub.add_parser("restore", help="Preview or apply a local recovery archive")
    p_restore.add_argument("archive")
    p_restore.add_argument(
        "--force",
        action="store_true",
        help="Apply merge restore after automatically backing up current state",
    )

    p_export = sub.add_parser("export", help="Create or inspect a private migration archive")
    p_export.add_argument(
        "--output", default=None, help="Portable ZIP path (default: user export dir)"
    )
    p_export_sub = p_export.add_subparsers(dest="export_command")
    p_export_create = p_export_sub.add_parser("create", help=command_help("export", "create"))
    p_export_create.add_argument(
        "--output",
        dest="create_output",
        default=None,
        help="Portable ZIP path (default: user export dir)",
    )
    p_export_inspect = p_export_sub.add_parser("inspect", help="Validate a portable archive")
    p_export_inspect.add_argument("archive")

    p_import = sub.add_parser("import", help="Preview or apply a private migration archive")
    p_import.add_argument("archive")
    p_import.add_argument(
        "--force",
        action="store_true",
        help="Apply merge import after automatically backing up current state",
    )

    p_serve = sub.add_parser("serve", help=command_help("serve"))
    p_serve.add_argument("--host", default="127.0.0.1", help="Loopback host only")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--dev", action="store_true", help="Dev mode: CORS for :3000 + hot reload")
    p_serve.add_argument("--open", action="store_true", help="Open browser after server starts")
    p_serve.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Do not start the scheduler (default: on when config.scheduler.enabled)",
    )

    p_compress = sub.add_parser(
        "compress", help="Manually compress a session's context (Phase 1+2)"
    )
    p_compress.add_argument("session_id", nargs="?", help="Session ID (default: most recent)")
    p_compress.add_argument("--focus", default=None, help="Focus topic for guided summarization")

    p_service = sub.add_parser(
        "service",
        help="Manage persistent serve (macOS launchd / Linux systemd)",
    )
    p_service_sub = p_service.add_subparsers(dest="service_command", required=True)
    for name, help_text in [
        ("install", "Install the user service (requires web build)"),
        ("uninstall", "Remove the user service"),
        ("start", "Start service"),
        ("stop", "Stop service"),
        ("restart", "Restart service"),
    ]:
        p_service_sub.add_parser(name, help=help_text)
    p_service_status = p_service_sub.add_parser("status", help="Show service + /health status")
    p_service_status.add_argument(
        "--json",
        dest="command_json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    p_service_verify = p_service_sub.add_parser(
        "verify",
        help="Strict read-only production readiness check",
    )
    p_service_verify.add_argument(
        "--json",
        dest="command_json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    p_service.add_argument("--port", type=int, default=None, help="API port (default: 19704)")
    p_service.add_argument("--host", default="127.0.0.1", help="Loopback host only")
    p_service.add_argument("--force", action="store_true", help="Replace the service definition")
    p_service.add_argument(
        "--json",
        dest="service_json",
        action="store_true",
        help="Emit JSON for status/verify (also accepted after the subcommand)",
    )
    p_commands = sub.add_parser("commands", help=command_help("commands"))
    p_commands.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    args = parser.parse_args()
    load_project_dotenv()
    setup_logging()

    if args.command == "market-pulse":
        run_market_pulse()
    elif args.command == "agent":
        run_agent(args.message)
    elif args.command == "ask":
        run_ask(args.message)
    elif args.command in {"data-check", "data"}:
        run_data_check(args.symbols, timeframe=args.timeframe, provider=args.provider)
    elif args.command == "run-job":
        run_job(args.job_id)
    elif args.command in {"scheduler", "jobs"}:
        if args.scheduler_command == "start":
            run_scheduler_start()
        elif args.scheduler_command == "runs":
            run_scheduler_runs(limit=args.limit)
        elif args.scheduler_command in {"run", "run-once"}:
            run_job(args.job_id)
        elif args.scheduler_command == "add":
            run_scheduler_add(
                args.name, args.prompt, args.schedule, trading_day_only=args.trading_day_only
            )
        elif args.scheduler_command == "pause":
            run_scheduler_pause(args.job_id)
        elif args.scheduler_command == "resume":
            run_scheduler_resume(args.job_id)
        elif args.scheduler_command == "remove":
            run_scheduler_remove(args.job_id)
        else:
            run_scheduler_list()
    elif args.command == "notifications":
        if args.notify_command == "test":
            run_notifications_test()
        else:
            run_notifications_recent(limit=getattr(args, "limit", 20))
    elif args.command == "rules":
        if args.rules_command == "show":
            run_rules_show()
        elif args.rules_command == "add":
            run_rules_add(args.text)
        elif args.rules_command == "edit":
            run_rules_edit(args.entry_id, args.text)
        elif args.rules_command == "remove":
            run_rules_remove(args.entry_id)
        else:
            run_rules_list()
    elif args.command == "evaluate":
        run_evaluate(limit=args.limit)
    elif args.command == "audit":
        if args.audit_command == "show":
            run_audit_show(args.event_id)
        else:
            run_audit_recent(limit=getattr(args, "limit", 20))
    elif args.command == "memory-merge":
        run_memory_merge()
    elif args.command == "memory-pin":
        run_memory_pin(args.text, target=args.target)
    elif args.command == "memory-forget":
        run_memory_forget(args.text, target=args.target)
    elif args.command == "contradiction-scan":
        run_contradiction_scan(apply=args.apply)
    elif args.command == "memory-reindex":
        run_memory_reindex()
    elif args.command == "memory-bootstrap":
        run_memory_bootstrap(dry_run=args.dry_run, max_promote=args.max)
    elif args.command == "memory":
        if args.memory_command == "merge":
            run_memory_merge()
        elif args.memory_command == "pin":
            run_memory_pin(args.text, target=args.target)
        elif args.memory_command == "forget":
            run_memory_forget(args.text, target=args.target)
        elif args.memory_command == "contradictions":
            run_contradiction_scan(apply=args.apply)
        elif args.memory_command == "reindex":
            run_memory_reindex()
        elif args.memory_command == "bootstrap":
            run_memory_bootstrap(dry_run=args.dry_run, max_promote=args.max)
    elif args.command == "setup":
        run_setup(force=args.force)
    elif args.command == "doctor":
        run_doctor()
    elif args.command == "backup":
        if args.backup_command == "inspect":
            run_backup_inspect(args.archive)
        else:
            run_backup(output=getattr(args, "create_output", None) or args.output)
    elif args.command == "restore":
        run_restore(args.archive, force=args.force)
    elif args.command == "export":
        if args.export_command == "inspect":
            run_export_inspect(args.archive)
        else:
            run_export(output=getattr(args, "create_output", None) or args.output)
    elif args.command == "import":
        run_import(args.archive, force=args.force)
    elif args.command == "serve":
        run_serve(
            args.host,
            _resolve_port(args.port),
            dev=args.dev,
            open_browser=args.open,
            no_scheduler=args.no_scheduler,
        )
    elif args.command == "compress":
        run_compress(args.session_id, focus=args.focus)
    elif args.command == "service":
        from trade_compass_agent.daemon.cli import run_service_command

        run_service_command(
            args.service_command,
            port=_resolve_port(args.port),
            host=args.host,
            force=args.force,
            as_json=args.service_json or getattr(args, "command_json", False),
        )
    elif args.command == "commands":
        run_commands(as_json=args.json)


if __name__ == "__main__":
    main()
