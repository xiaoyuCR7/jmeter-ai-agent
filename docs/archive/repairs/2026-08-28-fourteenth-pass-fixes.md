# 第十四批修复：登记分析严格账本读取

日期：2026-08-28。状态：**已实施并完成本地隔离回归；未部署，未调用真实模型或执行JMeter压测。**

本批实施用户回复“是”确认的[严格读取方案A](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-28-history-read-contract-proposal.md)。仅改变登记分析遇到账本读取故障时的处理：不再将故障当作空历史；其他入口和正常分析目标保持不变。

## 一、结果与边界

| 场景 | 本批行为 |
|---|---|
| 登记分析准备历史时账本失败 | `success=false`，`analysis.phase=history`、`error_code=ledger_read_failed`；保留已计算Fact，不调用模型、不归档，不伪造空历史统计 |
| 归档后的评估读取失败 | `success=false`，`analysis.phase=evaluation`、同一错误码；保留报告、validation、已有archive回执及错误，提示可能已有提交，不重试 |
| 正常无记录或无可用记录 | 仍返回空列表/None，首次分析继续可用，不将“无可用记录”解释成“系统从未存在记录” |
| 损坏单行、身份不符、失效历史JTL | 保留原跳过/None规则，不放宽事实校验、不阻断全部历史 |
| 旧分析入口、管理调用 | `strict`默认False，保持原错误/空结果行为，不由全局开关改变 |
| 取消、Session及分析准入 | 沿用原规则；严格哈希读取取消时，真实线程结束前仍占draining名额 |

调用链：登记分析显式启用内部`_strict_reads=True`，传到Fact历史、Hypothesis历史和模块评估；模块评估继续传到单条假设、新旧Fact、更新后读回及并发冲突后读回。未新增API请求参数、环境配置、数据库表、缓存、自动重试或LLM步骤。

## 二、逐项修改说明

### 1. 账本严格读取边界

**【修改内容】** 新增[read_errors.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/memory/read_errors.py)，提供固定安全消息的`LedgerReadError`及严格读取边界。在[FactLedger](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/memory/fact_ledger.py:214)的load/history和[HypothesisLedger](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/memory/hypothesis_ledger.py:259)的get/list加入可选关键字参数`strict=False`。

**【修改原因】** 原查询异常可能被捕获后返回空值，而初始化/连接异常又可能直接抛出。同类存储故障因发生位置不同产生不同结果，上层无法可靠区分“没有历史”和“历史无法读取”。

**【影响范围】** 覆盖初始化、连接、锁内查询、游标读取和已有列表游标收尾。严格模式的异常保持安全错误身份，日志只记录账本/操作/异常类别。参数校验和空查询快速返回仍在边界外；单行解析错误单独处理。Prompt、Tool权限、Context预算不变；Memory读写模型、SQL条件/排序/limit、Fact哈希、事务不变。Workflow只在登记模式的故障分支提前停止。

**【风险等级】** 中；优先级高。存储暂时不可用会让登记分析明确失败；旧默认入口仍保留其原有宽松处理，本批没有将全项目读取都改成严格模式。

**【验证方式】** 60个故障边界场景覆盖四个真实读取方法的严格/旧模式/取消，以及初始化、连接、execute、fetch/迭代和列表游标进入/退出。迭代测试先产生一条有效记录再失败，证明不会返回部分历史作为成功。扩展临时数据库测试，验证损坏JSON、身份不符、跨scope、零limit、空来源、无记录、旧16位哈希前缀、JTL变化/缺失/OSError仍保持原规则。

**【为什么这是最优方案】** 在当前批准范围内，用一个24行公共边界统一错误身份，不改所有消费者的返回类型，不增加全局状态或迁移。主要差异是包裹既有逻辑及传递参数，没有为整洁而改写查询或持久化实现。

### 2. 评估链完整传播与保留已提交状态

**【修改内容】** [模块/单条假设评估](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/memory/hypothesis_ledger.py:336)接收可选strict参数，逐层传给list/get、新旧Fact.load、条件更新后的get和并发冲突后的get。默认模式不额外传递新参数，以保持原调用方式兼容。

