from __future__ import annotations

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
from trade_compass_agent.runtime.session import derive_session_title


def suggest_session_title(message: str, config: AppConfig) -> str:
    """LLM-generated short title; falls back to truncation heuristic."""
    text = message.strip()
    if not text:
        return derive_session_title(message)

    try:
        client = create_chat_client(config)
        if getattr(client, "name", "") == "fallback":
            return derive_session_title(message)
        completion = client.complete(
            [
                ChatMessage(
                    role="user",
                    content=(
                        "用不超过 15 个中文字为下面的用户问题生成对话标题。"
                        "只输出标题本身，不要引号、标点或解释。\n\n"
                        f"{text[:500]}"
                    ),
                )
            ],
            tools=None,
        )
        title = (completion.content or "").strip().strip("\"'")
        if title:
            return title[:120]
    except Exception:
        pass
    return derive_session_title(message)
