# 第十六批修复：持久分析回执与同请求防重复

日期：2026-08-28。状态：**用户确认方案 A 后实施，已完成本地隔离验证；未部署，未运行真实模型、JMeter 或远程引擎。**

本批交付持久回执、提交者隔离、相同请求防重复执行和查询能力。**不包含自动续跑、分布式队列、项目 ACL、跨进程 Session 或全局配额。** [实施计划与契约](D:/trae/项目/jmeter-agent/docs/superpowers/plans/2026-08-28-analysis-receipts.md)记录边界和验证任务。

## 一、如何使用与兼容边界

原 `/api/v1/agent/run`、`/api/v1/agent/run/stream` 请求保持可用。只有 `agent_type=analysis`、提供已有 `execution_id`，并显式提供 `request_id` 时，才启用本批能力。未提供标识的旧调用不自动生成回执，也不承诺防重复。

在现有认证方式下提交，例如以下请求体；execution_id 需要替换成真实已登记执行 ID，本批没有发起该请求：

```json
{
  "agent_type": "analysis",
  "execution_id": "existing-execution-id",
  "request_id": "analysis-20260828-001",
  "task": "分析该执行的性能瓶颈"
}
```

查询：`GET /api/v1/agent/analysis-requests/analysis-20260828-001`。仅提交者可读取，管理员不因角色自动获得其他用户的回执。新分析使用新 request_id；不要在网络/存储错误后自动换标识重试。

| 情况 | 实际行为 |
|---|---|
| 新标识 | 原子占用后预检并执行；占用成功即保留标识，包括后续预检失败、忙碌或取消 |
| 相同用户、相同标识和参数，已有最终结果 | 返回历史结果；HTTP/SSE 可互换，不再次创建适配器、读取执行证据、调用模型或归档 |
| 相同标识但 task/context/model/session/execution 等参数改变 | 409；stream/传输方式不计入请求指纹 |
| 当前进程确认仍运行，尚无最终结果 | 202 JSON，仅有 receipt，不伪造 success 字段；SSE 路径也不会新建流执行 |
| 未知结果，或取消/预检失败且没有最终结果 | 409 JSON，携带已有状态；标识不重新取得执行权 |
| 已记录的分析失败 | 可以返回原失败结果，success=false，不自动重试 |
| 回执存储失败 | HTTP 503；SSE 头已发送时发 error，不发成功 done。可能存在已提交的模型/归档副作用 |
| 其他用户或不存在的回执 | 均为 404，避免泄露存在性 |

回执状态包括 running、succeeded、failed、cancelled、unknown。重启后或由其他进程查询尚未结束的记录时显示 unknown，**不表示另一个进程已停止**；不会覆盖其数据库状态。它以后若完成，仍可读取其已提交结果。旧报告不会因当前 JTL、模型配置或会话历史发生变化而重新计算。

## 二、逐项修改说明

### 1. 主数据库增加回执表及可靠请求身份

**【修改内容】** [AnalysisReceiptModel](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/models/db.py:46)增加独立表，保存用户 ID、请求标识、参数 SHA256 指纹、执行 ID、进程代次、阶段、状态、已知归档回执、最终结果和 UTC 时间。对 `(owner_id, request_id)` 设置数据库唯一约束。[请求模型](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/models/schemas.py:394)仅允许登记分析提供 1–64 位 ASCII 字母/数字/下划线/连字符标识。

**【修改原因】** 进程内字典无法跨重启保存结果，也无法靠先查后写避免并发重复执行。仅以 execution_id 作为缓存键还会阻止用户主动重新分析同一执行。

**【影响范围】** 主 DB 新表、请求契约及回执服务。现有业务表字段不变，使用原 init_db 的 additive create_all；不迁移用户数据。用户身份从认证 ContextVar 再查询活动 UserModel 得到，不采信请求体或模型提供的 owner。明确删除用户时级联删除其回执，保持账户删除可用，并避免同名重建账户继承旧报告。

**【风险等级】** 中；优先级高。新增持久数据需要备份和容量管理。没有 TTL/定时清理，也不随执行或项目删除自动清除历史回执；回执是提交者独立持有的历史结果。保留/清理策略需要后续单独治理。

**【验证方式】** 临时 SQLite 的两个服务实例、六个并发 claim 只有一个执行者；冲突参数返回 409，用户隔离、无认证/低权限拒绝、活动身份校验、用户删除及同名重建均验证。重复 init_db 保留原项目与已有回执。测试启用真实 SQLite 外键，不用内存字典替代唯一约束。

**【为什么这是最优方案】** 在批准范围内复用现有主 DB 和认证身份，用唯一约束落实请求身份；不增加队列、缓存组件或跨存储事务。独立 request_id 同时支持重试同一请求和主动创建新分析。

### 2. 持久状态、防重复和不确定结果处理