**【修改原因】** 仅修历史准备不能解决归档后评估的读取故障。特别是更新已提交、随后读回失败时，返回空值可能隐藏真实失败；从头重试则可能重复处理已提交状态。

**【影响范围】** HypothesisLedger与FactLedger的评估读取链；不更改同执行自证限制、scope/版本/测试类型匹配、非空样本要求、保守预测判据或条件更新协议。Prompt、Tool、Context不变；Memory中已提交的假设状态保留；Workflow在首次读取故障处停止后续评估，不做全局回滚或重放。

**【风险等级】** 中；优先级高。一次评估可能部分完成，整体失败不表示全部假设均未更新。本批不提供逐条持久回执或恢复任务。

**【验证方式】** 六项临时数据库故障测试分别覆盖模块list、假设get、新Fact、旧Fact、最终get和冲突get，校验更新次数及最终状态。另一个测试让第一条假设成功提交，再让第二条读取失败，确认第一条仍为CONFIRMED、第二条保持UNTESTED，且没有重放更新。原并发条件更新、过早确认防护等测试全部通过。

**【为什么这是最优方案】** 只沿现有调用链传递错误策略，保留已存在的写入保护；避免以读取失败为由引入跨库事务、补偿队列或危险自动重试。

### 3. 登记编排与HTTP/SSE安全失败回执

**【修改内容】** [AnalysisAgent](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/analysis_agent.py:275)仅在登记入口启用内部严格标记。历史准备及评估捕获`LedgerReadError`，形成对应phase及稳定错误码；评估失败消息与先前归档失败合并，已有回执不丢失。

**【修改原因】** 底层抛错后，上层还需要准确表达失败阶段，并防止SQL、数据库路径或驱动错误文本进入客户端。准备失败不能继续生成缺少历史的报告；评估失败不能删除已有提交或将其误报为未发生。

**【影响范围】** 登记分析共用编排、HTTP/SSE结果。HTTP继续使用原200业务失败封装，SSE保留一次最终done。准备失败不进入LLM/Tool/归档，保留已计算Fact；评估失败保留报告、验证和archive信息。既有Prompt、Tool审批、Session、准入实现及取消逻辑未修改；正常Context构造和Memory写入步骤不变，不删除模型阶段已经保存的会话记忆。

**【风险等级】** 中；优先级高。调用者必须区分“分析失败”与“没有任何提交”；错误消息明确提醒评估前可能已有写入。

**【验证方式】** 八项真实HTTP/SSE路由测试注入底层账本execute异常，覆盖Fact历史、假设历史、评估及已有归档错误后的评估，验证安全消息、phase、错误码、模型/归档次数、最终done、回执与Session/名额释放。另测旧直接分析入口仍按原行为运行、空真实账本首次登记分析成功。严格load/history的真实线程取消测试通过。

**【为什么这是最优方案】** 复用既有业务失败封装和编排，不引入新的HTTP状态、降级Prompt、UI状态机或额外模型调用；已发生的部分提交如实保留，不承诺不存在的原子性。

### 4. 前端类型及禁止自动重放

**【修改内容】** [RegisteredAnalysis类型](D:/trae/项目/jmeter-agent/jmeter-agent-frontend/src/api/index.ts:12)增加history/evaluation阶段和ledger_read_failed错误码。扩展既有auth-api测试，验证两个阶段的业务失败不会重放POST/SSE。

**【修改原因】** 新诊断字段应具备明确类型；读取失败发生在部分归档之后时，客户端自动重跑分析可能产生重复副作用。

**【影响范围】** 可选响应类型与测试。前端传输函数、页面、Prompt、Tool、Memory、Workflow、Context均未增加新行为；旧响应仍合法。

**【风险等级】** 低；优先级中。

**【验证方式】** 前端全量6个文件、93项测试通过，vue-tsc与Vite构建退出码0；后端路由测试验证真实失败封装，非只依靠前端模拟数据。

**【为什么这是最优方案】** 扩展可选类型和既有测试即可表达后端新语义，无须新协议版本或客户端恢复机制。

