> 历史记录：用户已批准下述四个方案。当前修复和验证状态请看 [批准后修复报告](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-27-approved-fixes.md)；本文保留第一批当时的结果。

# JMeter AI Agent 阶段修复记录与架构确认单

**状态：第一批局部补丁已完成并验证；整个审计清单尚未全部修复。架构改动未实施，等待用户确认。**

日期：2026-08-27。沿用原业务目标、Agent 角色、Vue/FastAPI/JMeter/SQLite/LanceDB 技术栈；未新增生产依赖、未改端口/凭证/真实数据库、未执行真实压测。

上一轮依据：[完整审计报告](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/audit/JMeter-AI-Agent-审计报告.md)。本次 23 个修改单元与 63 项审计条目不是一一对应关系，不能当作 63 个问题已全部关闭。

## 验证与改动范围

- 后端：**116 passed，43 warnings**；原有 59 项测试保留，修正了“失败仍应总成功”的错误断言，新增 57 项回归参数场景。
- 前端：**15 passed / 2 files**；包含原有 8 项测试与 7 项控制流回归；vue-tsc --noEmit 通过。
- Vite 生产构建通过，输出在项目外 repair/frontend-dist；依赖仍有 Sass/注释告警，没有借此升级依赖。
- 扫描 126 个后端/前端源文件，并对含测试的 94 个 Python 文件做 AST 检查；语法错误为 0。模式扫描和 AST 检查不等价于“没有任何 Bug”。
- 25 个现有文件修改（20 个生产代码文件、5 个已有测试文件），新增 4 个测试文件；没有删除源码。此文档另计。
- 修改前备份 147 个选定源码/配置/测试文件到项目外；配置文件哈希不变。未初始化 Git、未提交或发布。
- 所有模型、网络和进程动作均 mock；LanceDB/临时文件的实际读写限于测试目录。前端回归执行实际 setup 函数，不宣称真实浏览器验收。

### 二次扫描中的跨模块修正

发现并关闭：纯摘要会话被 bool 判断忽略；最终总结未带安全规则；取消被普通错误重规划；同批工具取消后仍继续；短期窗口淘汰当前任务。均先补失败测试，再修复并重跑全量。

### Token 与性能结论的边界

已去除事实 JSON 缩进、合法思考计划的额外格式重试、重复系统消息入库和未变化会话写库。恢复必要历史与共享安全规则也会增加部分模型输入。没有真实模型 usage/耗时基准，因此不承诺总 token 或端到端延迟下降百分比；全部 40 工具 schema 和模型相关预算问题仍在待办。

## 逐项修改记录

### R01 B01：调度器初始化和零冷却配置

**【修改内容】** 把不可达的回调、计数器、信号量初始化移入构造函数；显式 0 不再被默认冷却/等待值覆盖。

**【修改原因】** 首次资源检查会 AttributeError；配置 0 秒也不能按用户设置生效。

**【影响范围】** 调度器及 execute_test；Prompt 无变化；Tool 资源检查恢复；Memory/Context 无变化；Workflow 仍按既有资源检查运行。

**【风险等级】** 低

**【验证方式】** TestSchedulerReadiness：首次检查无需实例化真实 JMeter，显式零配置正确。

**【为什么这是最优方案】** 只移动初始化并区分 None/0，保留单例和现有调度策略。

### R02 B04：JMeter JSONPath 与变量引用

**【修改内容】** 使用 JSONPostProcessor.jsonPathExprs；path/header/query/body 生成 ${id}，不再生成 ${{id}}。

**【修改原因】** 错误属性和变量语法导致关联请求失效。

**【影响范围】** JMX 生成器；Prompt/Memory/Context 无变化；Tool 输出工件修正；Workflow 请求关联恢复。

**【风险等级】** 低

**【验证方式】** test_json_extractor_uses_jmeter_property、四种位置的 test_bindings_emit_single_pair_of_braces。

**【为什么这是最优方案】** 按已有 JMeter 组件格式修正字面值，不更换生成方案，也不批量重写历史 JMX。

### R03 B05：同步修复 JMX properties 消费方

**【修改内容】** 修改工具、调优和自愈统一读取/更新字典属性，保留原节点；JMeter bool 属性使用布尔值。

**【修改原因】** 解析器输出 dict，消费方却按属性对象列表访问，导致正常 JMX 修改失败。

