<div align="center">

# JMeter Agent

**把性能测试文档，变成一份站得住的达标 / 不达标结论。**

面向 Apache JMeter 的本地 AI 智能体：读懂你的性能需求，生成测试计划，执行压测，<br>
再拿每次结果逐条对照当初约定的指标下结论，并附上证据。

[English](README.md) · **简体中文**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Vue](https://img.shields.io/badge/vue-3-42b883?logo=vuedotjs&logoColor=white)
![JMeter](https://img.shields.io/badge/Apache%20JMeter-5.6.3-D22128?logo=apachejmeter&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-6f42c1)

<img src=".github/assets/report-evidence-zh.png" alt="测试报告：逐条需求的实际值、判定结果和文档原文" width="100%">

</div>

## 为什么做这个

大多数「AI + 压测」工具的分析停留在「P95 有点高，建议加缓存」，这样的结论没法拿去验收。压测结束后真正需要回答的是：

- **需求达标了吗？** 每一条都要有实际值，以及超出多少。
- **性能变差了吗？** 和基线、和上一次执行比，整体和接口级都要看。
- **为什么？** 要看测试时间窗内的服务端指标，而不是只凭客户端响应时间去猜。

JMeter Agent 先用确定性的方式算出这些答案，再让 AI 解释；AI 必须引用需求编号和实际数值，拿不出证据的地方会标成【假设】，并给出验证方法。

## 工作方式

```mermaid
flowchart LR
    A["测试文档<br/>（需求说明、测试方案、接口文档）"] -->|AI 提取，<br/>人工确认| B["性能需求<br/>P95 ≤ 1 秒、错误率 ≤ 0.1%、<br/>500 并发"]
    B -->|负载模型预填| C["JMeter 测试计划<br/>（表单或 JMX 树）"]
    C -->|执行| D["证据<br/>逐条判定 · 基线对比 ·<br/>Prometheus 指标"]
    D -->|引用证据，而不是猜| E["AI 分析"]
    D -. 同一份数据 .-> F["Claude Code / Cursor<br/>（通过 MCP）"]
```

1. **文档 → 需求**：粘贴或上传文档，AI 生成可量化的需求草稿（P90/P95/P99、平均、最大、错误率、TPS）和负载模型。每条需求都附带文档原文，原文在文档里找不到的会标出来待核对；确认之前不会保存。
2. **需求 → 测试计划**：新建计划时按文档里的并发、加压时间和时长预填，可以在表单视图或完整的 JMX 树里编辑。
3. **执行 → 证据**：每次执行逐条判定需求（接口级需求取匹配到的最差接口），自动和当前基线、上一次执行对比，并采集测试时间窗内的 Prometheus 指标，算出饱和时间占比和与响应时间的相关系数。
4. **证据 → 分析**：AI 分析拿到的是同一份证据，结论必须以它为依据。

## 功能

| | |
|---|---|
| **需求判定** | 支持整体和接口级需求；每份报告顶部给出判定，不达标的标出超出比例，文档原文自动核对。 |
| **从文档开始** | 一个向导接收需求 / 接口文档或 OpenAPI 地址，生成项目、测试计划和需求草稿。 |
| **稳定的基线** | 在报告上一键「设为基线」；基线保存指标快照，删掉旧执行也不影响对比。 |
| **服务端证据** | 内置主机（node_exporter）、JVM + HikariCP、MySQL 的 Prometheus 指标预设，按测试时间窗采集并缓存。 |
| **可信的 AI 分析** | 事实表、数字校验、假设台账和历史执行知识库（RAG）。支持 DeepSeek、OpenAI、Claude、通义千问及任何 OpenAI 兼容接口。 |
| **内置 MCP** | Streamable HTTP 端点，提供 `get_execution_evidence`、`extract_performance_requirements`、`promote_execution_to_baseline` 等证据工具；写操作需要在界面上确认。 |
| **扎实的 JMeter 底座** | 流式 JTL 解析（精确分位数、内存不随样本数增长）、压测机繁忙时自动排队、WebSocket 实时进度、SelfHeal 修复失效的提取规则。 |
| **默认私有** | 没有账号和登录；服务只允许监听本机回环地址，并校验 Host / Origin，防止浏览器里的其它网站调用本地接口。 |

## 界面截图

<table>
  <tr>
    <td width="50%"><img src=".github/assets/project-overview-zh.png" alt="项目概览：闭环进度和最近执行"><br><sub><b>项目概览</b>：配置进度和最近的判定结果</sub></td>
    <td width="50%"><img src=".github/assets/requirements-zh.png" alt="性能需求：AI 草稿、文档原文核对、负载模型"><br><sub><b>性能需求</b>：AI 草稿、原文核对、负载模型</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src=".github/assets/report-trends-zh.png" alt="报告趋势：响应时间、吞吐量和错误率曲线"><br><sub><b>趋势</b>：响应时间、吞吐量、错误率与分位数</sub></td>
    <td width="50%"><img src=".github/assets/runs-zh.png" alt="执行记录：判定、关键指标和 AI 分析状态"><br><sub><b>执行记录</b>：每次执行的判定和关键数字一目了然</sub></td>
  </tr>
</table>

<sub>截图来自对公开的 JSONPlaceholder API 的一次示例压测，配了 4 条示例需求。</sub>

## 快速开始

**环境要求**：Python 3.11+、Node.js 20+、Java 11+，以及 [Apache JMeter 5.6.3](https://jmeter.apache.org/download_jmeter.cgi)。已在 Windows 和 Linux 上测试；macOS 理论上可用，但尚未测试。

```bash
git clone https://github.com/xiaoyuCR7/jmeter-ai-agent.git
cd jmeter-ai-agent
```

**1. 让程序找到 JMeter**（任选一种）：

- 把压缩包里的 `bin/` 和 `lib/` 解压到项目根目录（无需配置）；
- 设置 `JMETER_HOME` 指向 JMeter 的 `bin` 目录（或安装根目录）；
- 把 JMeter 的 `bin` 加入 `PATH`。

只有执行压测时才需要 JMeter，编辑测试计划和跑单元测试都不需要。

**2. 启动**

```bash
./start.sh        # Linux / macOS
start.bat         # Windows
```

或手动启动：

```bash
cd jmeter-agent-backend && pip install -r requirements.txt && python -m app.main   # http://127.0.0.1:8000
cd jmeter-agent-frontend && npm install && npm run dev                             # http://localhost:3000
```

**3. 添加模型**：在「设置 → AI 模型配置」里选择 DeepSeek、通义千问、OpenAI、Claude 或任意 OpenAI 兼容接口，填入 API Key。
「上下文窗口」留空即可，保存时会向服务商查询这个模型的真实大小（DeepSeek 和 Claude 的接口会返回，其余按官方公布值）。
然后点左上角「从文档开始」。

接口文档地址：http://127.0.0.1:8000/api/docs

## 在 Claude Code、Cursor 等助手里使用

```bash
claude mcp add --transport http jmeter-agent http://127.0.0.1:8000/api/v1/mcp
```

Cursor、VS Code 等客户端的配置可以在「设置 → 外部助手接入（MCP）」里一键复制。分析时让助手使用 `analyze-execution` 提示词，或先调用 `get_execution_evidence`，它拿到的数据和报告页完全一致。只支持 SSE 的旧客户端可以用 `/api/v1/mcp/sse`。

## 配置

`jmeter-agent-backend/.env` 中的常用配置：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `PORT` | 后端端口 | `8000` |
| `JMETER_HOME` | JMeter 的 `bin` 目录或安装根目录 | `<项目>/bin` |
| `JMETER_TIMEOUT` | 单次执行最长时间（秒） | `3600` |
| `JMETER_HEAP_MIN` / `JMETER_HEAP_MAX` | JMeter JVM 堆内存 | `256m` / `2g` |
| `JMETER_EXECUTION_MODE` | `local` 或 `container`，见下方「执行隔离」 | `local` |
| `JMETER_MAX_OUTPUT_MB` | 报告目录超过这个大小（MB）时停止执行，`0` 表示不限制 | `10240` |
| `RESOURCE_CPU_THRESHOLD` / `RESOURCE_MEMORY_THRESHOLD` | 压测机占用超过该比例（%）时新执行排队 | `80` / `85` |
| `DEFAULT_AI_MODEL` | 请求没有指定模型时使用的模型 ID | 第一个启用的模型 |
| `AGENT_CONTEXT_TOKEN_BUDGET` | 上下文窗口未知的模型使用的 token 预算 | `16384` |

模型的 API Key 不从 `.env` 读取，请在界面里添加。它们保存在 `jmeter-agent-backend/ai_models_config.json`，这个文件已被 git 忽略。

因为没有登录，`HOST` 只能是本机回环地址，设成其它地址服务会拒绝启动。

### 执行隔离

JMeter 计划可以运行代码：JSR223 / BeanShell 组件、OS Process Sampler，以及 `${__groovy()}` 这类函数。
`JMETER_EXECUTION_MODE` 决定这些代码在哪里运行。

| 模式 | JMeter 怎么运行 | 适用场景 |
|---|---|---|
| `local`（默认） | 本机子进程，权限和你的账号相同。计划里有脚本、系统命令或 Include Controller 时，运行前会列出来请你确认（智能体发起的执行会单独再确认一次）。本应用生成的计划不含这些组件。 | 自己写的或在这里生成的计划 |
| `container` | Docker：根文件系统只读、去掉全部 Linux 权限、禁止提权、非 root 账号、限制内存 / CPU / 进程数、不共享 IPC，只读挂载计划所在的项目目录 | 来自其它地方的计划 |

容器模式需要 Docker，以及 `JMETER_CONTAINER_IMAGE`：一个本地已有、按 `@sha256:` 摘要锁定版本的镜像（不会自动拉取）。
容器需要联网才能施压；如果不想让它访问局域网或云服务器的元数据地址，请创建一个限制了出站访问的 Docker 网络，
并设置 `JMETER_CONTAINER_NETWORK`。每个执行容器都带有 `jmeter-agent` 标签，后端启动时会清理残留的容器。

两种模式下，报告目录超过 `JMETER_MAX_OUTPUT_MB`（默认 10240）时都会停止执行；JMeter 也拿不到后端的环境变量
（模型密钥、数据库配置等）。

### Webhook

设置 `WEBHOOK_URL` 后，每次执行都会向它 POST 事件，不论是从界面、智能体、调度器还是 MCP 发起的：

| 事件 | 时机 | 开关 |
|---|---|---|
| `test.started` | JMeter 正在启动 | `WEBHOOK_ON_START` |
| `test.completed` / `test.failed` / `test.cancelled` | 执行结束，在 AI 分析之前发送 | `WEBHOOK_ON_COMPLETE` / `WEBHOOK_ON_FAILURE`（同时管失败和取消） |
| `analysis.completed` | 执行后的 AI 分析完成或失败 | `WEBHOOK_ON_ANALYSIS` |

JSON 内容包括执行、项目、计划的 ID，计划名称，状态，时长，关键指标，需求判定结果和错误信息。错误信息只保留第一行，
本机路径只留文件名。`analysis.completed` 默认只发分析状态和结论；设置 `WEBHOOK_INCLUDE_ANALYSIS=true` 才会附上完整评估，
其中可能提到你的接口细节。

请使用 `https` 地址并设置 `WEBHOOK_SECRET`，否则服务启动时会记一条警告。接收方应当：

1. 用密钥对 `<X-JMeter-Agent-Timestamp>.<请求原始内容>` 计算 HMAC-SHA256，与 `X-JMeter-Agent-Signature` 中的
   `sha256=<十六进制>` 做常量时间比较（例如 Python 的 `hmac.compare_digest`）。
2. 拒绝 `X-JMeter-Agent-Timestamp`（Unix 秒）超过 5 分钟的请求。
3. 丢弃已经处理过的 `X-JMeter-Agent-Delivery` 投递 ID，重试时这个 ID 不变。

只有 2xx 算发送成功。网络错误、408、429 和 5xx 会重试 3 次（间隔 1 秒、5 秒、15 秒）；跳转和其它 4xx 不重试，
所以 `WEBHOOK_URL` 要填最终地址。日志里只记录接收方的主机名，不记录完整地址，因为 Webhook 地址里常带有 token。

<details>
<summary><b>主要接口</b></summary>

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / PUT | `/api/v1/projects/{id}/requirements` | 读取 / 保存性能需求 |
| POST | `/api/v1/projects/{id}/requirements/extract` | 从文档文本生成需求草稿 |
| GET / PUT | `/api/v1/projects/{id}/monitoring` | Prometheus 配置 |
| POST | `/api/v1/test-plans/{id}/execute` | 执行测试计划 |
| GET | `/api/v1/executions` | 执行列表（含判定和关键指标） |
| GET | `/api/v1/executions/{id}/report` | 完整报告 |
| GET | `/api/v1/executions/{id}/evidence` | 需求判定、历史对比、服务端指标 |
| POST | `/api/v1/executions/{id}/promote-baseline` | 设为基线 |
| POST | `/api/v1/mcp` | MCP Streamable HTTP 端点 |

</details>

<details>
<summary><b>目录结构</b></summary>

```
jmeter-ai-agent/
├── jmeter-agent-backend/     FastAPI 服务
│   └── app/
│       ├── api/              REST 与 MCP 路由
│       ├── services/
│       │   ├── requirements.py、evidence.py、baseline_compare.py、server_metrics.py
│       │   ├── ai/           模型适配、智能体、RAG
│       │   └── jmeter/       执行器、JMX 生成与解析、流式 JTL 解析
│       └── middleware/       本机 Host / Origin 校验
├── jmeter-agent-frontend/    Vue 3 + TypeScript + Element Plus
└── start.sh / start.bat
```

`bin/`、`lib/`（JMeter 运行时）、测试计划、报告和本地数据库都不入库。

</details>

<details>
<summary><b>常见问题</b></summary>

- **提示 `JMeter executable not found`**：先确认 `java -version`，再检查三种方式之一：项目根目录有 `bin/jmeter(.bat)`、设置了 `JMETER_HOME`、或 JMeter 在 `PATH` 中（`jmeter --version`）。
- **AI 分析失败**：在「设置 → AI 模型配置」里测试模型连接，检查密钥和网络。
- **提示「exceeds the context budget」**：这个模型的上下文窗口未知，用的是保守的 `AGENT_CONTEXT_TOKEN_BUDGET`。编辑模型，填上它的上下文窗口（单位 tokens）即可。
- **端口被占用**：`PORT=8001 ./start.sh`（Windows：先 `set PORT=8001` 再 `start.bat`）。
- **执行一直排队**：压测机占用超过了 `RESOURCE_*_THRESHOLD`，顶栏会显示 CPU / 内存。
- **日志**：`jmeter-agent-backend/server_stdout.log`、`server_stderr.log`，以及每次执行的 `reports/{execution_id}/jmeter.log`。

</details>

## 开发

```bash
cd jmeter-agent-backend && pip install -r requirements-dev.txt && pytest tests
cd jmeter-agent-frontend && npm install && npm run test:run && npx vue-tsc --noEmit
cd jmeter-agent-frontend && npx playwright install chromium && npm run test:e2e   # 浏览器冒烟测试（API 已模拟）；加 PW_CHANNEL=chrome 可复用本机 Chrome
```

欢迎提 Issue 和 PR。PR 请保持聚焦，行为改动请附带测试。

## 许可证

[Apache License 2.0](LICENSE)。Apache JMeter 是 Apache 软件基金会的商标，本项目与其没有隶属或背书关系。