## 三、本轮实际验证

在修改前保存178个源代码/测试/配置文件的外部基线。隔离runner将DB、路径、RAG和模型配置指向repair14/scratch，禁用模型下载并禁止真实JMeter子进程；测试使用模型替身、临时数据和受控线程。没有修改用户数据库、配置文件或JMX，没有部署或执行真实压测。

| 检查 | 实际结果 | 证据 |
|---|---|---|
| 账本读取红灯 | 40失败、20通过：缺少严格接口，旧默认行为通过 | [read-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/read-red.txt) |
| 读取边界及相邻回归 | 91通过 | [read-green.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/read-green.txt) |
| 评估传播红灯 | 7失败：未贯穿严格参数 | [evaluation-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/evaluation-red.txt) |
| 登记集成红灯 | 8失败、2通过：故障仍按空历史继续；兼容对照通过 | [integration-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/integration-red.txt) |
| 读取/评估/登记/取消专项 | 83通过；之后新增的哈希兼容等场景纳入全量 | [contract-green.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/contract-green.txt) |
| 后端全量 | **906通过，524条warnings，59.80秒，退出码0** | [backend-full.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/backend-full.txt) |
| 前端全量 | **6文件、93通过，退出码0** | [frontend-final.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/frontend-final.txt) |
| vue-tsc --noEmit | **退出码0，无诊断输出** | [typecheck-final.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/typecheck-final.txt) |
| Vite构建 | **退出码0，57.58秒**；仅输出到外部目录 | [build-final.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/build-final.txt) |
| 项目复扫 | **116个Python文件解析、138个源文件模式扫描，无语法错误** | [rescan-results.json](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/rescan-results.json) |
| Prompt/策略表达式比对 | **七项均与本批修改前一致** | [prompt-check.json](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/prompt-check.json) |

原后端弃用警告仍存在，包括datetime.utcnow和LanceDB table_names；构建仍有第三方PURE注释位置提示。本批没有为消除警告改依赖或打包策略。前后端测试/构建有并行执行，耗时不是生产性能基准，不能据此推断Agent变快或变慢。

本批源代码/测试变化：8个修改文件、1个新增文件、0删除；其中生产文件5个，全部复核，测试文件4个，复用既有测试文件，没有新增测试文件。另有实施计划、此报告及两份提案状态文档。基线配置和依赖文件没有变化；SQL筛选/排序、哈希及写事务保持。完整差异见[source-review.diff](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair14/source-review.diff)。

模式扫描仍命中ToolRegistry内部的`await tool.handler`。该文件未修改，原参数校验及确认边界保留；不能把字符串命中直接当作权限绕过。静态扫描、测试及差异复核不等于证明项目不存在其他Bug。

## 四、复核结论与未实施事项

在本轮验证范围内，未发现新增功能回归；没有新增Prompt、模型调用步骤、Tool重试、Memory查询、缓存或全局状态。准备阶段故障的测试确认模型调用为0；Fact准备已发生，因此不宣称零成本失败，也未测量生产Token/吞吐优化比例。

仍保留的边界：

- 严格模式仅覆盖登记分析。旧调用继续保留原有宽松故障行为；本批不将所有API的空值统一改成异常。
- 单行无效历史及已知JTL不可用仍跳过；空结果代表没有可用记录，不代表数据库绝无历史。本批也不是所有文件系统故障的健康诊断。
- 评估失败可能已有部分提交，最终回执不是持久任务日志；没有全流程原子事务、幂等恢复或自动补偿。
- 单进程准入、不可强制停止运行中线程、原有同步存储阻塞和大JTL资源成本仍按第十三批边界处理。
- 持久任务回执与恢复、项目ACL/跨进程Session协调、出站限制/报告独立origin、高级负载语义及真实隔离验收，仍需分别确认。

本批不代表[原63项审计](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-27-approved-fixes.md)全部完成。无数据迁移，必要时可按外部基线人工回退本批生产文件，但会重新暴露读取失败被当作空历史的问题；本批没有执行回退、提交或部署。
