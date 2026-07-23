from pathlib import Path
import stat

from trade_compass_agent.channels.subscriber_store import (
    load_channel_subscribers,
    save_channel_subscribers,
)


def test_channel_subscriber_persistence(tmp_path: Path) -> None:
    path = tmp_path / "channel_subscribers.json"
    save_channel_subscribers(path, {"feishu": {"oc_test_chat"}, "weixin": set(), "wecom": set()})

    loaded = load_channel_subscribers(path)
    assert loaded["feishu"] == {"oc_test_chat"}

    loaded["feishu"].add("oc_second")
    save_channel_subscribers(path, loaded)
    reloaded = load_channel_subscribers(path)
    assert reloaded["feishu"] == {"oc_second", "oc_test_chat"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
