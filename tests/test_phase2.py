"""Tests for Phase 2: Custom Jobs, Hooks, and Reflection."""

from pathlib import Path

from trade_compass_agent.ops.hooks import (
    HookContext,
    HookRegistry,
    create_default_registry,
)
from trade_compass_agent.ops.job_definition import JobDefinition
from trade_compass_agent.ops.prompt_jobs import PromptJobStore
from trade_compass_agent.ops.reflection import JobReflection


# ---------------------------------------------------------------------------
# PromptJobStore tests
# ---------------------------------------------------------------------------


class TestPromptJobStore:
    def test_create_and_get(self, tmp_path: Path):
        store = PromptJobStore(tmp_path / "test.db")
        job = store.create(name="test job", prompt="analyze AAPL", schedule="trading_day 14:30")
        assert job.id
        assert job.name == "test job"
        assert job.enabled is True

        fetched = store.get(job.id)
        assert fetched is not None
        assert fetched.name == "test job"
        assert fetched.prompt == "analyze AAPL"

    def test_list_and_filter(self, tmp_path: Path):
        store = PromptJobStore(tmp_path / "test.db")
        j1 = store.create(name="job1", prompt="p1", schedule="trading_day 09:00")
        j2 = store.create(name="job2", prompt="p2", schedule="sat 10:00")
        store.set_enabled(j2.id, False)

        all_jobs = store.list_all()
        assert len(all_jobs) == 2

        enabled = store.list_enabled()
        assert len(enabled) == 1
        assert enabled[0].id == j1.id

    def test_update(self, tmp_path: Path):
        store = PromptJobStore(tmp_path / "test.db")
        job = store.create(name="orig", prompt="p", schedule="trading_day 10:00")
        updated = store.update(job.id, name="renamed", prompt="new prompt")
        assert updated is not None
        assert updated.name == "renamed"
        assert updated.prompt == "new prompt"

    def test_delete(self, tmp_path: Path):
        store = PromptJobStore(tmp_path / "test.db")
        job = store.create(name="to_delete", prompt="p", schedule="sat 09:00")
        assert store.delete(job.id) is True
        assert store.get(job.id) is None
        assert store.delete("nonexistent") is False

    def test_created_by(self, tmp_path: Path):
        store = PromptJobStore(tmp_path / "test.db")
        j1 = store.create(name="a", prompt="p", schedule="trading_day 09:00", created_by="agent")
        j2 = store.create(name="b", prompt="p", schedule="trading_day 09:00", created_by="cli")
        assert store.get(j1.id).created_by == "agent"
        assert store.get(j2.id).created_by == "cli"

    def test_trading_day_only(self, tmp_path: Path):
        store = PromptJobStore(tmp_path / "test.db")
        job = store.create(name="td", prompt="p", schedule="trading_day 09:00", trading_day_only=True)
        fetched = store.get(job.id)
        assert fetched.trading_day_only is True


# ---------------------------------------------------------------------------
# HookRegistry tests
# ---------------------------------------------------------------------------