**【影响范围】** jmx tools、AutoTune、SelfHeal；Prompt/Memory/Context 无变化；Tool 参数接口不变；Workflow 原修改阶段恢复。

**【风险等级】** 中

**【验证方式】** 真实生成→解析→修改→重建测试；调优执行器为 mock；自愈提取器读写测试。

**【为什么这是最优方案】** 修正错误消费者，不改变正确的解析器/构建器契约。完整调优目标与自愈证据验收尚未修复。

### R04 B08：XML JTL 样本识别

**【修改内容】** 读取 sample/httpSample，保留旧 sampleResult 兼容识别。

**【修改原因】** 原解析器把标准 XML 结果读成零样本。

**【影响范围】** ReportParser、SLA、FactLedger 的输入统计；Prompt 文本无变化，但获得正确事实；Tool/Workflow 下游结果修正；Memory 可归档正确样本。

**【风险等级】** 中

**【验证方式】** 三种 XML 节点 fixture，失败请求保持 100% 错误率并不能通过 SLA。

**【为什么这是最优方案】** 仅修正识别条件，保留统计结构；未把整个报告解析器重写。

### R05 B08/B10：SLA 不再对缺失数据假通过

**【修改内容】** 零样本直接失败；无法找到的指标形成明确违规，不再静默跳过。

**【修改原因】** 缺失样本或拼错指标不能证明性能达标。

**【影响范围】** SLAEngine、报告/CI 消费者；Prompt/Context 格式不变；Tool/Workflow 获得真实失败；Memory 不应再把此结果当作通过。

**【风险等级】** 中

**【验证方式】** test_empty_report_cannot_pass_sla、test_unknown_sla_metric_is_failure。

**【为什么这是最优方案】** 保持 PASS/FAIL/WARN 接口与合法阈值语义，只关闭缺失证据的误通过路径。label 保持展示字段。

### R06 B22：事实表可序列化且减少 JSON 空白

**【修改内容】** 改为 Pydantic model_dump_json，排除项不变，datetime 正确编码，取消缩进空白。

**【修改原因】** json.dumps(model_dump()) 遇到正常 datetime 就失败。

**【影响范围】** AnalysisAgent Prompt 构造；Tool/Memory/Workflow 接口不变；Context 保留所有原字段与数值。

**【风险等级】** 低

**【验证方式】** test_fact_prompt_serializes_datetime_without_losing_metrics。

**【为什么这是最优方案】** 复用现有模型序列化，不添加自定义编码器或截断事实。

### R07 B13：WebSocket 锁与清理

**【修改内容】** 心跳超时在锁外断开；不取消当前心跳自身；广播网络发送不持有全局连接锁。

**【修改原因】** 非重入锁递归获取导致死锁，慢网络还会占住全局锁。

**【影响范围】** WSManager/实时进度；Prompt/Tool/Memory/Context 无变化；Workflow 事件内容不变。

**【风险等级】** 中

**【验证方式】** 超时断开与广播锁状态测试，无真实网络。

**【为什么这是最优方案】** 调整锁持有范围，不替换通信协议。前端 1006 关闭码问题 B14 尚未修改。

### R08 T02：工具业务失败与空 error

**【修改内容】** 识别 success=false、valid=false、failed/cancelled 状态；error=null 不再误判失败；保留原始 data。

**【修改原因】** 原实现只按 error 键是否存在判断，真假成功均可能错误。

**【影响范围】** ToolRegistry、所有 Agent 观察；Prompt 获得正确状态；Memory 存储真实结果；Workflow/Context 失败传播修正。

**【风险等级】** 中

**【验证方式】** 五类业务失败与 error=null 回归；取消状态分类为 CANCELLED。

**【为什么这是最优方案】** 兼容原 dict 返回格式，未引入全工具结果 schema 迁移。

### R09 W01：Agent 执行状态和完成条件

**【修改内容】** 空计划走真正 ReAct；工具步骤无调用不算完成；迭代耗尽、步骤失败不返回总成功；失败总结不再预设任务完成。

**【修改原因】** 原本没有完成操作也能 success=true。

**【影响范围】** BaseAgent、SSE 结果消费者；Prompt 总结措辞修正；Tool/Memory/Context 结果更真实；Workflow 停在无法修复的失败处。

**【风险等级】** 中

**【验证方式】** 空计划、有/无工具、失败、最大迭代、ReAct 上限和正常路径均覆盖。