**【修改内容】** 新增 [analysis_receipts.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/analysis_receipts.py)，集中处理 claim/get/checkpoint/finish、当前代次的运行任务和安全存储错误。状态更新仅允许匹配代次且仍 running 的记录；完成记录不能被后续取消或异常改写。

**【修改原因】** 断线可能发生在提交前，也可能发生在实际提交成功但响应未收到之后。不能一律视为失败重跑，更不能因为进程变化就将其他 worker 的任务标记失败。原参数指纹允许识别冲突，保留标识防止不确定结果被重放。

**【影响范围】** 新回执生命周期，不改变 Fact/Hypothesis/RAG 的持久化规则。正常请求新增少量数据库阶段写入；重复请求只做认证身份和回执查询，不读取 Memory/RAG，不创建模型适配器。无新增 Prompt、上下文块、Tool 调用或自动重试。

**【风险等级】** 中；优先级高。保守 unknown 会要求用户查询/人工核实，而不是自动继续。它不保证所有副作用恰好一次：不同 request_id、没有 request_id 的旧调用和不同提交者仍是不同请求。

**【验证方式】** 完成/失败回执复读、终态不可覆盖、无活跃拥有者变 unknown、取消保留归档 IDs、结果过大/非有限数值拒绝、数据库异常安全消息均验证。模拟数据库已经 commit 后连接抛错，确认成功记录不被降级，旧标识不重获执行权。

**【为什么这是最优方案】** 只保存可确认的阶段和结果，不增加未经批准的恢复协议。通过条件更新保护终态，比失败后清空标识重试更能避免重复模型调用和不明归档状态。

### 3. 分析阶段与应用生命周期同步接入

**【修改内容】** [AnalysisAgent](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/analysis_agent.py:264)在 preparation、model、postprocessing、archive、evaluation 边界等待回执写入，并在归档返回后记录已知 IDs/标志。无绑定回执时 checkpoint 不访问数据库。回执存储错误不被既有后处理异常分支吞掉。[应用生命周期](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/main.py:115)关闭时先停接收/取消回执请求，再由现有分析运行时等待实际线程退出。

**【修改原因】** 仅在 HTTP 返回时保存一份结果无法反映处理中断的位置；只记录“归档开始”也不能保留归档已返回的部分工作。取消请求不等于同步线程结束，必须保留上一批资源所有权规则。

**【影响范围】** 登记分析阶段、取消和清理顺序。preparation 包含原 Fact/历史准备，不增加新的推理步骤。模型、结构化提取、Fact 校验、归档和假设评估的正常先后顺序及判据不变。partial_effects_possible 是保守提示，不能把 cancelled/failed 当作全部副作用已撤销。

**【风险等级】** 中；优先级高。回执数据库故障会停止后续阶段；之前已经提交的知识不会回滚。归档内部仍不是跨库事务，阶段标记不等于逐条操作的事务日志。

**【验证方式】** 六个分析阶段/成功用例验证每处存储故障在下一步副作用前退出、已归档数据可保留、Session/准入名额释放。流关闭、手工取消、停机、重复停机及启动/服务异常清理均回归。既有“取消后真实线程仍占 draining 名额”的测试也在全量中通过。

**【为什么这是最优方案】** 复用现有阶段边界和运行时，不改写归档器、账本或 BaseAgent 循环。只增加必要的 await/checkpoint，不引入第二套队列或同步线程的虚假终止机制。

### 4. HTTP/SSE 共享结果，前端明确状态契约

**【修改内容】** 新增 [回执 API 适配器](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/api/analysis_request_routes.py)，在 [原入口](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/api/routes.py:1281)仅对带 request_id 的请求分流；增加 owner-only GET 查询。[AgentResult](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/types.py:130)增加可选 keyword-only receipt，[既有流桥](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/base.py:537)按需携带该字段。先提交最终回执，再返回 HTTP 完成结果/SSE done；保存之前的 complete 改为 analysis_ready。

**【修改原因】** HTTP 与 SSE 若各自实现去重，换传输方式就可能再次执行；提前发完成事件会把最终回执保存失败误报为成功。Session 忙碌可能在流桥调用 operation 之前被拒绝，也必须留下失败回执。

**【影响范围】** 带标识的 HTTP/SSE、新查询端点及 [前端 API 类型](D:/trae/项目/jmeter-agent/jmeter-agent-frontend/src/api/index.ts)。前端增加 getAnalysisReceipt 和结果/状态联合类型，保留 request_id 原值，编码查询路径；没有自动生成标识、自动轮询、自动重发或新增页面。旧请求响应不额外插入 receipt=null。

**【风险等级】** 中；优先级高。调用方需区分 202 状态与最终分析结果，不能把 202 当作分析成功。SSE 已发头后的存储错误只能通过 error 事件说明，不能修改为 HTTP 503。

**【验证方式】** HTTP→SSE 和 SSE→HTTP 重用同一结果，适配器/执行证据解析/分析函数均只调用一次；验证 202、409、查询隔离、预检失败保留标识、Session 忙碌、执行器启动前后断线、提交前不得 done。前端测试确认 202/409/500/503 不重放，查询只 GET 不 POST。类型检查和全量构建通过。

