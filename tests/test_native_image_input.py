from __future__ import annotations

import base64
import io
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from PIL import Image

import test_native_agents
from trade_compass_agent.llm.native_images import image_inputs, save_image
from trade_compass_agent.llm.native_session import NativeConversation, fingerprint
from trade_compass_agent.llm.providers import ChatMessage, LLMExecutionCancelled, llm_request_deadline
from trade_compass_agent.runtime.compression.summarizer import summarize_middle_turns
from trade_compass_agent.runtime.exceptions import AttachmentInputError
from trade_compass_agent.runtime.loop import AgentLoop
from trade_compass_agent.runtime.tools import chart_pattern


@pytest.fixture
def native_config(tmp_path):
    return test_native_agents.native_config.__wrapped__(tmp_path)


def encoded_image(color="red"):
    out = io.BytesIO()
    Image.new("RGB", (24, 18), color).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def frames(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_uploaded_images_reach_native_turn_and_survive_restart(native_config, monkeypatch, client):
    from trade_compass_agent.web import agent_api

    protocol = native_config.data_dir.parent / "images.jsonl"
    monkeypatch.setenv("FAKE_PROTOCOL", str(protocol))
    monkeypatch.setenv("FAKE_SKIP_TOOLS", "1")
    monkeypatch.setattr(agent_api, "load_app_config", lambda: native_config)
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    images = [encoded_image("red"), encoded_image("blue")]
    first = client.post("/api/agent/turn", json={"message": "比较两张图", "attachments": [
        {"type": "image", "content": value, "mime": "image/png"} for value in images]})
    assert first.status_code == 200
    sid = first.json()["session_id"]
    loop = AgentLoop.from_config(native_config)
    session = loop.session_store.load(sid)
    paths = session.messages[0].image_paths
    assert len(set(paths)) == 2
    first_turn = next(f["params"] for f in frames(protocol) if f.get("method") == "turn/start")
    assert [v["path"] for v in first_turn["input"] if v["type"] == "localImage"] == paths
    from pathlib import Path
    assert [base64.b64encode(Path(p).read_bytes()).decode() for p in paths] == images
    assert all(Path(p).is_relative_to(native_config.data_dir / "uploads") for p in paths)
    assert all(Path(p).stat().st_mode & 0o777 == 0o600 for p in paths)

    # Ordinary resume retains native images without resending old attachments.
    second = client.post("/api/agent/turn", json={"session_id": sid, "message": "继续比较，勿执行其他操作"})
    assert second.status_code == 200
    turns = [f["params"] for f in frames(protocol) if f.get("method") == "turn/start"]
    assert turns[0]["threadId"] == turns[1]["threadId"]
    assert all(v["type"] == "text" for v in turns[1]["input"])

    # Lost derived native state rebuilds visual input from application records.
    cursor = NativeConversation(native_config.data_dir, sid, loop.session_store.load(sid).messages)
    cursor.path.write_text("{}")
    third = client.post("/api/agent/turn", json={"session_id": sid, "message": "重新核对原图"})
    assert third.status_code == 200
    turns = [f["params"] for f in frames(protocol) if f.get("method") == "turn/start"]
    assert turns[0]["threadId"] != turns[-1]["threadId"]
    assert [v["path"] for v in turns[-1]["input"] if v["type"] == "localImage"] == paths
    assert loop.session_store.load(sid).messages[0].image_paths == paths
    page = client.get(f"/api/agent/sessions/{sid}/messages").json()
    assert page["messages"][0]["content"].startswith("比较两张图") and "[附件·图片 2]" in page["messages"][0]["content"]


@pytest.mark.parametrize("value", ["not base64!", "", base64.b64encode(b"not an image").decode()])
def test_invalid_upload_is_visible_without_model_submission(native_config, monkeypatch, client, value):
    from trade_compass_agent.web import agent_api

    protocol = native_config.data_dir.parent / "invalid.jsonl"
    monkeypatch.setenv("FAKE_PROTOCOL", str(protocol))
    monkeypatch.setattr(agent_api, "load_app_config", lambda: native_config)
    sid = client.post("/api/agent/sessions").json()["session_id"]
    result = client.post("/api/agent/turn", json={"session_id": sid, "message": "看图", "attachments": [
        {"type": "image", "content": value, "mime": "image/png"}]})
    assert result.status_code == 422 and "图片" in result.json()["detail"]
    assert not protocol.exists()
    history = client.get(f"/api/agent/sessions/{sid}/messages").json()
    assert len(history["messages"]) == 2 and "图片" in history["messages"][-1]["content"]
    assert not history["has_active_turn"]
    with client.stream("GET", f"/api/agent/stream?session_id={sid}") as stream:
        events = [line for line in stream.iter_lines() if line.startswith("event:")]
    assert events[-1] == "event: error" and "event: done" not in events


def test_image_assets_are_immutable_and_confined(native_config, tmp_path):
    path = save_image(encoded_image(), None, native_config)
    assert save_image(encoded_image(), None, native_config) == path
    assert image_inputs([ChatMessage(role="user", image_paths=[path])], native_config)[-1]["path"] == path
    submitted = image_inputs([ChatMessage(role="system", content="rules"), ChatMessage(role="user", image_paths=[path])], native_config)
    assert "第1条消息" in submitted[0]["text"]
    outside = tmp_path / "private.png"
    outside.write_bytes(base64.b64decode(encoded_image()))
    with pytest.raises(AttachmentInputError):
        image_inputs([ChatMessage(role="user", image_paths=[str(outside)])], native_config)
    from pathlib import Path
    Path(path).write_bytes(base64.b64decode(encoded_image("blue")))
    with pytest.raises(AttachmentInputError, match="发生变化"):
        image_inputs([ChatMessage(role="user", image_paths=[path])], native_config)
    Path(path).unlink()
    with pytest.raises(AttachmentInputError, match="不可用"):
        image_inputs([ChatMessage(role="user", image_paths=[path])], native_config)
    assert save_image(encoded_image(), None, native_config) == path


def test_public_image_url_is_snapshotted_with_existing_fetch_policy(native_config, monkeypatch):
    raw = base64.b64decode(encoded_image())
    with monkeypatch.context() as patch:
        patch.setattr("trade_compass_agent.runtime.tools.fetch_url._fetch_with_redirects",
                      lambda url, **kw: SimpleNamespace(status=200, truncated=False, body=raw))
        path = save_image(None, "https://example.com/chart.png", native_config)
    assert image_inputs([ChatMessage(role="user", image_paths=[path])], native_config)
    with pytest.raises(AttachmentInputError, match="直接上传"):
        save_image(None, "http://127.0.0.1/private.png", native_config)


def test_image_preparation_honors_parent_stop_before_writing(native_config):
    import time
    with llm_request_deadline(time.monotonic() + 10, is_cancelled=lambda: True):
        with pytest.raises(LLMExecutionCancelled):
            save_image(encoded_image(), None, native_config)
    assert not (native_config.data_dir / "uploads").exists()


def test_stopped_download_cannot_publish_an_asset_later(native_config, monkeypatch):
    import threading
    import time

    entered, release, finished, cancel = (threading.Event() for _ in range(4))
    raw = base64.b64decode(encoded_image())

    def download(*a, **kw):
        entered.set()
        release.wait(2)
        finished.set()
        return SimpleNamespace(status=200, truncated=False, body=raw)

    def stop():
        assert entered.wait(2)
        cancel.set()

    monkeypatch.setattr("trade_compass_agent.runtime.tools.fetch_url._fetch_with_redirects", download)
    watcher = threading.Thread(target=stop, daemon=True)
    watcher.start()
    try:
        with llm_request_deadline(time.monotonic() + 3, is_cancelled=cancel.is_set):
            with pytest.raises(LLMExecutionCancelled):
                save_image(None, "https://example.com/image.png", native_config)
        assert not (native_config.data_dir / "uploads").exists()
    finally:
        release.set()
        watcher.join(1)
        assert finished.wait(1)
    assert not (native_config.data_dir / "uploads").exists()


def test_compaction_preserves_original_visual_requests():
    image = ChatMessage(role="user", content="比较原图中两项费用", image_paths=["original.png", "second.png"])
    messages = [ChatMessage(role="system", content="rules"), ChatMessage(role="user", content="start"),
                ChatMessage(role="assistant", content="ok"), image,
                *[ChatMessage(role="assistant", content="已处理的历史" * 500) for _ in range(8)],
                ChatMessage(role="user", content="现在只读核对")]
    compressed, _, count, _ = summarize_middle_turns(messages,
        llm_call=lambda *a: "<summary>历史已完成，不重放。</summary>",
        protect_recent_count=1, protect_recent_tokens=0)
    assert count > 0 and compressed[-1] == messages[-1]
    assert [m for m in compressed if m.image_paths] == [image]
    again = [*compressed[:-1], *[ChatMessage(role="assistant", content="其他已处理历史" * 500) for _ in range(8)], messages[-1]]
    repeated, _, repeated_count, _ = summarize_middle_turns(again,
        llm_call=lambda *a: "<summary>增量历史已完成。</summary>", protect_recent_count=1, protect_recent_tokens=0)
    assert repeated_count > 0 and [m for m in repeated if m.image_paths] == [image]


def test_empty_image_field_keeps_pre_image_cursor_compatible(native_config):
    history = [ChatMessage(role="user", content="existing")]
    records = [asdict(m) for m in history]
    for row in records:
        row.pop("image_paths")
    cursor = NativeConversation(native_config.data_dir, "legacy", history)
    cursor.path.write_text(json.dumps({"status": "ready", "thread_id": "original", "profile": "p",
        "history": fingerprint(records), "context": fingerprint(records)}))
    cursor = NativeConversation(native_config.data_dir, "legacy", history)
    assert cursor.resume_id("p") == "original"


def test_native_chart_tool_sends_real_chart_image(native_config, monkeypatch):
    seen = []
    client = SimpleNamespace(analyze_images=lambda images, prompt: seen.append((images, prompt)) or SimpleNamespace(content="图像结果"))
    bars = [SimpleNamespace(open=10, high=11, low=9, close=10, volume=100) for _ in range(80)]
    stack = SimpleNamespace(config=native_config, provider=SimpleNamespace(get_bars=lambda *a, **k: bars))
    monkeypatch.setattr(chart_pattern, "create_vision_client", lambda cfg: client)
    monkeypatch.setattr(chart_pattern, "render_kline_chart", lambda *a, **k: encoded_image())
    result = chart_pattern.analyze_chart_pattern(stack, "600519")
    assert "图像结果" in result and seen[0][0] == ["data:image/png;base64," + encoded_image()]


def test_explicit_text_only_native_model_is_rejected(native_config, monkeypatch):
    from trade_compass_agent.llm.native import NativeAgentClient
    from trade_compass_agent.runtime.exceptions import AgentUnavailableError

    monkeypatch.setenv("FAKE_IMAGE_MODEL", "text-only")
    monkeypatch.setenv("FAKE_IMAGE_MODALITIES", "text")
    config = replace(native_config, llm=replace(native_config.llm, model="text-only"))
    with pytest.raises(AgentUnavailableError, match="不支持图片"):
        NativeAgentClient(config).analyze_images(["data:image/png;base64," + encoded_image()], "看图")


def test_image_does_not_authorize_model_requested_trade(native_config, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL", "place_paper_trade")
    monkeypatch.setenv("FAKE_ARGUMENTS", json.dumps({"symbol": "600519", "side": "buy", "quantity": 100,
                                                   "user_instruction": "立即买入"}))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    loop = AgentLoop.from_config(native_config)
    result = loop.run_turn("只读取这张图，不执行交易", attachments=[{"type": "image", "content": encoded_image()}])
    session = loop.session_store.load(result.session_id)
    receipt = json.loads(next(m.content for m in session.messages if m.role == "tool"))
    assert receipt["error"] and "invalid_user_instruction" in json.dumps(receipt)
    assert not (native_config.data_dir / "paper_trades.jsonl").exists()


def test_native_chat_images_do_not_require_auxiliary_vision(native_config, monkeypatch):
    config = replace(native_config, llm=replace(native_config.llm,
        vision_model="qwen3.8-27b", vision_provider="lmstudio"))
    monkeypatch.setenv("FAKE_SKIP_TOOLS", "1")
    monkeypatch.setattr("trade_compass_agent.runtime.intake.create_vision_client",
                        lambda *a: pytest.fail("Direct Codex inputs must not depend on auxiliary vision"))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    loop = AgentLoop.from_config(config)
    result = loop.run_turn("看图，不调用工具", attachments=[{"type": "image", "content": encoded_image()}])
    assert loop.session_store.load(result.session_id).messages[0].image_paths
    assert config.llm.vision_provider == "lmstudio" and config.llm.vision_model == "qwen3.8-27b"