**【为什么这是最优方案】** 保留 AgentResult 字段与现有循环，只修正状态判定。业务工件是否真正注册仍需 A01 的统一服务验收，故 W01 整体未宣称完全关闭。

### R10 W02：重规划和重试预算

**【修改内容】** 用新步骤替换失败步骤，不再重跑旧步骤；每次重试都消耗运行迭代预算。

**【修改原因】** 原索引没有前进，且 continue 可绕开预算计数。

**【影响范围】** BaseAgent/Planner；Prompt 角色不变；Tool 减少重复调用；Memory 少记录重复结果；Workflow/Context 保留当前新计划。

**【风险等级】** 中

**【验证方式】** bad→replacement 恰好两次；max_iterations=1 时重试调用仅一次。

**【为什么这是最优方案】** 修正切片和计数位置，无新状态机。跨重规划的副作用幂等性仍需执行服务设计。

### R11 P01：共享安全规则贯穿角色、规划与总结

**【修改内容】** 专用角色保留原职责提示，同时组合共享规则；规划、最终总结同样带安全规则；参考数据与指令区分。

**【修改原因】** 原专用 Prompt 替换基础规则，最终总结又没有 system 约束。

**【影响范围】** BaseAgent、TaskPlanner、专用 Agent、总结 Prompt；Tool 约束说明一致；Memory 摘要按数据使用；Workflow/Context 角色不变。

**【风险等级】** 中

**【验证方式】** 专用角色规则保留和最终总结 system policy 测试；正常 Agent 测试通过。

**【为什么这是最优方案】** 共享一份短规则，避免复制整份基础角色 Prompt。Prompt 防护不是权限控制，T01/S01/S03 仍待实施。

### R12 M01：上下文传递与当前任务保留

**【修改内容】** ReAct/思考阶段使用有界历史、前序工具结果；当前任务与约束独立保留，不随历史窗口淘汰；同一当前消息只注入一次。

**【修改原因】** 原 ReAct 只看到当前步骤；简单增加历史后，限长又可能丢掉原任务。

**【影响范围】** BaseAgent；Prompt/Context 补回必要数据；Tool 可使用前序 ID；Memory 仍有限长；Workflow 依赖步骤保持可执行。

**【风险等级】** 中

**【验证方式】** 原任务/上下文/历史/前序结果哨兵测试；3 条窗口淘汰后当前任务仍在且不重复。

**【为什么这是最优方案】** 在现有消息模型内保留运行输入，不引入新会话协议。必要输入可能增加 token，不把正确性修复冒充整体降本。

### R13 M02：短期窗口、滚动摘要和脏写标志

**【修改内容】** deque 限长；摘要携带上一份摘要，按 user 数据传入；未变化会话跳过写库；保存期间新修改仍保持 dirty。

**【修改原因】** 窗口上限未生效，旧摘要丢失，重复保存浪费 I/O；摘要不应成为更高权限指令。

**【影响范围】** ConversationMemory；Prompt 摘要指令增强；Tool 结果保存接口不变；Workflow 不新增压缩调用；Context 保留当前任务另由 R12 保障。

**【风险等级】** 中

**【验证方式】** 限长、滚动摘要、角色、重复写库、持久化往返测试。

**【为什么这是最优方案】** 兑现原有滑动窗口设计，不添加数据库表或长期记忆框架。摘要仍有长度/内容截取限制，不能承诺保留全部旧细节。

### R14 M02：分析会话连续性与纯摘要会话

**【修改内容】** AnalysisAgent 转交框架 kwargs 中的 session_id；加载结果按 is not None 判断，纯摘要会话不再被忽略。

**【修改原因】** 参数被接收却未传给基类；空消息容器的 bool 值掩盖了有效摘要。

**【影响范围】** AnalysisAgent/BaseAgent/Memory；Prompt 职责不变；Tool 无接口变化；Workflow 多轮延续；Context 恢复摘要。

**【风险等级】** 低

**【验证方式】** test_analysis_agent_forwards_session_id、test_session_with_only_a_summary_is_loaded。

**【为什么这是最优方案】** 修复构造和存在性判断，不改变 session_id 或持久化格式。

### R15 W03：读取 ReAct 嵌套结果

**【修改内容】** 兼容直接结果和 tool_results 中的原始 payload，去重计划路径生成的 ID。

**【修改原因】** 原提取器只读顶层，导致已有结果在 UI 返回为空。

**【影响范围】** WorkflowAgent 返回字段；Prompt/Memory/Context 不变；Tool payload 不变；Workflow 结果提取修正。

