"""Mirror of apps/web/src/lib/turn-outcome.ts state machine rules."""

from __future__ import annotations


def has_assistant_payload(summary: str | None, sections: list | None) -> bool:
    text = (summary or "").strip()
    return bool(text) or bool(sections)


def should_show_global_failure(
    *,
    succeeded: bool,
    assistant_delivered: bool,
    reason: str,
    ok: bool | None = None,
    summary: str | None = None,
    sections: list | None = None,
) -> bool:
    if succeeded or assistant_delivered:
        return False
    if reason == "done":
        return ok is False and not has_assistant_payload(summary, sections)
    return True


def test_no_global_failure_when_assistant_already_delivered() -> None:
    assert (
        should_show_global_failure(
            succeeded=True,
            assistant_delivered=True,
            reason="http",
        )
        is False
    )


def test_global_failure_on_http_when_no_assistant() -> None:
    assert (
        should_show_global_failure(
            succeeded=False,
            assistant_delivered=False,
            reason="http",
        )
        is True
    )


def test_no_global_failure_on_done_ok_false_with_summary() -> None:
    assert (
        should_show_global_failure(
            succeeded=False,
            assistant_delivered=False,
            reason="done",
            ok=False,
            summary="已完成分析。",
            sections=[],
        )
        is False
    )


def test_global_failure_on_done_ok_false_without_summary() -> None:
    assert (
        should_show_global_failure(
            succeeded=False,
            assistant_delivered=False,
            reason="done",
            ok=False,
            summary="",
            sections=[],
        )
        is True
    )
