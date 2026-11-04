# 第十二批修复：分析HTTP/SSE接入已登记执行的Fact/RAG流程

## 范围与结论

本批执行用户对[第十一批兼容方案](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-28-eleventh-pass-fixes.md)的“是”确认：**只有analysis请求显式提供execution_id时启用专用分析流程；省略或传null时保留原行为。** 不新增页面，不改变既有报告页面、执行后TestAnalyzer或WorkflowAgent的产品流程。

生产修改涉及7个文件，扩展7个既有测试文件，新增69项后端、11项前端测试。没有新增依赖、数据库表、全局锁服务或模型重试策略。实施计划见[计划文件](D:/trae/项目/jmeter-agent/docs/superpowers/plans/2026-08-28-analysis-entry.md)。

```text
认证/角色校验 → 请求校验 → DB登记记录与JTL路径检查
  ├─ 无execution_id：原run/run_stream
  └─ 有execution_id：整个操作持有既有Session占用
       → JTL计算Fact → 再校验scope → 历史Fact/假设上下文
       → 原规划和模型/工具循环 → analysis_ready
       → 结构化提取、数字校验、Fact/假设/结论归档、已有假设评估
       → 最终complete或error → SSE仅一次done / HTTP返回完整结果
```

这里的RAG继续使用已有历史Fact/假设注入与LanceDB归档；没有额外引入向量检索节点，也没有把所有报告入口替换成这条流程。

## 1. 可选执行ID与登记证据预检

【修改内容】在[请求模型](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/models/schemas.py:384)加入可选execution_id，仅analysis可携带。新增[登记证据解析](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/analysis_agent.py:144)，核对DB中的执行、计划、项目、终态与context_snapshot，限制到该执行登记的result.jtl。拒绝缺失/空文件、身份冲突、畸形快照和目录/文件重定向；在模型创建及SSE响应头发送前返回明确错误。

【修改原因】从任务文本猜测执行ID、路径、模块或版本，不能建立可靠的事实来源；仅检查“位于reports目录”也不足以防止读到另一执行的文件。不能依赖可能尚未加载的内存执行缓存。

【影响范围】HTTP/SSE请求和AnalysisAgent准备阶段；Prompt中的module来自计划ID，version固定unknown，test_type来自执行时快照，不使用后来编辑的计划配置。Tool权限不变；Memory历史检索使用登记scope；Workflow只增加显式选择的入口；Context不能替换登记身份或路径。项目没有独立业务模块/版本字段，本批不自行推断。

【风险等级】中，优先级高。未完成执行和缺少来源快照的旧记录不能使用新模式，但仍可使用不带ID的原任务模式。允许failed/cancelled终态中已有的有效JTL；配置的reports根目录可以是链接，单个执行目录或result.jtl不能被重定向。

【验证方式】25项证据测试涵盖三种终态、非法ID、冷缓存、计划编辑、缺失/冲突/畸形记录、空文件、重定向路径，以及HTTP/SSE从真实静态JTL到隔离持久化的两条链路。重定向测试模拟realpath解析结果，避免依赖Windows创建符号链接权限。18项路由契约测试验证显式选择、旧字段集合和400/404/409/422错误；8项认证测试确认未登录/viewer在证据查询前被拒绝。

【为什么这是最优方案】在当前约束下，复用既有ID规则、路径校验和登记数据库，比新增路由或猜测元数据更小、更可验证；新模式失败不自动降级到不受来源限制的分析。

## 2. 共享专用编排，保留任务、Context和原循环

【修改内容】新增[run_analysis/run_analysis_stream](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/analysis_agent.py:196)，内部复用analyze_with_rag和原模型循环。专用流程增加私有runner/expected_scope参数以及可选用户任务，旧调用默认路径不变。JTL无可用样本或scope在准备期间改变时，不调用模型或归档。

【修改原因】直接把路由的run替换成analyze_with_rag，会丢失原AgentResult计划、上下文与流生命周期；若内部再次调用已加锁的run，还会被自己的Session占用拒绝。

【影响范围】AnalysisAgent、BaseAgent内部调用以及结果封装。Prompt只追加一次原始用户分析请求并注明不得覆盖登记证据；原三个分析Prompt常量、规划Prompt和安全规则表达式不变。Tool注册/参数/确认机制不变；Memory仍由原模型循环读写；Workflow复用原提取、校验与归档规则；Context仍经原reference-context路径传入。