**【风险等级】** 低

**【验证方式】** 直接/嵌套两种结构及重复结果测试。

**【为什么这是最优方案】** 只修正消费者。文件名仍不等于数据库计划注册，假进度和 A01 业务生命周期问题尚未关闭。

### R16 S02：CSV 路径边界

**【修改内容】** 上传、列表、预览、删除统一对 realpath 做 commonpath 检查，拒绝 ../ 和跨根路径，列表跳过越界链接。

**【修改原因】** 字符串前缀判断不是目录边界；project_id 拼接也能逃逸。

**【影响范围】** data_routes 文件访问；Prompt/Memory/Context 无变化；Tool/API 输入格式不变；Workflow 合法 CSV 操作保留。

**【风险等级】** 中

**【验证方式】** 越界预览/删除/上传/列表拒绝；目录内正常预览通过。仅临时占位文件。

**【为什么这是最优方案】** 复用标准路径函数且不增加依赖。没有宣称整个文件系统已安全：JMX/通用文件工具、授权、TOCTOU 和 Windows 特殊文件名仍需治理。

### R17 B11：缺失报告目录的失败报告

**【修改内容】** report_dir=None 时不拼接路径；HTML 路径辅助函数返回 None，HTML 接口正常 404。

**【修改原因】** 失败执行最需要诊断时却产生 TypeError。

**【影响范围】** 报告 API/ReportParser；Prompt/Tool/Memory/Context 无变化；Workflow 可读取失败元数据和空摘要。

**【风险等级】** 低

**【验证方式】** 失败执行无 report_dir 返回报告，HTML 缺失返回 404。

**【为什么这是最优方案】** 复用现有可选路径字段，无新响应类型。缺失 start_time 等其他历史脏数据没有纳入本项完成声明。

### R18 W04：已登记执行的取消清理

**【修改内容】** 捕获 CancelledError，在释放并发槽前调用现有进程终止逻辑并删除登记，再传播取消。

**【修改原因】** CancelledError 不属于普通 Exception，原路径遗漏清理。

**【影响范围】** JMeterExecutor/并发 guard；Prompt/Memory 无变化；Tool 超时取消可触达清理；Workflow/Context 状态不再伪装继续运行。

**【风险等级】** 中

**【验证方式】** 全 mock 进程验证 kill 调用、登记移除、清理时并发槽仍持有。

**【为什么这是最优方案】** 复用现有清理机制，不擅自替换 Windows 启动方式。启动中取消、PID 识别和整个子进程树终止仍需架构确认及真实环境验收。

### R19 B23：保存失败禁止启动旧计划

**【修改内容】** handleSave 返回成功/失败；handleRun 只在保存成功后执行。

**【修改原因】** 原 catch 吞掉错误，上层仍会运行磁盘上的旧计划。

**【影响范围】** TestCaseEditor；Prompt/Memory/Context 无变化；执行 API 参数不变；Workflow 保存→执行的前置条件恢复。

**【风险等级】** 低

**【验证方式】** 提取实际 setup 函数：保存失败 executeTest=0 次，成功=1 次。

**【为什么这是最优方案】** 返回布尔结果避免点击保存产生未处理异常。编辑器无损往返 B03 尚未修复。

### R20 B12：SSE 分帧和终态

**【修改内容】** 保留换行边界，EOF 消费完整尾行；检查 HTTP 状态；缺少完成事件的 EOF 显示错误并结束运行态。

**【修改原因】** 过滤空行后 pop 把完整最后一帧当成半行，页面可能永久运行。

**【影响范围】** AIChat 工作流页面；Prompt/Tool/Memory/Context 无变化；Workflow 前端终态修正。

**【风险等级】** 低

**【验证方式】** 整帧、分块、CRLF、无尾换行、异常 EOF、HTTP 401；全部为实际函数。

**【为什么这是最优方案】** 修正现有读取循环，不替换 SSE 协议。测试不代表浏览器端到端或断线后服务端作业取消已验收。

### R21 M03：冷启动检索、查询约束和版本过滤

**【修改内容】** 冷启动从 LanceDB 查询候选；无 embedding 时保留文字查询；合并 module/type/version 条件；降级分支使用相同精确过滤。

**【修改原因】** 缓存不是全量知识库；原回退忽略 query；多次 where 与遗漏 version 可能丢约束。

