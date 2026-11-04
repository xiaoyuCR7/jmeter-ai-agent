# JMeter Agent 架构

这份文档讲清楚代码怎么分工、一次运行经过哪些环节，以及 AI 在哪里起作用、在哪里不起作用。功能介绍和使用方法见 [README](../README.zh-CN.md)。

## 一条原则

**确定性的事交给代码，需要读懂内容的事才交给模型。**

- 需求是否达标、和基线比较、服务端指标、该先看哪些接口（`focus`），都由代码从 JTL 和 Prometheus 算出来。
- 模型只做三件事：从文档里读出需求和接口，写报告里的解释，回答用户的追问。
- 模型写出的每个数字都要经过 `FactValidator` 核对。核对不上就让模型改写一次，改写后仍不对的数字会在报告里标出来。
- 文档工作流、容量探索、验证实验的步骤是固定的，所以由代码驱动，不让模型去规划。

## 目录

```
jmeter-agent-backend/app/
├── api/                         REST 路由，每个领域一个文件；experiment_routes 负责验证运行、容量探索和 CI 门禁
├── services/
│   ├── execution_lifecycle.py   所有运行的唯一入口：prepare → run → post_run，可以按线程数缩放计划
│   ├── post_run.py              每次运行结束后：写 AI 报告、记录事实和假设
│   ├── experiments.py           验证运行、容量探索、CI 门禁
│   ├── evidence.py              需求判定、基线对比、服务端指标、运行诊断，汇总成一份证据
│   ├── run_diagnostics.py       按接口和时间段的诊断，以及 focus（最该先看的接口）
│   ├── jmeter/
│   │   ├── executor.py          启动 JMeter 子进程或容器
│   │   ├── run_guard.py         运行中读 JTL，请求几乎全部失败时停止运行
│   │   ├── live_stats.py        把运行中读到的结果按 5 秒汇总，供实时面板（GET /executions/{id}/live）
│   │   ├── load_variants.py     按比例缩放线程组，不改动计划文件
│   │   └── report_parser.py     流式解析 JTL，分位数是精确值
│   └── ai/
│       ├── providers.py         设置页里的模型服务商预设
│       ├── adapters/            OpenAI 兼容、Anthropic、Azure OpenAI 三种适配器
│       ├── agent/               tool_loop（唯一的工具调用循环）、ToolRegistry、工具定义
│       ├── agents/              AnalysisAgent（追问）、WorkflowAgent（从文档生成计划）
│       ├── analyzer.py          报告：一次结构化调用，然后核对数字
│       ├── analysis_context.py  报告和追问共用的历史召回
│       └── memory/              事实账本、假设账本、经验、数字核对
jmeter-agent-frontend/src/       Vue 3 + Element Plus
```

## 一次运行的过程

1. 运行可以从 Web、MCP、调度器或实验入口发起，最后都调用 `execution_lifecycle.prepare`。
   - 先检查计划里的脚本，没被批准就拒绝运行。
   - 再套用当前激活的环境；如果是生产环境，要用户确认。
   - 按需缩放线程数，然后把实际要跑的计划存一份快照到 `reports/<id>/snapshot.jmx`。
2. `run` 启动 JMeter，同时启动 `run_guard`。如果运行被守护停止，`error_message` 里会写明原因。守护读到的每一行结果也交给 `live_stats` 汇总，只存在内存里，运行结束即丢弃；进度按计划时长和已运行时间计算（计划按循环次数结束时无法预估）。
3. 运行结束后解析 JTL，发出 `finished` 事件（Webhook 由这个事件触发），然后调用 `post_run.finish_run`。
4. `post_run` 汇总证据，调用 `TestAnalyzer` 写报告。报告使用的语言由 `REPORT_LANGUAGE` 或页面语言决定。
5. 报告写完后，它的数字会被核对；事实写入事实账本；新的假设写入假设账本；之前的假设如果能用这次运行判定，也一并判定。

## AI 调用

| 场景 | 入口 | 形态 | 工具 |
|---|---|---|---|
| 报告 | `analyzer.TestAnalyzer` | 一次结构化输出（Claude 用强制工具，其它模型用 JSON mode） | 无 |
| 追问 | `AnalysisAgent.run_analysis` | `tool_loop` | 只读，而且只能看这一次运行 |
| 从文档生成计划 | `WorkflowAgent` | 先提取需求，再用 `tool_loop` 调 `save_plans` | 只有 `save_plans` |
| 需求提取 | `requirements.extract_requirements` | 一次调用，每条需求附上原文引用 | 无 |
| 外部助手 | MCP（`mcp_server.py`） | 客户端驱动 | 全部内置工具。`write` 和 `execute` 两类工具要用户在界面上确认 |

并发：同时最多运行 `AGENT_MAX_CONCURRENT` 个模型任务。运行后自动写报告属于后台任务，后台任务最多占用 N-1 个名额，所以批量运行结束时，用户的追问不会一直排队。

## 存储

- 业务数据（项目、计划、执行、环境、基线）存在 SQLAlchemy async + SQLite 里。`storage.store` 是它在内存里的读缓存，启动时加载一次，所有写入都先经过 `db_store`。
- 服务层（`app/services`）不依赖 Web 层：拒绝请求时抛 `app.core.errors.ServiceError(状态码, 说明)`，由 `main.py` 统一转成 HTTP 响应；agent 工具和 MCP 看到的是说明文字。所有路由都在 `app/api/routes.py` 里注册。
- 记忆数据和业务数据在同一个 SQLite 文件里，但各自用独立的表和连接：
  - 事实和假设用 aiosqlite；
  - 经验（`experience.py`）的读写量很小，用同步 sqlite3，在线程里执行。
- 容量探索的记录是 `BASE_DIR/capacity_searches/*.json`，每次探索一个文件。
- 模型配置在 `ai_models_config.json` 里，这个文件被 git 忽略。

## 模型适配

- `OpenAIAdapter` 负责所有 OpenAI 兼容服务。如果某个模型拒绝 `max_tokens`、`temperature` 或 JSON mode，适配器会改名或去掉这个参数后重发一次，并记住这个结果，之后的请求直接按新方式发。
- `AnthropicAdapter`：在官方端点上，系统提示和第一条用户消息（运行的证据）会打上提示缓存标记；报告用强制工具输出，保证返回的是一个完整的对象。
- `AzureOpenAIAdapter`：使用资源端点和 API 版本，模型名填部署名。
- 本机服务（Ollama、LM Studio、vLLM，或者任何 loopback 地址）可以不填 Key。
