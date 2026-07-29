# Trade Compass Agent（交易罗盘）

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?logo=fastapi)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111)](https://react.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/AdenChenCoder/trade-compass-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/AdenChenCoder/trade-compass-agent/actions/workflows/ci.yml)

[简体中文](README.md) | [English](README.en.md)

**一个专为 A 股市场打造的本地优先 AI 投研工作台。**

Trade Compass Agent（交易罗盘）将行情数据、技术面与基本面分析、专业 Agent、模拟组合、自动化工作流和长期记忆整合在一个 Web 与 CLI 应用中。你可以用它研究股票、制定交易计划、跟踪信号、复盘决策，并将自己的投研方法沉淀为一套可重复执行的工作流。

![交易罗盘 Web 工作台](docs/assets/trade-compass-workbench.png)

## 快速开始

### 环境要求

- 一键安装：macOS 或 Linux，以及 `curl`
- 手动安装或源码开发：Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)
- 一个可用的 LLM 服务密钥；默认使用 DeepSeek

### 安装正式版本

首个正式版本将通过 GitHub Release 提供。发布后，macOS 或 Linux 用户可
一键安装：

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://github.com/AdenChenCoder/trade-compass-agent/releases/latest/download/install.sh | sh
```

如果本机已经安装 `uv`，也可以直接安装：

```bash
uv tool install trade-compass-agent
```

安装完成后，运行配置向导：

```bash
trade-compass setup
```

终端向导会依次完成模型与 API Key、存储目录、行情数据、定时自动化、
消息渠道、增强搜索和隐私设置。之后可再次运行 `trade-compass configure`
调整设置。使用 `↑/↓` 移动、`Space` 多选、`Enter` 确认；密钥输入会自动
隐藏。

配置完成后，检查运行环境并启动交易罗盘：

```bash
trade-compass doctor
trade-compass serve --open
```

通过上述方式安装时无需准备 Node.js 或 pnpm，Web UI 和股票分析所需的
K 线图表能力均已包含在应用中。

### 从源码运行

源码开发需要 Node.js 20 和 pnpm 9+。

```bash
uv sync
pnpm install --frozen-lockfile
pnpm --dir apps/web build
cp .env.example .env
chmod 600 .env
```

在仓库 `.env` 中填写模型 API Key，然后启动源码工作区：

```dotenv
DEEPSEEK_API_KEY=your-deepseek-key
```

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

### 可扩展 Skills

内置 Skills 提供催化日历、想法生成、日内技术和投资大师方法等研究能力。
你也可以添加自己的 Skill，将个人投研流程和输出规范交给 Agent 重复执行。

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

以下命令假定应用已经安装；从源码运行时，请在命令前添加 `uv run`。

### Web 工作台

```bash
trade-compass serve
```

Web UI 集中提供 Agent 对话、历史会话、模拟组合、记忆、审计记录、用户规则、Skills、定时任务和设置。交互式 API 文档位于 `http://127.0.0.1:19704/docs`。

不启动后台定时任务：

```bash
trade-compass serve --no-scheduler
```

### CLI

```bash
# 向 Agent 提问
trade-compass agent "今天 A 股市场怎么样？"

# 检查市场数据
trade-compass market-pulse
trade-compass data check 600519 510300

# 查看定时任务、规则和研究记录
trade-compass jobs list
trade-compass rules list
trade-compass audit recent --limit 20
trade-compass evaluate --limit 100
```

运行 `trade-compass --help` 可以查看全部命令。

### 作为本地服务运行

```bash
trade-compass service install
trade-compass service status
trade-compass service verify
```

## 配置

日常设置通过配置向导管理，无需手动修改配置文件：

```bash
trade-compass configure
```

安装版的设置和密钥保存在本机 `~/.trade-compass/` 下；源码工作区使用
`config/default.yaml` 和仓库根目录的 `.env`。

支持的 LLM 提供商包括 DeepSeek、OpenAI、Anthropic、OpenRouter、DashScope、Ollama 和 LM Studio。默认安装已包含股票分析所需的图表渲染能力；可选依赖还可以增加 Tushare、MCP 客户端、消息通道、行情预测和增强搜索能力。

例如，启用 Tushare 数据：

```bash
uv tool install "trade-compass-agent[tushare]"
```

再次运行 `trade-compass configure`，选择自动或 Tushare 数据源，并在
隐藏输入框中填写 Token。

## 文档导航

| 目标 | 文档 |
| --- | --- |
| 安装并完成首次运行 | [快速上手](docs/getting-started.md) |
| 配置模型、存储、数据源和可选功能 | [配置](docs/configuration.md) |
| 使用和自动化命令行 | [CLI 参考](docs/cli.md) |
| 创建并打包运行时 Skills | [Skills](docs/skills.md) |
| 理解仓库和状态边界 | [架构](docs/architecture.md) |
| 构建、验证和发布版本 | [发布](docs/releasing.md) |

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

## 社区

- 提议较大改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 通过 [SUPPORT.md](SUPPORT.md) 选择正确的支持入口。
- 安全漏洞按 [SECURITY.md](SECURITY.md) 私下报告，不要创建公开 Issue。
- 社区参与遵循 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
- 版本记录维护在 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

[MIT](LICENSE)