**【影响范围】** LanceRAGStore/检索消费者；Prompt 接收更相关知识；Tool 检索接口不变；Memory/Context 减少无关知识；Workflow 不增加模型请求。

**【风险等级】** 中

**【验证方式】** 本机真实 LanceDB、假 embedding：冷启动、无关查询、引号数据、三类过滤；连同 CRUD/修复测试 16 项通过。

**【为什么这是最优方案】** 使用现有表和过滤能力，不更换向量库。分页前排序、FTS 索引、零向量持久修复和写失败一致性仍未解决。

### R22 K02：减少合法思考计划的无效格式重试

**【修改内容】** 合法 tool_name=null 计划直接接受；格式解析失败返回空计划，交由真实 ReAct 回退。

**【修改原因】** 原 planner 把合法思考步骤当格式错误，多请求一次模型；错误文本又被包装成成功思考步骤。

**【影响范围】** TaskPlanner/BaseAgent；Prompt 允许 null，与原 schema 一致；Tool 不被强制误用；Memory/Context 不变；Workflow 减少无意义重试。

**【风险等级】** 低

**【验证方式】** 合法思考计划只调用一次 chat；非法文本不能变成有效计划。

**【为什么这是最优方案】** 纠正重试条件，不删除必要的格式错误重试。固定工作流整体 LLM 往返和全部工具 schema 成本仍待优化。

### R23 取消状态跨 Tool 与 Workflow 传播

**【修改内容】** cancelled 使用独立错误分类，不重规划；用户取消后同一模型响应中的后续工具不再执行。

**【修改原因】** 普通失败传播修复后，若把取消也当可恢复错误，会重新触发操作。

**【影响范围】** ToolRegistry/BaseAgent；Prompt 无变化；Tool 调用提前停止；Memory 保留已发生结果；Workflow 终态 CANCELLED；Context 不再用于新的重规划。

**【风险等级】** 中

**【验证方式】** 取消工具不调用 replan；同批第二个工具不执行；正常和错误调用仍通过。

**【为什么这是最优方案】** 使用已有 CANCELLED 枚举，在最小责任层停止，而非增加新状态协议。

## 原审计 63 项状态表

“本项局部机制已修”只覆盖报告列出的具体错误，不代表所在模块或全部安全能力已经完备。“未处理”也不是需要架构改造的同义词；这些局部项将在确认后继续逐项处理。