【风险等级】中，优先级高。实际调用次数仍取决于原规划、ReAct与必要的结构化提取，不能把测试中的固定步骤视为生产调用上限。原大Fact/历史上下文的预算限制没有在本批重构。

【验证方式】17项Agent回归覆盖真实假模型核心、原任务/Context/计划保留、scope改变、零样本、取消和流式结果。两项端到端用例使用真实JTL计算与SQLite/LanceDB写入，固定模拟规划和模型响应；即使context写入其他module/version/path，也不能更换登记Fact身份。

【为什么这是最优方案】共享已有分析算法而不复制一套HTTP版和SSE版；保留旧调用默认值，不改变数值验证、假设状态和Tool职责。

## 3. Session占用覆盖后处理，流关闭释放后台任务

【修改内容】将原Session检查提取为[_run_guarded](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/base.py:171)，将原事件队列提取为[_stream_operation](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/base.py:491)。新分析在准备、模型循环、校验及归档期间都保持占用。公开流方法和路由使用aclosing传递关闭；断开/撤销后的关闭会取消并等待后台协程，释放其队列和占用。

【修改原因】原锁只覆盖run内部模型循环，无法保护外层Fact准备和归档；多层异步生成器若不显式关闭内层，可能留下仍在运行的任务。准备期间的主动取消也不能被内部run初始化清除。

【影响范围】BaseAgent、AnalysisAgent、HTTP/SSE生成器；Prompt和Tool参数不变，既有会话命名空间和Memory保存逻辑不变。Workflow改变的是生命周期边界，Context内容不变。保留原全局模型循环并发限制，没有新建跨进程锁。

【风险等级】中，优先级高。已提交的Fact或知识记录不会因为连接关闭而回滚，客户端也可能收不到最后回执；不自动重试有写入副作用的分析。asyncio.to_thread内已经开始的同步JTL读取不能被强行终止；协程取消会阻止后续模型/归档步骤。主动cancel在阶段边界检查，不承诺抢占同步操作。

【验证方式】准备和归档暂停时，当前实例及相同Session的另一实例均被拒绝；两阶段关闭流均等待worker结束；实际路由的认证撤销测试证明关闭传到最内层任务。另覆盖准备、提取、归档阶段的主动取消，已归档Fact回执保留。全量既有BaseAgent/Memory/认证测试同时通过。

【为什么这是最优方案】复用既有队列和占用集合，只扩大新操作的保护边界；避免复制流驱动、重复获取Session或引入新的调度架构。

## 4. 完成事件与JSON结果反映整个分析流程

【修改内容】新模式将模型循环的中间complete改为analysis_ready，保留preparation/postprocessing阶段事件，直到归档/评估结束才发最终complete或error。SSE只由共享驱动发一次done。AgentResult和专用响应子类增加可选analysis，用Pydantic序列化处理日期及嵌套ValidationResult；报告正文和步骤计数不在analysis内重复复制。原WorkflowAgent响应模型不变，旧通用请求不会新增analysis:null字段。

【修改原因】模型输出成功不代表后续归档成功；过早完成会误导用户。直接json.dumps包含datetime/Pydantic模型的字典会使SSE在结束时失败。内部清空事件列表也会丢失已发送的准备阶段记录。

【影响范围】事件、响应schema、AnalysisAgent及客户端类型；Prompt/Tool不变。Memory账本规则不变；Workflow真实终态与部分归档回执一致；Context不增加整份重复分析结果。客户端应以done为SSE结束标志。

【风险等级】中，优先级高。新增事件仅用于新模式；必须检查顶层success/error及analysis内validation、archive.errors、archive.rejected_reason。沿用原分析语义：编排成功不表示每个数字或假设都验证通过，质量过滤也可能跳过结论归档。并未把数字验证扩展成因果验证。

【验证方式】HTTP/SSE各覆盖正常及归档部分失败；验证嵌套校验对象和computed_at能序列化、原plan_steps保留、归档前没有最终complete/done、失败仍保留fact_archived回执。旧响应键集合保持完全一致。

【为什么这是最优方案】增加一个可选结果层与少量阶段事件，保留原输出和计划；用现有Pydantic处理序列化，不新增JSON转换框架或第二套终止协议。

## 5. 冷缓存场景下保留自动归档scope

