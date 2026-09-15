#!/usr/bin/env python3
"""Preview the lifecycle migration; --apply commits with a complete vault backup.

Stop old application writers before --apply. This command never calls a model or
changes rules, trading accounts, the ledger, or the autonomous-trading switch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from trade_compass_agent.memory.memory_store import MemoryStore, _content_hash
from trade_compass_agent.memory.skill_store import SkillStore


def copy_vault(source: Path, destination: Path):
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("Backup directory must be outside the memory vault")
    shutil.copytree(source, destination)
    # A raw copy of a WAL-backed database isn't a verified recovery point.
    for database in source.rglob("*"):
        if database.is_file() and database.suffix in {".db", ".sqlite", ".sqlite3"}:
            with database.open("rb") as header:
                if header.read(16) != b"SQLite format 3\x00":
                    continue
            try:
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as reader, sqlite3.connect(destination / database.relative_to(source)) as writer:
                    reader.backup(writer)
                    if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError(f"Backup verification failed: {database.name}")
            except sqlite3.DatabaseError as exc:
                raise ValueError(f"SQLite backup failed: {database.name}") from exc


def recover_low_trust_provenance(memory: MemoryStore, sessions: Path | None):
    if not sessions or not sessions.is_dir():
        return []
    targets = {_content_hash(m.text) for m in memory.get_entries_with_meta() if m.source == "reconciled"}
    receipts = {}
    for path in sorted(sessions.glob("*.jsonl")):
        calls = {}
        for line_no, line in enumerate(path.open(encoding="utf-8"), 1):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            for call in row.get("tool_calls") or []:
                fn = call.get("function", call)
                if fn.get("name") != "write_knowledge":
                    continue
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        continue
                text = str(args.get("content", ""))
                if args.get("action") == "add" and _content_hash(text) in targets:
                    calls[call.get("id")] = text
            text = calls.get(row.get("tool_call_id")) if row.get("role") == "tool" else None
            if not text:
                continue
            try:
                result = json.loads(row.get("content", "{}"))
            except ValueError:
                continue
            if result.get("ok") is True and result.get("source") in {"agent", "scheduler", "background_review", "dreaming"} and isinstance(result.get("confidence"), (int, float)) and result["confidence"] < .5:
                receipts[_content_hash(text)] = {"source": result["source"], "confidence": result["confidence"],
                    "evidence": f"session:{path.stem}:line:{line_no}"}
    repaired = []
    with memory._transaction():
        for row in memory._meta["memory"]:
            receipt = receipts.get(row["content_hash"])
            if row["source"] != "reconciled" or not receipt:
                continue
            memory._remember(row, "memory", "provenance_recovered", row["entry_id"])
            row.update(source=receipt["source"], confidence=min(row["confidence"], receipt["confidence"]), version=row["version"] + 1)
            row["evidence"] = list(dict.fromkeys(row.get("evidence", []) + [receipt["evidence"]]))
            if row["status"] == "candidate":
                row["reason"] = "awaiting_evidence"
            repaired.append(row["entry_id"])
        if repaired:
            memory._save_meta()
    return repaired


def recover_capacity_rejections(memory: MemoryStore, sessions: Path | None, date: str | None):
    """Recover exact rejected drafts, never replay mutations or revive old history."""
    if not date or not sessions or not sessions.is_dir():
        return []
    datetime.strptime(date, "%Y-%m-%d")
    recovered = []
    for path in sorted(sessions.glob(f"scheduler-*-{date}.jsonl")):
        calls = {}
        with path.open(encoding="utf-8") as rows:
            for line_no, line in enumerate(rows, 1):
                try:
                    row = json.loads(line)
                    for call in row.get("tool_calls") or []:
                        fn = call.get("function", call)
                        if fn.get("name") != "write_knowledge":
                            continue
                        args = fn.get("arguments", {})
                        args = json.loads(args) if isinstance(args, str) else args
                        if args.get("action") == "add" and args.get("target", "memory") == "memory":
                            calls[call.get("id")] = str(args.get("content", "")).strip()
                    content = calls.get(row.get("tool_call_id")) if row.get("role") == "tool" else None
                    if not content:
                        continue
                    result = json.loads(row.get("content", "{}"))
                except (ValueError, TypeError):
                    continue
                if result.get("ok") is not False or not str(result.get("error", "")).startswith("Would exceed memory limit"):
                    continue
                with memory._transaction():
                    if any(m.content_hash == _content_hash(content) for m in memory.get_entries_with_meta(include_history=True)):
                        continue
                    receipt = memory.add(content, source="scheduler", confidence=.4, meta_extra={
                        "reason": "recovered_capacity_rejection",
                        "evidence": [f"session:{path.stem}:line:{line_no}", result["error"]],
                    })
                    if not receipt["ok"]:
                        raise ValueError(f"Draft recovery failed: {path.name}:{line_no}: {receipt['error']}")
                    recovered.append(receipt["entry_id"])
    return recovered


def reopen_stale_history(memory: MemoryStore, evidence_path: Path | None):
    """An explicit, audited allowlist gives old decay retirements another review."""
    if evidence_path is None:
        return []
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    selected = {key: value for key, value in evidence.items() if value.get("stale")}
    receipts = []
    with memory._transaction():
        rows = {m.entry_id: m for m in memory.get_entries_with_meta()}
        pending = []
        for key, proof in selected.items():
            row = rows.get(key)
            marker = f"reopened_stale:{key}"
            if row is None or row.text != proof.get("text"):
                # A reviewed revision may legitimately have different text on replay.
                if row and marker in row.evidence:
                    continue
                raise ValueError(f"Audited memory no longer matches: {key}")
            if marker in row.evidence:
                continue
            if row.status != "archived" or row.reason != "legacy_archived" or row.source == "user_pin" or row.successor_id:
                raise ValueError(f"Not an eligible legacy retirement: {key}")
            if not all("Archived stale memory" in log.get("text", "") and log.get("path") and log.get("line")
                       and row.text[:30] in log["text"] for log in proof["stale"]):
                raise ValueError(f"Invalid stale-retirement evidence: {key}")
            pending.append((row, proof, marker))
        # Validate the whole allowlist before changing any record.
        for row, proof, marker in pending:
            result = memory.reopen_for_review(entry_id=row.entry_id, expected_version=row.version,
                reason="用户请求重新复评：服务停摆期间的未访问衰减不能证明记忆失效。",
                evidence=[marker] + [f"{log['path']}:{log['line']}: {log['text']}" for log in proof["stale"]])
            if not result.get("ok"):
                raise ValueError(result["error"])
            receipts.append(result)
    return receipts


def migrate(root: Path, sessions: Path | None, recover_failed_date: str | None = None, reopen_stale_evidence: Path | None = None):
    memory = MemoryStore(root)
    recovered = recover_low_trust_provenance(memory, sessions)
    recovered_drafts = recover_capacity_rejections(memory, sessions, recover_failed_date)
    reopened = reopen_stale_history(memory, reopen_stale_evidence)
    skills = SkillStore(root / "skills")
    records = memory.get_entries_with_meta(include_history=True)
    return {"capacity": memory.capacity(), "provenance_repaired_ids": recovered,
            "reopened_stale": reopened,
            "recovered_draft_ids": recovered_drafts,
            "records": len(records), "texts_hash": hashlib.sha256(json.dumps(sorted(m.text for m in records), ensure_ascii=False).encode()).hexdigest(),
            "skill_count": len(skills.list_skills(True)),
            "skill_validation_failures": [{"name": s.name, "errors": s.quality.hard_errors} for s in skills.list_skills(True) if s.source == "memory_vault" and s.quality.hard_errors]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-dir", type=Path, required=True)
    parser.add_argument("--sessions-dir", type=Path)
    parser.add_argument("--recover-failed-date", help="Recover capacity-rejected scheduler drafts for YYYY-MM-DD as candidates")
    parser.add_argument("--reopen-stale-evidence", type=Path, help="Audited ID/text/log allowlist for user-requested reassessment of old time-decay retirements")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply:
        backup = args.backup_dir or args.memory_dir.parent / f"memory-before-lifecycle-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        copy_vault(args.memory_dir, backup)
        report = migrate(args.memory_dir, args.sessions_dir, args.recover_failed_date, args.reopen_stale_evidence)
        report.update(applied=True, backup_dir=str(backup))
        (backup.parent / f"{backup.name}-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix="memory-lifecycle-preview-") as temporary:
            root = Path(temporary) / "vault"
            copy_vault(args.memory_dir, root)
            report = {**migrate(root, args.sessions_dir, args.recover_failed_date, args.reopen_stale_evidence), "applied": False}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