| 编号 | 审计项 | 当前状态 |
|---|---|---|
| A01 | 三条执行入口没有统一的业务服务 | 架构/契约关联项：未实施，待确认方案 |
| A02 | SQLite、内存字典、磁盘扫描形成多套事实来源 | 架构/契约关联项：未实施，待确认方案 |
| A03 | 有防污染分析模块，但主报告链路没有使用它 | 架构/契约关联项：未实施，待确认方案 |
| B01 | 资源调度器的初始化代码位于 return 后 | 本项局部机制已修并测试 |
| B02 | 登录/刷新令牌存储与 ORM 模型不一致 | 架构/契约关联项：未实施，待确认方案 |
| B03 | 用例编辑器不能无损保存现有计划 | 未处理，仍在后续清单 |
| B04 | JMX 生成的 JSONPath 属性名和变量引用错误 | 本项局部机制已修并测试 |
| B05 | AutoTune、自愈和部分工具误读 JMX properties 类型 | 本项局部机制已修并测试 |
| B06 | 基础生成器错误处理带路径的 base_url | 未处理，仍在后续清单 |
| B07 | 生成接口接受的参数并未全部落入 JMX | 未处理，仍在后续清单 |
| B08 | XML JTL 被解析为零请求，且空结果可通过 SLA | 本项局部机制已修并测试 |
| B09 | 吞吐量时间窗口和分桶会扭曲结果 | 未处理，仍在后续清单 |
| B10 | 未知 SLA 指标被静默跳过并判为通过 | 本项局部机制已修并测试 |
| B11 | 失败/取消执行的空 report_dir 导致报告接口异常 | 本项局部机制已修并测试 |
| B12 | SSE 最后一帧被前端留在缓冲区 | 本项局部机制已修并测试 |
| B13 | WebSocket 心跳超时会死锁 | 本项局部机制已修并测试 |
| B14 | 前端 WebSocket 使用禁止主动发送的关闭码 1006 | 未处理，仍在后续清单 |
| B15 | 前后端存在实际可点击的 API 契约断点 | 未处理，仍在后续清单 |
| B16 | OpenAPI 导入的语义支持不足且会假成功 | 未处理，仍在后续清单 |
| B17 | Cron 的星期语义错误且调度任务串行阻塞 | 未处理，仍在后续清单 |
| B18 | 异常和漂移输出包含数学/序列化错误 | 未处理，仍在后续清单 |
| B19 | CI 示例不能可靠阻断失败 | 未处理，仍在后续清单 |
| B20 | 多环境、执行错误和部分测试参数未可靠持久化 | 架构/契约关联项：未实施，待确认方案 |
| B21 | Windows 进程跟踪和分布式完成判断不可靠 | 架构/契约关联项：未实施，待确认方案 |
| B22 | 事实增强分析在构造 Prompt 时就发生序列化异常 | 本项局部机制已修并测试 |
| B23 | 编辑器保存失败后仍然启动压测 | 本项局部机制已修并测试 |
| P01 | 专用 Prompt 覆盖了基础安全规则 | 本项局部机制已修并测试 |
| P02 | 分析提示要求超出观测数据的根因和容量结论 | 未处理，仍在后续清单 |
| P03 | 文档按字符截断且 Prompt 分散漂移 | 未处理，仍在后续清单 |
| T01 | 确认标志和 JSON Schema 没有被执行层强制落实 | 架构/契约关联项：未实施，待确认方案 |
| T02 | 业务失败返回值被包装成工具成功 | 本项局部机制已修并测试 |
| T03 | 原生 Tool Calling 的结果关联信息被丢弃 | 架构/契约关联项：未实施，待确认方案 |
| T04 | MCP 路径绕过 Registry，且 SSE 传输不符合声明版本 | 架构/契约关联项：未实施，待确认方案 |
| M01 | ReAct 实际没拿到原任务、会话历史和上一步结果 | 本项局部机制已修并测试 |
| M02 | 会话容量限制和滚动摘要不可靠 | 部分修复：窗口、摘要衔接、会话和重复写入已修；摘要截取与长期保留策略未全部处理 |
| M03 | RAG 冷启动检索与无 embedding 回退返回错误结果 | 部分修复：冷启动/查询/版本过滤已修；索引、分页排序及降级完整性未全部处理 |
| M04 | RAG 降级并非持久化 JSON，写入失败仍可报成功 | 架构/契约关联项：未实施，待确认方案 |
| M05 | 知识标识和来源隔离不足 | 架构/契约关联项：未实施，待确认方案 |
| M06 | 假设会被过早确认，数字校验不能承担完整防幻觉职责 | 未处理，仍在后续清单 |
| W01 | Agent 的成功判定不代表任务完成 | 部分修复：循环与步骤状态已修；业务产物注册和任务验收仍依赖统一服务 |
| W02 | 重规划重复执行失败旧步骤 | 本项局部机制已修并测试 |
| W03 | Workflow 的结构化结果与流式进度脱节 | 部分修复：嵌套结果提取已修；数据库计划身份与真实阶段流式仍未统一 |
| W04 | 取消/工具超时不能可靠停止 JMeter 子进程 | 部分修复：已登记进程取消及 Agent 取消已修；启动期间取消/进程树/断流闭环未完全验收 |
| W05 | 调优/自愈缺少可信的验收标准 | 未处理，仍在后续清单 |
| K01 | 所有 40 个工具 schema 每轮完整暴露 | 未处理，仍在后续清单 |
| K02 | 先规划、逐步 ReAct、总结和重规划重复花费 | 部分修复：合法计划无效格式重试已移除；固定工作流多轮模型调用仍待优化 |
| K03 | 现有截断策略无法可靠控制真实上下文 | 未处理，仍在后续清单 |
| F01 | 同步 CPU/磁盘操作阻塞 async 事件循环 | 未处理，仍在后续清单 |
| F02 | JTL 重复解析且内存随样本数增长 | 未处理，仍在后续清单 |
| F03 | 任务、LLM 并发和背压并未形成统一预算 | 架构/契约关联项：未实施，待确认方案 |
| F04 | 轮询、网络失败和客户端生命周期存在额外成本 | 未处理，仍在后续清单 |
| S01 | 业务、MCP 和 WebSocket 缺少统一认证/授权 | 架构/契约关联项：未实施，待确认方案 |
| S02 | 文件路径边界可被绕过 | 部分修复：仅 CSV 上传/列表/预览/删除边界已修；其他文件入口未全部修复 |
| S03 | JMX 是可执行程序，当前没有对应沙箱边界 | 架构/契约关联项：未实施，待确认方案 |
| S04 | Shell 拼接以及远端地址缺少严格验证 | 未处理，仍在后续清单 |
| S05 | 凭证与测试内容的暴露面过大 | 架构/契约关联项：未实施，待确认方案 |
| S06 | 身份失效、会话归属和资源配额不足 | 架构/契约关联项：未实施，待确认方案 |
| C01 | 大路由、弱类型和多份序列化使契约易漂移 | 未处理，仍在后续清单 |
| C02 | 异常吞并、兼容补丁和依赖声明掩盖真实问题 | 未处理，仍在后续清单 |
| C03 | 测试结构偏单模块，缺少关键用户旅程和安全契约 | 部分修复：新增关键回归测试；登录、真实浏览器、标准 MCP 和真实 JMeter 验收仍缺失 |
| X01 | 先形成可信的 JMeter 闭环，再增加 Agent 数量 | 演进建议，未实施 |
| X02 | 建立可观测性与评测基线 | 演进建议，未实施 |
| X03 | 按实际规模增加隔离和分布式能力 | 演进建议，未实施 |

