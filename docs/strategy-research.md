# 策略研究与 Agent 知识

收藏笔记的用途是发现可检验的想法。当前仅完成抽样阅读，没有全量转写所有收藏与视频，也没有证明某个方法能持续盈利。

第一批实现将确定性计算放进程序，将研究判断方法放进 Skill。没有修改人类 Rules、已有自主模拟交易开关或真实交易权限。

## 现在可以做什么

在 Web 工作台的“研究”页保存假设与版本，选择 A 股代码运行回测，再查看净值、费用、已平仓交易、未平仓和警告。每次实验保存数据快照，重启后仍可查看。新版本保留旧版本及其研究结果。

当前只实现 `trend_breakout`：

- 当日收盘价超过此前 N 根日线最高价，快均线高于慢均线，当日量达到此前 M 日均量的指定倍数。
- 箱体和均量都不包含当天；均线包含当天收盘价。
- 跌破慢均线或快均线低于慢均线，产生退出信号；另按实验配置处理止损止盈。
- 文字假设和其他失效条件是研究说明，不会被解释成可执行代码。

窗口、仓位与止损止盈默认值是可复现实验起点，不是从收藏中验证得到的最优参数，也不是 Agent 交易的新增固定限制。

美股和港股假设可以分别记录；当前程序拒绝使用 A 股回测模型验证 US/HK，仍保持原有 A 股产品定位。分市场研究的核对项见 [市场与证据边界](../.trade-compass/skills/strategy-research/references/market-context.md)。

## 同一业务逻辑的三个入口

Web/API 与 Agent/CLI 都调用 `evaluation.research`，不会维护独立的回测或版本规则。

| 操作 | API | Agent 工具 | CLI |
| --- | --- | --- | --- |
| 保存新版本 | `POST /api/research/strategies` | `save_strategy_research` | `trade-compass research save strategy.json` |
| 查看版本与结果 | `GET /api/research/strategies`、`GET /api/research/runs` | `get_strategy_research` | `trade-compass research list` |
| 运行回测 | `POST /api/research/runs` | `run_strategy_backtest` | `trade-compass research run request.json` |
| 查看完整快照 | `GET /api/research/runs/{run_id}` | `get_strategy_research(run_id=...)` | `trade-compass research show RUN_ID` |

策略定义示例：

```json
{
  "strategy_id": "trend-box",
  "name": "放量突破假设",
  "market": "CN",
  "hypothesis": "放量突破此前箱体后，趋势是否更容易延续？",
  "invalidation": "跌破慢均线，或快均线转弱；另外观察震荡市的反复损失。",
  "method": "trend_breakout",
  "parameters": {
    "fast_window": 10,
    "slow_window": 20,
    "breakout_window": 20,
    "volume_window": 10,
    "volume_ratio": 1.2
  },
  "sources": [],
  "knowledge_refs": ["strategy-research"]
}
```

只把实际读过的来源加入 `sources`。保存后取返回的 `version`，提交请求：

```json
{
  "strategy_id": "trend-box",
  "version": "替换为保存返回的16位版本",
  "symbol": "600519",
  "limit": 250
}
```

这是参数示例，不是选股建议。`limit` 是向行情源请求的日线数，不保证行情源有完整历史；可选 `start_date`、`end_date` 限定回测区间，区间前的数据仅用于指标预热。当日及未来日线被排除。可选 `backtest` 传完整实验配置，未传时费用取当前应用配置；实际采用的配置始终保存在结果中。

## 时间、价格和费用口径

引擎版本 `daily-next-open-v1` 使用收盘信号和下一条信号日历记录的开盘价。调用者传入的信号日期序列决定尝试成交的日期；研究入口以返回的日线序列生成信号。缺少该日有效 OHLCV、零量或显式停牌时不成交。完整日历和停复牌事件仍是后续补强项，不声称能还原所有历史交易机会。

