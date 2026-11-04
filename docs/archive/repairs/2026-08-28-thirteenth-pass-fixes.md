# 第十三批修复：登记分析准入与取消资源归属

日期：2026-08-28。状态：**已实现并完成本地隔离回归；未部署，未进行真实模型或压测验收。**

本批仅实施用户回复“是”确认的[方案A](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-28-architecture-next-proposal.md)：显式带execution_id的登记分析使用单进程、无等待队列的准入限制。业务目标、分析职责和已准入任务的分析步骤保持不变。

## 一、实施结果

```text
既有鉴权、execution_id/路径预检
  → 既有Session互斥
  → 尝试取得登记分析名额
      满额/停机：返回原失败封装，附admission错误码；不调用模型，不自动重试
      成功：Fact准备 → 历史校验 → 模型循环 → 提取 → 验证/归档 → 假设评估
  → 编排结束时关闭lease
      没有同步工作：释放名额
      线程仍在运行：保留draining名额，实际future完成后才释放
```

`ANALYSIS_MAX_CONCURRENT`为可选正整数；未配置时沿用`AGENT_MAX_CONCURRENT`，源码默认是3，不代表实际部署配置。配置在资源管理器启动时生效，没有新增热更新行为，也没有改用户配置文件。

既有模型信号量与新分析名额相互独立；已准入分析等待模型信号量时仍占分析名额。不带ID的通用Agent请求、直接`analyze_with_rag`、JMeter执行调度和手工归档不受本批准入限制。

## 二、逐项修改说明

### 1. 分析资源管理器与容量配置

**【修改内容】** 新增[analysis_runtime.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/analysis_runtime.py)，提供立即准入、lease、实际同步future跟踪、停机及显式重启；在[config.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/core/config.py:115)增加可选容量和正整数校验。线程池按需创建，每个lease最多一个未完成同步提交。

**【修改原因】** 原模型循环限额不覆盖Fact准备及后处理。单靠取消await并在finally减计数，会在后台线程尚未结束时接纳新工作；只配置线程数也不能防止提交队列积压。

**【影响范围】** 登记分析的进程内资源数量；ContextVar仅传递资源归属并保留原上下文复制行为。不修改Prompt、Tool清单/权限、Memory数据结构、Workflow步骤或上下文预算。未绑定lease的调用仍使用原`asyncio.to_thread`。

**【风险等级】** 中，优先级高。峰值请求更早失败，模型名额等待期也占分析容量；错误的容量配置会明确失败。单个解析永久阻塞时，其名额也会持续占用。

**【验证方式】** 20项独立runtime测试覆盖容量/回退、非法配置、重复释放、真实线程取消后成功或异常、排队future取消、ContextVar传播、重复提交拒绝、提交失败、陈旧上下文、停机等待及重启。容量2的集成测试证明前两会话进入准备，第三会话在Fact/历史/模型调用前收到busy。

**【为什么这是最优方案】** 在已确认范围内直接复用Python标准库，不引入分布式队列、数据库迁移或多阶段锁；同时跟踪真实future，满足取消后不提前释放的关键条件。这里的“最优”指当前约束下的最小可靠方案，不是未经基准测试的吞吐最优声明。

### 2. 完整登记分析准入与HTTP/SSE兼容

**【修改内容】** 在[AnalysisAgent](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/analysis_agent.py:198)的既有Session保护之内增加准入，将lease覆盖到评估结束。忙碌返回`analysis_busy`，停机返回`analysis_unavailable`，均附`phase=admission`及原execution_id；不伪造Fact/归档字段。

**【修改原因】** 模型循环完成后仍可能发生提取、持久化和假设评估，此时提前释放会漏算正在执行的分析；超额请求也不应先读大JTL再排队。

**【影响范围】** 登记模式HTTP/SSE共用的编排入口；原HTTP 200业务失败结果及SSE一次最终done保持。BaseAgent、Session命名空间和模型信号量实现未修改。准入成功后的Prompt、Tool、Memory和Context构造不变，未增加推理步骤。