## 必须先确认的跨模块方案

**推荐渐进整合现有服务，不重写框架、不增加多 Agent、不立即引入消息总线或分布式向量库。**

| 方案 | 影响范围 | 推荐做法 | 利弊与推荐理由 |
|---|---|---|---|
| 统一身份、授权与工具确认 | 登录/令牌 ORM、REST、前端、WS、MCP、文件与模型配置 | 修复现有身份模型，复用现有认证体系；危险操作的确认由服务端校验并绑定具体工具参数/工件，而非模型自报 confirmed | 优：真正阻止未授权执行；代价：前端登录与客户端认证方式会改变，旧令牌/会话需要兼容与迁移。只补 Prompt 无法解决权限问题 |
| 统一计划和执行生命周期 | Web、Agent、MCP、定时任务、数据库、缓存、SSE/WS | 让现有入口调用同一领域服务，统一计划 ID、运行记录、环境快照、工件 hash、取消与完成标准；保留现有外部字段并做兼容 | 优：不同入口得到一致记录与可验收结果；代价：涉及多模块协调、历史数据恢复和副作用幂等，不能当成单点补丁 |
| 保留 JMeter 能力的执行隔离 | Windows/Unix 启动、分布式执行、JMX/CSV/日志、部署说明 | 保留脚本能力，使用受限工作目录和低权限执行单元，可靠跟踪/终止进程树；按信任和批准区分计划 | 优：限制恶意/失控 JMX 的影响；代价：需要确认部署模式和受信脚本需求，路径/进程行为会改变。不能简单删除 JSR223/BeanShell 来假装安全 |
| 规范消息与长期记忆契约 | AIChatMessage、OpenAI/Anthropic adapters、ToolRegistry、MCP、Memory、RAG/Fact/Hypothesis | 明确 tool_call_id/role/结构化结果；标准 MCP 会话与传输；知识增加项目、环境、执行来源隔离，旧记录不能擅自归属 | 优：避免结果串线和知识串用；代价：跨供应商协议适配、数据迁移与旧客户端兼容，需要分步验证和回滚 |

备选一：只继续局部补丁，跨模块方案不实施。改动较小，但主业务未认证、MCP 非标准、执行记录不一致等高风险仍在，不能作为整体修复完成。

备选二：整体框架重写。短期改动和迁移风险最高，不符合 Minimal Change，不推荐。

### 确认后的执行顺序

1. 按仍未修复的高风险局部项补回归，优先 B03 编辑往返、B06/B07 生成语义、B09 统计、B15/B16 路由与导入、B17/B18 数学/调度错误。
2. 先落实认证/授权与确认边界，再整合执行生命周期，之后处理真实进程隔离和标准 MCP/消息契约。
3. 最后做长期知识迁移、事实/假设验收、模型预算与固定工作流的调用优化；保持功能正确率与工件语义。
4. 每阶段继续失败测试→最小补丁→定向/全量回归→影响记录；有真实外部调用需求时先明确隔离环境和目标。

## 尚不能作出的保证