class TestHookRegistry:
    def test_register_and_fire(self):
        registry = HookRegistry()
        called = []
        registry.register("test_hook", "pre_job", lambda ctx: called.append(ctx.phase))

        dummy_job = JobDefinition(id="test", name="Test", description="", schedule="trading_day 09:00")
        from trade_compass_agent.ops.run_store import RunRecord
        run = RunRecord(id="r1", job_id="test", trigger="test", status="running")

        ctx = HookContext(job=dummy_job, run=run, phase="pre_job")
        results = registry.fire(ctx)

        assert len(called) == 1
        assert called[0] == "pre_job"
        assert "test_hook" in results

    def test_priority_ordering(self):
        registry = HookRegistry()
        order = []
        registry.register("low", "post_job", lambda ctx: order.append("low"), priority=200)
        registry.register("high", "post_job", lambda ctx: order.append("high"), priority=10)

        dummy_job = JobDefinition(id="test", name="Test", description="", schedule="trading_day 09:00")
        from trade_compass_agent.ops.run_store import RunRecord
        run = RunRecord(id="r1", job_id="test", trigger="test", status="completed")
        ctx = HookContext(job=dummy_job, run=run, phase="post_job")
        registry.fire(ctx)

        assert order == ["high", "low"]

    def test_hook_error_doesnt_propagate(self):
        registry = HookRegistry()
        registry.register("bad", "pre_job", lambda ctx: 1 / 0)

        dummy_job = JobDefinition(id="test", name="Test", description="", schedule="trading_day 09:00")
        from trade_compass_agent.ops.run_store import RunRecord
        run = RunRecord(id="r1", job_id="test", trigger="test", status="running")
        ctx = HookContext(job=dummy_job, run=run, phase="pre_job")
        results = registry.fire(ctx)
        assert "bad(ERROR)" in results

    def test_default_registry_has_builtins(self):
        registry = create_default_registry()
        assert len(registry.hooks_for("post_job")) >= 1
        assert len(registry.hooks_for("on_failure")) >= 1

    def test_step_hooks_receive_step_id(self):
        registry = HookRegistry()
        captured = []
        registry.register("step_spy", "pre_step", lambda ctx: captured.append(("pre", ctx.step_id)))
        registry.register("step_spy", "post_step", lambda ctx: captured.append(("post", ctx.step_id)))

        dummy_job = JobDefinition(id="test", name="Test", description="", schedule="trading_day 09:00")
        from trade_compass_agent.ops.run_store import RunRecord
        run = RunRecord(id="r1", job_id="test", trigger="test", status="running")

        registry.fire(HookContext(job=dummy_job, run=run, phase="pre_step", step_id="s1"))
        registry.fire(HookContext(job=dummy_job, run=run, phase="post_step", step_id="s1"))

        assert captured == [("pre", "s1"), ("post", "s1")]


# ---------------------------------------------------------------------------
# JobReflection tests
# ---------------------------------------------------------------------------


class TestJobReflection:
    def test_store_and_resolve(self, tmp_path: Path):
        reflection = JobReflection(tmp_path)
        reflection.store_pending("premarket", "run-1", predictions={"signal": "buy AAPL"}, summary="盘前分析完成")

        assert reflection.pending_count("premarket") == 1

        resolved = reflection.resolve_pending("premarket")
        assert len(resolved) == 1
        assert resolved[0].run_id == "run-1"
        assert reflection.pending_count("premarket") == 0

    def test_custom_resolve_fn(self, tmp_path: Path):
        reflection = JobReflection(tmp_path)
        reflection.store_pending("close", "run-a", predictions={"price_target": 150})

        def my_resolver(pending):
            return {"actual_price": 152}, "预测偏差 +1.3%"

        resolved = reflection.resolve_pending("close", resolve_fn=my_resolver)
        assert len(resolved) == 1
        assert resolved[0].actuals["actual_price"] == 152
        assert "偏差" in resolved[0].lesson

    def test_get_context(self, tmp_path: Path):
        reflection = JobReflection(tmp_path)
        reflection.store_pending("eod_review", "r1", predictions={"win_rate": 0.6}, summary="复盘完成")
        reflection.resolve_pending("eod_review")

        ctx = reflection.get_context("eod_review")
        assert ctx
        assert "复盘完成" in ctx

    def test_empty_context(self, tmp_path: Path):
        reflection = JobReflection(tmp_path)
        ctx = reflection.get_context("nonexistent")
        assert ctx == ""

    def test_clear(self, tmp_path: Path):
        reflection = JobReflection(tmp_path)
        reflection.store_pending("weekly", "r1", summary="s1")
        reflection.store_pending("weekly", "r2", summary="s2")
        assert reflection.pending_count("weekly") == 2
        reflection.clear("weekly")
        assert reflection.pending_count("weekly") == 0

    def test_partial_resolve(self, tmp_path: Path):
        reflection = JobReflection(tmp_path)
        reflection.store_pending("premarket", "r1", summary="first")
        reflection.store_pending("premarket", "r2", summary="second")

        def only_first(pending):
            if pending.run_id == "r1":
                return {}, "resolved first"
            return None  # skip r2

        resolved = reflection.resolve_pending("premarket", resolve_fn=only_first)
        assert len(resolved) == 1
        assert resolved[0].run_id == "r1"
        assert reflection.pending_count("premarket") == 1