**【风险等级】** 中，优先级高。调用者需要处理明确的busy结果；本批没有新增排队、自动补偿或重试。

**【验证方式】** 分别阻塞准备、模型、提取、归档、评估阶段，验证其他会话立即拒绝、旧通用任务仍可用；覆盖准备/模型/归档失败释放、部分归档回执保留、HTTP/SSE失败封装、既有鉴权和Session冲突测试。原临时JTL→Fact→作用域归档端到端测试继续通过。

**【为什么这是最优方案】** 在已有共享编排入口设一道门，不复制HTTP与SSE流程，也不重复获取原模型信号量；拒绝请求不进入模型循环，因此不会为这些被拒绝的请求新增模型Token或Tool调用。

### 3. Fact读取、校验和重建的真实线程归属

**【修改内容】** 将AnalysisAgent的两个JTL计算边界，以及[FactLedger](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/memory/fact_ledger.py)保存前哈希、读回校验哈希、重建计算的既有`to_thread`调用改为上下文感知助手。只跟踪原本在线程中进行的计算，不把数据库事务或RAG缓存操作搬到线程池。

**【修改原因】** 仅跟踪首次JTL解析不足以覆盖后续历史校验和归档前哈希；取消这些读取也可能遗留实际工作。

**【影响范围】** 同步读取的调度与归属。Fact计算公式、来源路径校验、SHA256、旧哈希前缀兼容、SQL、事务和作用域过滤不变。Prompt/Tool/Memory内容及Workflow顺序不变；Context复制与原`to_thread`一致。

**【风险等级】** 中，优先级高。登记模式下同步读取共用分析线程池；不能声称消除了LanceDB原有同步阻塞或超大JTL的内存成本。

**【验证方式】** 四项真实线程屏障测试分别取消save/load/history/rebuild，证明物理读取结束前仍占名额，之后释放，原账本记录未被取消操作替换。既有Fact快照、哈希一致性、事务回滚、历史作用域及RAG归档测试全量通过。

**【为什么这是最优方案】** 只替换五处既有同步边界，保留数据库连接和缓存原归属；避免扩大到整个持久化架构，也不通过删校验或缓存陈旧证据来提速。

### 4. 主动取消和应用停机

**【修改内容】** 登记模式主动取消会中断拥有lease的分析任务等待，返回取消结果；普通任务取消仍传播取消信号。lease在finally关闭，但运行中的同步future继续占名额。[main.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/main.py:112)通过AsyncExitStack启动/清理runtime，清理先于adapter关闭。

**【修改原因】** 单独设置取消标记不能及时打断等待中的JTL读取或后处理；停机若先释放adapter或虚报线程完成，会造成资源与任务生命周期不一致。

**【影响范围】** 登记分析取消与应用生命周期；旧通用Agent取消逻辑、Prompt、Tool授权、Memory格式、Session释放语义不变。已提交的归档不回滚、不重放；取消可能发生在部分写入之后，最终回执不等于持久任务日志。

**【风险等级】** 中，优先级高。线程无法强制终止，因此停机可能等待真实同步读取结束；未设置会虚报清理成功的超时。原生同步存储调用仍可能阻塞事件循环。

**【验证方式】** 主动取消、Task重复取消、关闭SSE、授权撤销与各阶段关闭测试；真实线程取消时名额保持draining。停机测试验证拒绝新任务、取消owner、实际线程结束前不完成shutdown；生命周期测试覆盖启动后段失败、服务期失败及adapter关闭前runtime已停止。

**【为什么这是最优方案】** 复用已有取消、Session清理和AsyncExitStack机制，补充真实工作归属；不引入新的取消API或强杀线程机制，也不把Session长期锁住来代替资源管理。

### 5. 前端类型与无重放契约

**【修改内容】** 在[RegisteredAnalysis](D:/trae/项目/jmeter-agent/jmeter-agent-frontend/src/api/index.ts:12)增加可选admission阶段和两个错误码类型；在既有auth-api测试中验证busy/unavailable的POST及SSE不会重放。未改请求实现、页面或UI自动重试策略。