【修改内容】[RAGArchiver](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/rag/archiver.py:158)的自动结论/原始报告归档，将已验证Fact.context_scope传入私有参数。未提供该参数的既有手工归档仍使用原查询方式。

【修改原因】新入口从DB解析执行，不保证内存执行缓存有对应记录。原_upsert只从内存查scope，可能把有来源Fact的RAG投影保存成无scope文档，导致后续隔离检索不可用。

【影响范围】RAG文档身份与scope元数据；Prompt和Tool schema不变，调用者不能经新HTTP字段指定scope。Memory的Fact与RAG投影保持相同来源，Workflow不新增步骤或查询，Context来源不变。

【风险等级】低至中，优先级高。修复只影响之后的自动归档，不批量迁移或删除历史无scope文档，不宣称跨存储原子性已经实现。

【验证方式】独立LanceDB回归在执行缓存为空时检查结论和原始报告scope；临时回退该赋值后测试能明确失败，恢复后通过。两项完整入口测试进一步验证DB登记→Fact持久化→两类RAG文档来源一致。

【为什么这是最优方案】使用本次操作已有的可信scope，不补写全局执行缓存，也不为每个投影增加数据库读取；旧手工调用保持兼容。

## 6. 类型化客户端调用与安全的认证重试

【修改内容】在[既有API模块](D:/trae/项目/jmeter-agent/jmeter-agent-frontend/src/api/index.ts:210)添加runAgent、runAgentStream及请求/结果/事件类型。SSE传Cookie、语言与AbortSignal，复用既有单次共享认证刷新；网络/5xx错误不重放POST。刷新前后及释放401响应体后检查会话代次和取消状态；重复401发送既有auth-required事件。类型允许真实事件中的data:null。

【修改原因】客户端原来没有这两个Agent接口的契约。副作用请求不能使用一般网络重试；等待认证或释放响应体时退出登录，不能让旧分析请求在新会话下重放。

【影响范围】仅API模块与测试，不新增UI或改造原runAIWorkflowStream。Prompt、Tool、Memory内容和Workflow算法不变；Context原样发送，不从前端生成登记元数据。

【风险等级】低至中，优先级高。方法返回原Response，调用者需处理非2xx和SSE数据；尚未新增页面消费该入口。认证刷新本身是共享操作，取消当前分析不会取消其他请求依赖的刷新。

【验证方式】新增11项前端测试覆盖新/旧JSON契约、Cookie/语言/AbortSignal、HTTP和SSE的5xx禁止重试、网络失败、重复401、刷新期间取消、预先取消、退出登录后迟到的401，以及释放响应体期间发生退出登录的竞争。最后一项先观察到2次POST失败，再修复为1次。

【为什么这是最优方案】使用项目已有axios/fetch与认证机制，不引入SDK或额外重试库，不改变原页面路径；仅在本次新增方法中补齐异步边界检查。

## 验证记录与重新扫描

| 门禁 | 结果 | 证据 |
|---|---|---|
| 后端隔离全量pytest | **765 passed，524 warnings，63.78s，exit 0** | [日志](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/backend-full.txt) |
| 前端全量Vitest | **6个文件，89 passed，26.90s，exit 0** | [日志](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/frontend-final.txt) |
| 最终Vue/TypeScript检查 | **vue-tsc --noEmit，exit 0** | [日志](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/typecheck-final.txt) |
| 最终Vite生产构建 | **2503个模块，24.62s，exit 0**；输出工作区外，不覆盖原dist | [日志](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/build-final.txt) |
| 新增后端定向回归 | **69 passed，95 warnings，24.65s，exit 0** | [日志](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/integration-final.txt) |
| 红灯证据 | 首批28失败；路由组17失败；边界组10失败；客户端退出竞争1失败 | [首批](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/red.txt)、[契约](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/contracts-red.txt)、[边界](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/boundary-red.txt)、[竞争](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/frontend-race-red.txt) |
| 语法与模式复扫 | **113个Python文件，136个源码文件；无语法错误** | [结果](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/rescan-results.json) |
| Prompt/政策表达式比较 | **7项全部与修改前一致** | [结果](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/prompt-check.json) |
| 生产差异复查 | **7个实现文件、7个既有测试文件；0新增/删除源码文件**；另新增计划/本报告，更新上批索引 | [完整差异](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair12/source-review.diff) |

