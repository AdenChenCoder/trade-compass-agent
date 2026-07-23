# Trade Compass Agent（交易罗盘）

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?logo=fastapi)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111)](https://react.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | [简体中文](README.zh-CN.md)

**一个专为 A 股市场打造的本地优先 AI 投研工作台。**

Trade Compass Agent（交易罗盘）将行情数据、技术面与基本面分析、专业 Agent、模拟组合、自动化工作流和长期记忆整合在一个 Web 与 CLI 应用中。你可以用它研究股票、制定交易计划、跟踪信号、复盘决策，并将自己的投研方法沉淀为一套可重复执行的工作流。

## 快速开始

### 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 和 pnpm 9+
- 一个可用的 LLM 服务密钥；默认使用 DeepSeek

### 安装并启动

在仓库根目录执行：

```bash
uv sync
pnpm install --frozen-lockfile
pnpm --dir apps/web build

cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写模型 API Key：

```dotenv
DEEPSEEK_API_KEY=your-deepseek-key
```

启动交易罗盘：

```bash
uv run trade-compass doctor
uv run trade-compass serve --open
```

打开 `http://127.0.0.1:19704/agent`，即可开始对话。

```text
结合价格结构、基本面、近期公告和市场环境分析 600519，
给出核心驱动因素、主要风险和交易计划。
```

## 产品特性

### AI 驱动的市场研究

Agent 可以获取行情、计算技术指标、检查基本面与公司公告、搜索资讯和研究资料、分析资金流，并将不同来源的信息汇总为一份完整结论。

### 专业 Agent 团队

将不同研究任务交给内置的专业角色：

- **日内技术分析** — 短周期价格结构与技术信号
- **个股研究** — 基本面、研究资料、多空辩论与投资经理综合判断
- **宏观情绪** — 宏观环境、市场情绪与资金流
- **智能筛选** — 对量化筛选候选进行 AI 复核
- **产业链卡点分析** — 识别供应链瓶颈与关键上游公司
- **组合风控** — 分析敞口、集中度、回撤与组合风险

### A 股市场工具箱

- 日线与分钟线行情，并支持可替换的数据源
- MA、MACD、RSI、布林带、ATR 等技术指标
- 市场脉搏、板块强度、资金流、基本面、公司公告和资讯
- A 股交易单位、T+0/T+1、不同板块涨跌停、费用与滑点
- 内置 AkShare 与 Baostock，可选接入 Tushare 和巨潮资讯

### 模拟组合与信号跟踪

创建多个模拟账户，记录模拟交易，分析持仓和盈亏，检查组合集中度，并在 1、3、5 个交易日后评估信号表现。

### 自动化研究工作流

按需运行或定时执行完整投研流程：

- 盘前简报与早盘计划
- 股票筛选与想法生成
- 日内技术与个股研究
- 催化日历与收盘检查
- 日终复盘与周度复盘

### 记忆、规则与复盘

交易罗盘会在本地保存会话、研究笔记、用户规则、历史决策和复盘记录。记忆搜索、反思、矛盾检测和决策调和可以将有效上下文带入后续研究。

### Web、CLI、API 与外部集成

通过 React 工作台交互，使用 CLI 自动化任务，基于 FastAPI API 扩展应用，连接 MCP 工具，并通过飞书、企业微信、微信或通用 Webhook 接收通知。

## 工作方式

```mermaid
flowchart LR
    U["Web / CLI / API"] --> A["交易罗盘 Agent"]
    A --> T["行情与研究工具"]
    A --> S["专业 Agent"]
    T --> P["交易计划与信号"]
    S --> P
    P --> F["模拟组合"]
    F --> R["复盘与记忆"]
    J["定时工作流"] --> A
```

## 使用方式

### Web 工作台

```bash
uv run trade-compass serve
```

Web UI 集中提供 Agent 对话、历史会话、模拟组合、记忆、审计记录、用户规则、Skills、定时任务和设置。交互式 API 文档位于 `http://127.0.0.1:19704/docs`。

不启动后台定时任务：

```bash
uv run trade-compass serve --no-scheduler
```

### CLI

```bash
# 向 Agent 提问
uv run trade-compass agent "今天 A 股市场怎么样？"

# 检查市场数据
uv run trade-compass market-pulse
uv run trade-compass data-check 600519 510300

# 查看定时任务、规则和研究记录
uv run trade-compass scheduler list
uv run trade-compass rules list
uv run trade-compass audit recent --limit 20
uv run trade-compass evaluate --limit 100
```

运行 `uv run trade-compass --help` 可以查看全部命令。

### 作为本地服务运行

```bash
uv run trade-compass service install
uv run trade-compass service status
uv run trade-compass service verify
```

## 配置

应用配置位于 `config/default.yaml`，API Key 和本地环境覆盖项位于 `.env`。

支持的 LLM 提供商包括 DeepSeek、OpenAI、Anthropic、OpenRouter、DashScope、Ollama 和 LM Studio。可选依赖还可以增加 Tushare、MCP 客户端、消息通道、图表渲染、行情预测和增强搜索能力。

例如，启用 Tushare 数据：

```bash
uv sync --extra tushare
```

```dotenv
TUSHARE_TOKEN=your-tushare-token
```

随后在 `config/default.yaml` 中设置 `data.tushare_enabled: true`。

## 开发与贡献

```bash
uv sync --extra dev
pnpm install --frozen-lockfile

uv run trade-compass serve --dev  # API: :19704
pnpm --dir apps/web dev            # Web UI: :3000
```

提交 Pull Request 前运行：

```bash
scripts/ci_check.sh
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
git diff --check
```

贡献说明请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)，版本记录请参阅 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

[MIT](LICENSE)