T+1 阻止新买入份额当日卖出；现有市场规则识别出的 T+0 标的按其规则处理。止损跳空按开盘价计算；开盘未跨过止损止盈、而日内两者都触达时，保守地先处理止损。日线模型无法证明排队、竞价和盘中流动性条件下能成交。

回测与模拟账本共用手续费函数：佣金（含最低佣金）、过户费、卖出印花税和单边滑点。滑点作为成本计入一次。已平仓净盈亏包含买卖两端费用，期末净值为现金加未平仓估值；不强制虚构期末卖出。沿用旧收盘估值时保留价格日期及过期标记。闭合交易统计和包含未平仓的净值风险分别计算。

旧回测结果不会被迁移或重写。修复后重新运行的结果可能变化，原因包括成交时点、费用及期末持仓口径修正。默认回测费用与当前模拟账本默认值对齐，不表示这些费率适用所有历史日期。

行情元数据分别提供 `retrieved_at`（响应生成时间）、`data_as_of`（行情观察时间）、`adjustment`、`is_sample`、`is_cached` 与来源。无法确定行情源原始抓取时间时 `origin_fetched_at=null`。历史、缓存和示例日线不会因为刚返回就标为实时。

## 数据、版本和恢复

写入位置在配置的 `data_dir/research/`：

- `strategies/<strategy_id>/<version>.json`：内容哈希标识的不可覆盖版本。
- `runs/<run_id>.json`：策略快照、参数、完整行情、信号、费用、结果、引擎/应用版本、知识快照和校验值。

列表接口省略行情、逐日信号及知识正文；完整记录接口保留原始证据。`replay_research_run(record)` 可断网重放；引擎版本不一致时拒绝静默使用新语义。数据根目录的现有备份、预览恢复和迁移机制同时覆盖这些记录。没有新建数据库、后台服务或调度任务，没有写入已安装包目录。

研究记录当前只会标记为 `sample_demo` 或 `exploratory_backtest`。不自动宣称完成样本外、前向模拟或真实执行验证。复权价格、幸存者偏差、分红、历史费率、停复牌和涨跌停排队等限制会影响结果，必须结合警告判断。

## 信号、成交和复盘

`emit_signal` 可附账户、策略版本和研究记录；关联存在性、标的和版本会被核对。`place_paper_trade` 可传明确的 `signal_id`，成交账本和 Decision Journal 保留对应关系与来源 Skill。

信号不再仅凭“同一股票的最新分析”猜测归因。未关联旧成交不自动补链；关联不明的历史结果保留原口径。新关联的结果使用账本 FIFO 分配数量与费用，部分卖出保持活跃，全部对应买入份额结清后才闭合。工作流也使用同一结算函数；衍生追踪失败不会把已经成功的成交回报成失败。

旧的价格跟随结果标为 `price_followthrough`；新成交归因标为 `execution_net_pnl`。前者不能作为后者引用，累计统计也不代表策略样本外有效。

`analyze_outcomes` 默认保留旧价格跟随口径；分析成交净收益时须显式传 `outcome_basis="execution_net_pnl"`。两类证据分别筛选，避免混合训练或规则提炼。

## Agent 应学会的内容

- `strategy-research`：来源纪律、市场区分、可执行规则、假设与反例、实验记录。
- `strategy-validation-review`：审查时间泄漏、样本选择、费用、基准、失效环境与成交归因。

这两份版本化内置 Skill 随 wheel 分发，可被可写记忆库中的同名 Skill 覆盖，仍服从人类 Rules 与置顶记忆。研究记录快照保存本次可读取的 Skill 及引用 Markdown 的内容哈希。现有 Skill `verified` 仍表示过程质量，不赋予策略盈利证明或交易权限。

后续演进应以证据推进：先补完整交易日历、不可成交条件、公司行动和当时可见数据，再增加事前冻结的样本外/滚动验证与前向模拟。多市场成交模型应独立定义交易场所、币种、日历、结算、费用和证券单位，不能仅给 A 股模型换代码。