524条警告来自既有弃用/字段命名提示；新增认证/RAG用例增加了这些代码的执行次数。构建保留第三方VueUse PURE注释提示，以及外部outDir不自动清空的提示，未为消除警告修改依赖。调试中也修正了测试夹具缺少JTL来源字段，以及把Fact时间字段误写成created_at的问题；这些测试错误不算生产缺陷。

逐个复查了生产差异、调用关系、来源解析、锁的进入/退出、生成器关闭、模型/归档失败返回及认证异步边界。模式扫描唯一命中仍为ToolRegistry经过参数和确认校验后的正常handler调用。快照覆盖的配置/依赖文件未变；不把静态模式扫描当作完整语义审计证明。

测试使用工作区外repair12隔离目录、静态JTL、假模型、临时DB和假embedding；runner禁止真实JMeter子进程。本批没有调用真实模型、发起压测、启动Docker/远程引擎，也没有使用用户数据库、凭据或既有JMX进行验证。此前批次记录的误启动事件没有被覆盖。

## 接口使用与状态解释

两个入口分别是POST /api/v1/agent/run和POST /api/v1/agent/run/stream，JSON结构相同：

```json
{
  "agent_type": "analysis",
  "execution_id": "已登记的执行ID",
  "task": "分析本次性能结果，并区分事实与假设",
  "session_id": "可选会话ID",
  "context": "可选参考信息"
}
```

示例ID需替换成实际合法ID；没有执行ID的既有请求不需要迁移。stream字段沿用旧schema，但传输方式仍由URL决定。客户端可调用api.runAgent(data)或api.runAgentStream(data, abortController.signal)。

| 情况 | 结果 |
|---|---|
| 未登录 / viewer | 401 / 403，先于登记查询 |
| 非analysis携带ID、空ID、过长ID | 422 |
| 非法ID / 执行不存在 | 400 / 404 |
| 执行未终止、来源缺失/冲突/畸形、路径不符、文件空 | 409，在模型创建与SSE头前返回 |
| JTL解析后零样本、scope在准备期间改变 | 新分析结果success=false，不调用模型/归档 |
| 模型已完成 | analysis_ready，仅表示中间阶段 |
| 归档部分失败 | 最终success=false，保留Fact及已完成归档回执，不重放 |
| 数字不通过或结论质量过滤 | 按原规则保留Fact/未验证假设，检查validation与archive.rejected_reason，不把编排成功当成结论已验证 |
| 同Session占用 | 失败结果，不夺取原任务队列、不重复执行 |
| 关闭/撤销流 | 停止发送并关闭协程；不保证收到done，也不回滚已经提交的成果 |

## Token、性能与仍未完成的事项

新模式没有为适配HTTP/SSE再调用一遍模型，也没有新增自动模型/工具重试或重复Memory保存；嵌套响应不再重复完整报告。新增的用户任务文字及Fact/历史上下文是这项已批准能力的必要输入，不能声称它比原通用对话使用更少Token。预检读取登记信息、准备时重新检查来源都有成本；未测量生产Token、吞吐和延迟，也不将测试耗时当作性能基准。

**已执行验证范围内没有观察到新增回归；不宣称零Bug、零幻觉、完全防Prompt Injection或整个项目审计完成。** 以下边界仍按原状态表保留：

- 高：真实模型、真实压测与隔离验收，需要明确授权目标和负载上限；本批没有擅自测试。
- 高：出站访问限制、报告独立origin及部署拓扑，需要单独设计确认。
- 高：高级阶梯/尖峰/CSV/稳定性语义，需要产品验收样例，不能猜测参数优先级。
- 中至高：跨存储原子性、持久化幂等任务/恢复日志、项目ACL/配额、跨进程Session锁、完整指标与因果证据映射，未在本批引入。
- 中：大JTL读取/哈希成本、候选评估预算、账本读取失败的降级策略，以及提取/归档阶段的独立资源配额仍未专项优化。现有信号量只限制原模型循环，不是整条分析流水线的总并发配额。
- 中：新入口尚未添加页面消费者，也未迁移历史无scope数据。取消后成果查询/恢复是后续设计问题，不能自动重试掩盖。

历史与未完成状态仍见[原63项状态表](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-27-approved-fixes.md)。本批只关闭上批已获确认的HTTP/SSE接入项，不默认为关闭其他未列出的问题。
