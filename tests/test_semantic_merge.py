"""Tests for semantic_merge module."""

from pathlib import Path

from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.semantic_merge import (
    _find_clusters,
    _should_run,
    merge_similar_entries,
)


class TestFindClusters:
    def test_groups_similar_entries(self):
        entries = [
            "涨停家数超过三十家可作为市场情绪偏强的参考指标",
            "涨停超过三十家可视为市场情绪偏强的信号",
            "sina批量报价接口比逐个获取快十倍以上",
        ]
        clusters = _find_clusters(entries)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2
        assert "sina" not in clusters[0][0] and "sina" not in clusters[0][1]

    def test_no_clusters_when_all_different(self):
        entries = [
            "涨停家数超过三十家可作为市场情绪偏强的参考指标",
            "sina批量报价接口比逐个获取快十倍以上",
            "贵州茅台的销售渠道以直销和经销商并重",
        ]
        clusters = _find_clusters(entries)
        assert len(clusters) == 0


class TestShouldRun:
    def test_runs_when_no_previous(self):
        assert _should_run({})

    def test_runs_after_cooldown(self):
        assert _should_run({"last_merge_at": "2020-01-01T00:00:00+00:00"})

    def test_skips_within_cooldown(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        assert not _should_run({"last_merge_at": now})


class TestMergeSimilarEntries:
    def test_skips_without_llm(self, tmp_path: Path):
        store = MemoryStore(tmp_path)
        for i in range(6):
            store.add(f"测试条目编号{i}，内容足够长以通过最小长度检查", target="memory")
        result = merge_similar_entries(store, llm_call=None, force=True)
        assert result == 0

    def test_skips_with_few_entries(self, tmp_path: Path):
        store = MemoryStore(tmp_path)
        store.add("涨停家数超过三十家可作为市场情绪偏强的参考指标", target="memory")
        store.add("sina批量报价接口比逐个获取快十倍以上", target="memory")

        def fake_llm(sys: str, user: str) -> str:
            return "merged"

        result = merge_similar_entries(store, llm_call=fake_llm, force=True)
        assert result == 0

    def test_merges_similar_cluster(self, tmp_path: Path):
        store = MemoryStore(tmp_path)
        store.add("涨停家数超过三十家可作为市场情绪偏强的参考指标", target="memory", source="curator", confidence=0.85)
        store.add("涨停超过三十家可视为市场情绪偏强的信号", target="memory", source="curator", confidence=0.85)
        store.add("sina批量报价接口比逐个获取快十倍以上", target="memory", source="curator", confidence=0.85)
        store.add("主力资金持续流入的板块值得重点关注", target="memory", source="curator", confidence=0.85)
        store.add("贵州茅台的核心竞争力在于品牌壁垒和定价权", target="memory", source="curator", confidence=0.85)

        merged_text = "涨停超三十家表明市场情绪偏强"

        def fake_llm(sys: str, user: str) -> str:
            return merged_text

        result = merge_similar_entries(store, llm_call=fake_llm, force=True)
        assert result == 1

        entries = store.memory_entries
        assert merged_text in entries
        assert len(entries) == 4

    def test_skips_low_trust_entries(self, tmp_path: Path):
        store = MemoryStore(tmp_path, min_inject_confidence=0.5)
        store.add("涨停家数超过三十家可作为市场情绪偏强的参考指标", target="memory", source="curator", confidence=0.85)
        store.add("涨停超过三十家可视为市场情绪偏强的信号", target="memory", source="curator", confidence=0.85)
        store.add("sina批量报价接口比逐个获取快十倍以上", target="memory", source="curator", confidence=0.85)
        store.add("主力资金持续流入的板块值得重点关注", target="memory", source="curator", confidence=0.85)
        store.add("贵州茅台的核心竞争力在于品牌壁垒和定价权", target="memory", source="curator", confidence=0.85)
        for row in store._meta["memory"]:
            row["source"] = "agent"
        store._save_meta()

        def fake_llm(sys: str, user: str) -> str:
            return "涨停超三十家表明市场情绪偏强"

        result = merge_similar_entries(store, llm_call=fake_llm, force=True)

        assert result == 0
        assert "涨停超三十家表明市场情绪偏强" not in store.memory_entries
