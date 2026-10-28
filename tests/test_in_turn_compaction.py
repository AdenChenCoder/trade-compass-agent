from trade_compass_agent.llm.providers import ChatMessage
from trade_compass_agent.runtime.compression.summarizer import (
    summarize_middle_turns,
    latest_persisted_summary,
)
from trade_compass_agent.runtime.compression.trim import _resolve_protect_tail_boundary


def test_native_public_token_counts_preserve_full_recent_source_quota():
    messages = [
        ChatMessage(role="tool", content="dense financial evidence " * 100) for _ in range(10)
    ]
    assert _resolve_protect_tail_boundary(messages, 0, 5, lambda _: 1) == 5


def test_in_progress_summary_keeps_exact_current_request_and_tool_pairs():
    messages = [
        ChatMessage(role="system", content="rules"),
        ChatMessage(role="user", content="old request"),
        ChatMessage(role="assistant", content="old reply"),
    ]
    current = ChatMessage(role="user", content="CURRENT_REQUEST: execute at most once")
    messages.extend([ChatMessage(role="assistant", content="older fact") for _ in range(5)])
    messages.append(current)
    for i in range(8):
        messages.extend(
            [
                ChatMessage(
                    role="assistant",
                    reasoning_content="PRIVATE",
                    tool_calls=[{"id": str(i), "function": {"name": "read", "arguments": "{}"}}],
                ),
                ChatMessage(role="tool", tool_call_id=str(i), name="read", content=f"source{i}"),
            ]
        )
    captured = []

    def summary(system, user):
        captured.append(user)
        return (
            "<summary>Previously executed operations and their receipts; do not repeat.</summary>"
        )

    compressed, _, count, _ = summarize_middle_turns(
        messages,
        llm_call=summary,
        protect_recent_count=2,
        protect_recent_tokens=0,
        message_token_counter=lambda _: 1,
    )
    assert count > 0 and compressed[-1] is messages[-1]
    assert sum(m is current for m in compressed) == 1
    assert "CURRENT_REQUEST" not in captured[0] and "PRIVATE" not in captured[0]
    assert compressed.index(current) < next(
        i for i, m in enumerate(compressed) if "上下文压缩" in (m.content or "")
    )
    assert latest_persisted_summary(compressed).startswith("Previously executed")


def test_in_progress_keeps_complete_loaded_skill_and_batch_receipts_outside_tail():
    current = ChatMessage(role="user", content="Follow the loaded rules and execute once")
    messages = [ChatMessage(role="system", content="rules"), current]
    protected = []
    for name in ["load_skill", "batch_paper_trades"]:
        pair = [
            ChatMessage(role="assistant", tool_calls=[{
                "id": name, "function": {"name": name, "arguments": "{}"},
            }]),
            ChatMessage(role="tool", tool_call_id=name, name=name,
                        content=name + " FULL_CONTENT " * 1000 + " EXACT_END"),
        ]
        protected.extend(pair)
        messages.extend(pair)
    messages.extend(ChatMessage(role="assistant", content=f"old fact {i}") for i in range(6))
    messages.append(ChatMessage(role="assistant", content="latest progress"))
    prompts = []

    def summary(system, user):
        prompts.append(user)
        return "<summary>Old facts summarized.</summary>"

    compressed, _, count, _ = summarize_middle_turns(
        messages, llm_call=summary, protect_head_count=0,
        protect_recent_count=1, protect_recent_tokens=0,
    )
    assert count == 6
    assert compressed[-1] is messages[-1]
    assert sum(m is current for m in compressed) == 1
    assert all(any(m is original for m in compressed) for original in protected)
    assert "FULL_CONTENT" not in "".join(prompts)
    for call, receipt in zip(protected[::2], protected[1::2]):
        assert compressed[compressed.index(call) + 1] is receipt
        assert receipt.content.endswith("EXACT_END")


def test_live_turn_compacts_after_tools_without_replaying_order_or_losing_history(
    tmp_path, monkeypatch
):
    import json
    from trade_compass_agent.config import AppConfig, AgentConfig, CompressionConfig, LLMConfig
    from trade_compass_agent.llm.lmstudio_context import ContextMeasurement
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall, _maintenance_mode
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    seen = []
    summaries = []

    class Client:
        name = "lmstudio"
        model = "qwen3.8-27b"
        context_window = 131072
        response_token_limit = 32768
        calls = 0

        def measure_input_tokens(self, messages, **kwargs):
            expanded = self.calls >= 4 and not latest_persisted_summary(messages)
            return ContextMeasurement(900 if expanded else 10, 131072, tuple(1 for _ in messages))

        def complete(self, messages):
            assert _maintenance_mode.get()
            summaries.append(messages)
            return ChatCompletion(
                content="<summary>Old market observations only; already executed actions remain completed.</summary>"
            )

        def stream_complete(self, messages, **kwargs):
            assert not _maintenance_mode.get()
            self.calls += 1
            if self.calls == 1:
                return ChatCompletion(
                    content=None,
                    reasoning_content="SECRET_REASONING",
                    tool_calls=[
                        ToolCall(
                            "buy",
                            "place_paper_trade",
                            '{"symbol":"000001","side":"buy","quantity":100}',
                        )
                    ],
                )
            if self.calls == 5:
                assert latest_persisted_summary(messages)
                assert any(m.role == "tool" and "exec-once" in m.content for m in messages)
                assert messages[-1].role == "tool" and "pulse3" in messages[-1].content
            if self.calls < 6:
                return ChatCompletion(
                    content=None,
                    reasoning_content="SECRET_REASONING",
                    tool_calls=[ToolCall(f"pulse{self.calls}", "get_market_pulse", "{}")],
                )
            return ChatCompletion(
                content="本轮已买入000001共100股，trade_id=exec-once。后续仅查询市场，无其他新增成交。"
            )

    client = Client()
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: client)
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)

    def execute(self, name, args):
        seen.append(name)
        if name == "place_paper_trade":
            return json.dumps(
                {
                    "status": "executed",
                    "trade_id": "exec-once",
                    "symbol": "000001",
                    "side": "buy",
                    "quantity": 100,
                    "price": 10,
                    "account": "short_stock",
                }
            )
        return json.dumps({"snapshot": f"pulse{seen.count('get_market_pulse')}"})

    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        llm=LLMConfig(provider="disabled"),
        agent=AgentConfig(learning_enabled=False, llm_session_titles=False),
        context_compression=CompressionConfig(
            context_budget=1000, protect_recent_count=2, protect_recent_tokens=2
        ),
    )
    agent = loop.AgentLoop.from_config(config)
    reply = agent.run_turn(
        "买入000001一百股，只执行一次，然后查询市场四次", session_id="scheduler-live-compaction"
    )
    assert "exec-once" in reply.summary and client.calls == 6 and len(summaries) == 1
    assert seen.count("place_paper_trade") == 1 and seen.count("get_market_pulse") == 4
    session = agent.session_store.load("scheduler-live-compaction")
    assert sum(m.role == "user" for m in session.messages) == 1
    assert latest_persisted_summary(
        [m.to_chat_message() for m in agent.session_store.load_context(session)]
    )
    assert all("SECRET_REASONING" not in p.read_text() for p in config.data_dir.rglob("*.jsonl"))
