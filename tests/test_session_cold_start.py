from concurrent.futures import ThreadPoolExecutor
import json
import threading

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatCompletion
from trade_compass_agent.memory.observation_store import ObservationStore
from trade_compass_agent.memory.session_summary_store import SessionSummaryStore
from trade_compass_agent.runtime.market_stack import MarketStack


def test_fresh_shared_stores_initialize_concurrently_without_losing_records(tmp_path):
    for attempt in range(4):
        root = tmp_path / str(attempt)
        barrier = threading.Barrier(8)

        def write(index):
            barrier.wait(timeout=5)
            observations = ObservationStore(root / "observations.db")
            summaries = SessionSummaryStore(root / "sessions.db")
            assert observations.append(f"session-{index}", "fixture", f"Unique research record {index}")
            summaries.upsert(f"session-{index}", f"Private summary {index}", turn_count=1, tools_used=[])

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(8)))
        assert ObservationStore(root / "observations.db").count() == 8
        summaries = SessionSummaryStore(root / "sessions.db")
        for index in range(8):
            assert summaries.get(f"session-{index}").summary == f"Private summary {index}"


def test_first_concurrent_api_turns_keep_separate_history(client, tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    stack = MarketStack.from_config(config)
    monkeypatch.setattr("trade_compass_agent.web.agent_api._get_cached_stack", lambda: (config, stack))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.shared_consolidation_worker", lambda **kw: None)
    barrier = threading.Barrier(3)
    model_barrier = threading.Barrier(3)

    class Reply:
        def stream_complete(self, messages, **kw):
            model_barrier.wait(timeout=10)  # Initialization locks must not serialize inference.
            return ChatCompletion(content=messages[-1].content)

    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Reply())

    def turn(index):
        barrier.wait(timeout=5)
        for turn_index in range(5):
            response = client.post("/api/agent/turn", json={"session_id": f"cold-{index}",
                                  "message": f"PRIVATE_{index} / turn {turn_index}"})
            assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(turn, range(3)))
    for index, row in enumerate(rows):
        assert f"PRIVATE_{index}" in row["summary"]
        visible = client.get(f"/api/agent/sessions/cold-{index}/messages").json()
        assert len([m for m in visible["messages"] if m["role"] == "user"]) == 5
        assert len([m for m in visible["messages"] if m["role"] == "assistant"]) == 5
        for other in range(3):
            if other != index:
                assert f"PRIVATE_{other}" not in json.dumps(visible)
