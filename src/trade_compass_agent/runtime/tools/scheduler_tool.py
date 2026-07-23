"""Agent tool: schedule_task — create scheduled prompt jobs via chat."""

from __future__ import annotations

import json

SCHEDULE_TASK_SCHEMA = {
    "name": "schedule_task",
    "description": (
        "创建定时任务。系统会按指定时间用 AgentLoop 执行 prompt。"
        "示例调度: 'trading_day 14:30'（每个交易日14:30）, 'sat 10:00'（每周六10:00）"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "任务名称（简短描述）",
            },
            "prompt": {
                "type": "string",
                "description": "要定期执行的指令",
            },
            "schedule": {
                "type": "string",
                "description": "调度表达式: 'trading_day HH:MM' 或 'sat HH:MM' 或 'mon HH:MM' 等",
            },
            "trading_day_only": {
                "type": "boolean",
                "description": "是否仅交易日执行（默认 false）",
            },
            "delivery_channels": {
                "type": "array",
                "items": {"type": "string", "enum": ["web_log", "feishu", "wecom", "weixin"]},
                "description": "任务完成后的推送渠道，默认 web_log",
            },
        },
        "required": ["name", "prompt", "schedule"],
    },
}

LIST_SCHEDULED_TASKS_SCHEMA = {
    "name": "list_scheduled_tasks",
    "description": "列出所有用户创建的定时任务。",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

REMOVE_SCHEDULED_TASK_SCHEMA = {
    "name": "remove_scheduled_task",
    "description": "删除一个用户创建的定时任务。",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "要删除的任务 ID",
            },
        },
        "required": ["task_id"],
    },
}


def tool_schedule_task(
    config,
    *,
    name: str,
    prompt: str,
    schedule: str,
    trading_day_only: bool = False,
    delivery_channels: list[str] | None = None,
) -> str:
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore
    from trade_compass_agent.ops.tick_scheduler import reload_active_scheduler

    channels = tuple(delivery_channels) if delivery_channels else ("web_log",)
    store = PromptJobStore(config.data_dir / "scheduler.db")
    job = store.create(
        name=name,
        prompt=prompt,
        schedule=schedule,
        trading_day_only=trading_day_only,
        delivery_channels=channels,
        created_by="agent",
    )
    reload_active_scheduler()
    return json.dumps({
        "ok": True,
        "job_id": job.id,
        "message": f"已创建定时任务 '{name}'，调度: {schedule}",
    }, ensure_ascii=False)


def tool_list_scheduled_tasks(config) -> str:
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore

    store = PromptJobStore(config.data_dir / "scheduler.db")
    jobs = store.list_all()
    if not jobs:
        return json.dumps({"tasks": [], "message": "暂无自建定时任务"}, ensure_ascii=False)

    tasks = []
    for j in jobs:
        tasks.append({
            "id": j.id,
            "name": j.name,
            "schedule": j.schedule,
            "enabled": j.enabled,
            "trading_day_only": j.trading_day_only,
            "created_by": j.created_by,
            "prompt_preview": j.prompt[:100],
        })
    return json.dumps({"tasks": tasks}, ensure_ascii=False)


def tool_remove_scheduled_task(config, *, task_id: str) -> str:
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore
    from trade_compass_agent.ops.tick_scheduler import reload_active_scheduler

    store = PromptJobStore(config.data_dir / "scheduler.db")
    deleted = store.delete(task_id)
    if deleted:
        reload_active_scheduler()
        return json.dumps({"ok": True, "message": f"已删除定时任务 {task_id}"}, ensure_ascii=False)
    return json.dumps({"ok": False, "message": f"未找到任务 {task_id}"}, ensure_ascii=False)