**【为什么这是最优方案】** 复用原 `_stream_operation`，不增加事件队列或重构所有路由。显式 opt-in 让旧工作流保持兼容，并给客户端完整、可识别的状态。

### 5. 回执敏感信息与容量边界

**【修改内容】** 最终回执只保存结果白名单字段，不额外保存原始 task/context、模型配置、事件或 traceback；最终序列化要求有限 JSON，最大 4 MiB。超限/无法序列化时明确失败，不截断伪造完整结果，不释放标识重新执行。查询返回 `Cache-Control: no-store`。

**【修改原因】** 回执需要复现报告，但无须成为另一份完整对话/Tool 轨迹存储。无限制存储单份结果会放大异常负载；缓存用户专属报告可能扩大敏感数据暴露面。

**【影响范围】** 仅本批持久回执载荷与查询头。最终 output、plan description、Fact/结构化报告仍可能含敏感业务信息，必须按私人报告管理；这不是通用秘密识别或内容脱敏器。超过 4 MiB 的带标识分析不能获得可重放的最终结果；旧不带标识入口没有新增此限制。

**【风险等级】** 中；优先级中。存储总行数没有自动上限/清理，部署前仍需容量与保留策略；显式删除用户会删除其回执，删除/恢复数据库也会影响防重保证。

**【验证方式】** 检查 Context 不出现在回执结果，测试 NaN/超限结果的 unknown 状态及不再执行、owner-only 查询和 no-store 响应头。核心 Prompt/策略表达式与源文件差异检查确保没有新增记忆注入或提示词副本。

**【为什么这是最优方案】** 在新增存储边界限制载荷和访问，不为了回执重构原报告格式或加密/密钥管理体系；避免额外持久化不需要的输入和调试轨迹。

## 三、验收证据

全部测试使用仓库外临时数据库/报告路径、模型替身和内存 ASGI。运行器禁用模型下载并阻止真实 JMeter 子进程；未使用用户数据库、凭据或压测目标。

| 验证 | 本次结果 |
|---|---|
| 存储/请求身份首次红灯 | 11 failed，随后 11 passed |
| HTTP/SSE 首次红灯 | 6 failed；停机用例已通过 |
| 阶段接入首次红灯 | 6 failed |
| 边界复核红灯 | 用户删除、已退出任务状态 2 failed，修复后通过 |
| no-store 回归红灯 | 2 failed，修复后最终全量通过 |
| 最终后端全量 | **951 passed，624 warnings，74.03 秒**；较本批开始新增 35 个用例 |
| 前端全量 | **96 passed，6 个测试文件** |
| Vue/TypeScript 类型检查 | 退出码 0 |
| Vite 构建 | 退出码 0，48.04 秒，输出至仓库外 |
| 独立进程持久化检查 | 第一个 Python 进程提交完成回执/未完成 archive 阶段后退出；第二个进程从同一临时 DB 读到完成报告和 unknown 部分阶段，两种标识均不能重新执行 |
| Python AST | 119 个文件，0 语法错误 |
| 源码模式复扫 | 140 个文件；既有 Tool handler 命中前的校验/确认仍在，未修改 |
| 核心 Prompt/策略 AST 比对 | 7 项完全一致 |
| 源码范围 | 修改 12 个、增加 3 个源码/测试文件；共 10 个生产文件、5 个测试文件，无删除，无依赖或配置文件变化 |

后端新增 100 条 warning 来自新增回执测试运行已有的 datetime.utcnow 默认值；总计还包含原有 LanceDB、Pydantic、multipart 等警告。本批不通过依赖升级消除它们。测试时长不是生产性能基线；本批未测量真实 Token 节省或吞吐提升。

证据目录：[repair16](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair16)。最终日志为 `backend-final.txt`，另含前端测试/类型/构建日志、红灯日志、`restart-write.txt`/`restart-read.txt`、源码基线、差异和复扫结果。

## 四、部署、回退和剩余架构项

启动新版本时由现有 init_db 增加回执表。本批只在临时 DB 验证过建表和重复初始化，**没有对用户数据库执行迁移或部署**。回退前应停止接纳带标识请求并等待/取消当前工作，保留新表及其数据；旧版本不能提供 request_id 语义，不得将带标识请求路由到旧版本，也不能因回退而自动重放历史请求。

阶段回执不是跨 Fact/Hypothesis/RAG 的原子提交日志；unknown 不可直接恢复执行。自动续跑、分布式队列、项目 ACL、跨进程 Session/总配额、出站/报告来源隔离及真实负载验收继续待分别确认。数据库备份恢复、保留期、跨存储删除与容量治理也没有在本批擅自扩大。

在本批回归和复扫范围内未发现新增功能回归；这不证明整个项目没有 Bug，也不表示剩余架构项已完成。