- 不能声明整个项目“没有 Bug / 没有 Prompt Injection / 没有 Token 浪费”；尚有明确未修复项。
- 不能把 116+15 项测试通过当成所有功能已验收；未做真实模型、真实 JMeter、真实浏览器和 MCP SDK 端到端验证。
- B03 编辑器保存仍可能改变负载/断言，B02 登录存储仍有问题，S01 主业务认证仍未统一；当前版本仍不应暴露给不可信网络或直接开展无人值守生产压测。
- 向量库 fallback 的持久化、写失败一致性、假设过早确认等尚未修复；不要把现有知识结果视为自动验证的真相。

## 变更文件与验证工件

- [jmeter-agent-backend/app/api/data_routes.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/api/data_routes.py)
- [jmeter-agent-backend/app/api/routes.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/api/routes.py)
- [jmeter-agent-backend/app/services/ws_manager.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ws_manager.py)
- [jmeter-agent-backend/app/services/jmeter/executor.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/executor.py)
- [jmeter-agent-backend/app/services/jmeter/jmx_generator.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/jmx_generator.py)
- [jmeter-agent-backend/app/services/jmeter/report_parser.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/report_parser.py)
- [jmeter-agent-backend/app/services/jmeter/sla_engine.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/sla_engine.py)
- [jmeter-agent-backend/app/services/jmeter/test_scheduler.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/test_scheduler.py)
- [jmeter-agent-backend/app/services/ai/agent/base.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/base.py)
- [jmeter-agent-backend/app/services/ai/agent/memory.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/memory.py)
- [jmeter-agent-backend/app/services/ai/agent/planner.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/planner.py)
- [jmeter-agent-backend/app/services/ai/agent/tool_registry.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/tool_registry.py)
- [jmeter-agent-backend/app/services/ai/agents/analysis_agent.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/analysis_agent.py)
- [jmeter-agent-backend/app/services/ai/agents/autotune_agent.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/autotune_agent.py)
- [jmeter-agent-backend/app/services/ai/agents/selfheal_agent.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/selfheal_agent.py)
- [jmeter-agent-backend/app/services/ai/agents/workflow_agent.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agents/workflow_agent.py)
- [jmeter-agent-backend/app/services/ai/rag/store.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/rag/store.py)
- [jmeter-agent-backend/app/services/ai/agent/tools/jmx.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/tools/jmx.py)
- [jmeter-agent-backend/tests/test_agent_base.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_agent_base.py)
- [jmeter-agent-backend/tests/test_executor.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_executor.py)
- [jmeter-agent-backend/tests/test_fact_ledger.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_fact_ledger.py)
- [jmeter-agent-backend/tests/test_memory_persistence.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_memory_persistence.py)
- [jmeter-agent-backend/tests/test_rag_store.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_rag_store.py)
- [jmeter-agent-frontend/src/views/AIChat.vue](D:/trae/项目/jmeter-agent/jmeter-agent-frontend/src/views/AIChat.vue)
- [jmeter-agent-frontend/src/views/TestCaseEditor.vue](D:/trae/项目/jmeter-agent/jmeter-agent-frontend/src/views/TestCaseEditor.vue)
- [jmeter-agent-backend/tests/test_api_regressions.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_api_regressions.py)
- [jmeter-agent-backend/tests/test_jmx_contracts.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_jmx_contracts.py)
- [jmeter-agent-backend/tests/test_ws_manager.py](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_ws_manager.py)
- [jmeter-agent-frontend/tests/workflow-regressions.test.ts](D:/trae/项目/jmeter-agent/jmeter-agent-frontend/tests/workflow-regressions.test.ts)

验证工件：

- [backend-final.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/backend-final.txt)
- [frontend-all.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/frontend-all.txt)
- [frontend-typecheck.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/frontend-typecheck.txt)
- [frontend-build.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/frontend-build.txt)
- [batch1-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/batch1-red.txt)
- [batch2-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/batch2-red.txt)
- [planner-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/planner-red.txt)
- [batch3-backend-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/batch3-backend-red.txt)
- [batch3-frontend-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/batch3-frontend-red.txt)
- [rag-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/rag-red.txt)
- [rescan-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/rescan-red.txt)
- [cancel-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/cancel-red.txt)
- [cancel-result-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/cancel-result-red.txt)
- [context-window-red.txt](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/context-window-red.txt)
- [source-review.diff](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/source-review.diff)
- [rescan-results.json](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/rescan-results.json)
- [source-before.json](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/source-before.json)
- [verify.py](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/verify.py)
- [rescan.py](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair/rescan.py)