**【修改原因】** 新增可诊断的失败元数据需要类型表达；资源忙碌不能触发会重复分析或归档的自动POST重试。

**【影响范围】** 前端响应类型及传输契约测试；不影响Prompt、Tool、Memory、Context或Workflow。旧响应无需新增字段。

**【风险等级】** 低，优先级中。

**【验证方式】** 前端全量91项测试、vue-tsc及Vite构建；后端HTTP/SSE真实路由测试共同验证，非仅靠前端模拟响应。

**【为什么这是最优方案】** 只扩展可选类型和已有测试，不增加页面、新的协议版本或额外客户端状态机。

## 三、本轮验证证据

所有命令在本轮实际执行，模型为测试替身，存储和构建输出隔离；没有调用真实模型、JMeter、Docker、远程引擎或修改用户数据库。源文件基线在改动前保存到工作区之外，未覆盖。

| 检查 | 实际结果 | 证据 |
|---|---|---|
| runtime红灯 | 缺少runtime及配置校验时12失败、6 setup errors | [runtime-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/runtime-red.txt) |
| 集成红灯 | 接入前13失败、21通过 | [integration-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/integration-red.txt) |
| 接入后的专项回归 | 109通过；后续容量2场景及最终取消分支也纳入下面全量验证 | [admission-final.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/admission-final.txt) |
| 后端全量 | **804通过，524条warnings，46.22秒，退出码0** | [backend-full.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/backend-full.txt) |
| 前端全量 | **6文件、91通过，退出码0** | [frontend-final.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/frontend-final.txt) |
| vue-tsc --noEmit | **退出码0**，无诊断输出 | [typecheck-final.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/typecheck-final.txt) |
| Vite构建 | **退出码0，18.01秒**，产物仅在外部repair13目录 | [build-final.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/build-final.txt) |
| 全项目静态复扫 | **115个Python文件可解析，137个源文件模式扫描，无语法错误**；人工复核全部生产差异 | [rescan-results.json](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/rescan-results.json) |
| Prompt/策略表达式比对 | **7项与基线一致** | [prompt-check.json](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/prompt-check.json) |

本批源代码/测试差异为9个修改文件、2个新增文件，0删除：生产代码6个、测试5个；另有本报告、提案状态和实施计划文档。依赖文件及基线中的JSON/INI/TOML配置未变；没有数据库迁移。完整差异见[source-review.diff](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair13/source-review.diff)。

全量后端warnings包括已有datetime.utcnow、LanceDB table_names等弃用提示，不代表已消除这些技术债。模式扫描的`await tool.handler`命中位于ToolRegistry内，人工确认其前面仍有参数校验与确认检查；不能仅凭这个字符串判定权限绕过。

## 四、复核结论与剩余边界

在上述隔离测试、语法/模式扫描和生产差异复核范围内，未发现本批新增的功能回归。Prompt表达式和原有事实/假设规则未变，没有新增LLM提示、重试、Tool调用或Memory查询。被拒绝请求在测试中没有模型调用；**未测量生产Token、吞吐或延迟，不声明优化百分比，也不保证项目不存在其他Bug。**

明确未解决：

- 单进程限额不是跨worker总量限制；单个超大JTL仍可能消耗较多CPU/内存。
- 运行中的线程不能强杀；既有同步LanceDB操作仍可能阻塞事件循环。
- 取消/响应丢失后的持久回执、幂等、部分归档恢复没有新增；跨存储写入仍非一个事务。
- 历史读取故障与空历史的严格契约、项目ACL、跨进程Session租约、出站限制、报告独立origin、高级负载语义与真实隔离验收仍按原提案分别确认。

下一架构候选仍是“历史读取故障与空数据区分”，但其失败/降级语义未获本批授权，未顺带实施。[原63项审计状态表](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-27-approved-fixes.md)不因此全部变为完成。

回退不需数据迁移；如需回退，只恢复本批6个生产文件及相应类型配置边界，保留前十二批修复。回退会重新暴露并发准备和取消后后台积压风险；外部基线仅用于人工核对，本批未执行回退。
