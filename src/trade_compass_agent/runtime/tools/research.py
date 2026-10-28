"""Small research tool surface, sharing all validation with the Web API."""

import json

from trade_compass_agent.evaluation.research import (
    ResearchRequest, ResearchStore, StrategyDefinition, run_strategy_research,
)


def _schema(name: str, description: str, model=None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": model.model_json_schema() if model else {
            "type": "object", "properties": {"run_id": {"type": "string"}},
            "additionalProperties": False,
        },
    }}


RESEARCH_TOOL_SCHEMAS = [
    _schema("save_strategy_research", "保存策略假设的新版本，不改变交易权限或人类规则。当前支持趋势箱体突破规则；sources 仅记录实际读过的来源。", StrategyDefinition),
    _schema("run_strategy_backtest", "用完整日线运行探索性回测，保存行情快照、费用、规则版本和结果。不下单；示例数据不能证明收益。仅支持 A 股。", ResearchRequest),
    _schema("get_strategy_research", "查看策略版本和研究记录；传 run_id 可读取完整快照。回测结果不代表已验证盈利能力。"),
]


def tool_strategy_research(stack, name: str, arguments: dict) -> str:
    store = ResearchStore(stack.config.data_dir)
    if name == "save_strategy_research":
        result = store.save_strategy(StrategyDefinition.model_validate(arguments))
    elif name == "run_strategy_backtest":
        result = run_strategy_research(stack, ResearchRequest.model_validate(arguments))
        result = {key: value for key, value in result.items() if key not in {"dataset", "signals", "knowledge_snapshots"}}
    elif name == "get_strategy_research":
        result = store.get_run(arguments["run_id"]) if arguments.get("run_id") else {
            "strategies": store.list_strategies(), "runs": store.list_runs(),
        }
    else:
        raise ValueError(f"Unknown research tool: {name}")
    return json.dumps(result, ensure_ascii=False, allow_nan=False)
