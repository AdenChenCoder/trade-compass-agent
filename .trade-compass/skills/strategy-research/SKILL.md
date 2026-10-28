---
name: strategy-research
description: 将投资笔记或交易经验转成有来源、明确规则与失效条件的策略假设，保存版本并运行可重放的探索性回测。用于策略研究，不处理普通行情问答。
metadata:
  version: "1.0"
---

# 策略研究

把收藏、文章和个人经验作为线索。区分作者原话、用户规则、你的推断与计算结果；未读到的正文、视频和收益记录标为未知。阅读 [市场与证据边界](references/market-context.md) 后，再决定哪些想法可验证。

先检查用户 Rules、适用市场、证券类型、数据时间范围和复权方式。用户明确约束优先；研究参数不会自动变成交易限制。

用 `save_strategy_research` 保存假设，写出 hypothesis、invalidation、实际读过的 sources 和影响分析的 knowledge_refs（Skill 名称）。当前唯一可执行方法 `trend_breakout`：

- 收盘价超过此前 breakout_window 根日线的最高价；当天不纳入箱体。
- fast_window 均线高于 slow_window 均线。
- 当日量不低于此前 volume_window 日均量乘 volume_ratio；当天不纳入基准均量。
- 跌破慢均线或快均线低于慢均线形成退出信号。止损、止盈与仓位来自该次回测配置，是实验参数。

这些默认窗口只是可复现实验起点，不是从收藏笔记验证出的最优参数。其他策略先保存明确的研究说明，不把它伪装成此方法。无法用当前 schema 准确表达时，先说明缺少哪种能力。

用 `run_strategy_backtest` 指定已保存的 ID、版本、标的与区间；用 `get_strategy_research` 复核实际记录。检查价格时点、来源、费用、未平仓与警告。示例行情只能说明流程可运行。US/HK 假设可记录，当前回测引擎只执行 CN，不能换股票代码套用。

报告需要包含：来源与假设、支持/反对证据、实际实验区间与费用、净收益和回撤、平仓数量及期末持仓、数据不足和未建模因素、下一步能推翻假设的检验。没有样本外、不同市场环境和前向模拟记录时，称为探索性结果。

研究不会下单。已有交易授权仍按自主模拟交易开关、用户指令和 Rules 执行；无需为研究另增权限。若随后产生相关交易信号，`emit_signal` 附 account、strategy_id、strategy_version 与可选 research_run_id，成交时明确传 signal_id。没有关联证据就留空。
