# 第十五批修复：漂移分析的时间与数值边界

日期：2026-08-28。状态：**已实施并完成本地隔离回归；未部署，未调用真实模型或执行压测。**

本批处理继续扫描中复现的三个局部缺陷，不改变 Agent 业务目标、分析职责、回归公式或阈值。修改 3 个生产文件和 2 个既有测试文件；无新增依赖、配置、数据表、接口字段或后台任务。

## 一、逐项修改说明

### 1. HTTP 与 Tool 的执行时间解释一致

**【修改内容】** 在 [HTTP 漂移入口](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/api/analysis_routes.py:62)和 [漂移 Tool](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/tools/resource.py:308)中，将执行时间转换为 UTC 后比较窗口和构造分析历史。无时区时间按服务器本地时间解释，带时区时间保留其实际时刻；不修改原执行对象。

**【修改原因】** HTTP 原来以无时区 cutoff 比较带时区执行时间，会抛出 TypeError。Tool 虽有 UTC 处理，却直接给旧无时区时间贴上 UTC 标签；而 [实际执行写入](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/execution_lifecycle.py:201)使用 `datetime.now()`。在非 UTC 服务器上，这会改变历史窗口及采样间隔，甚至纳入过期记录。

**【影响范围】** HTTP/Tool 的历史时间过滤及传给 DriftDetector 的日期。项目、执行状态、指定测试计划、非空报告和样本数过滤保持不变。Tool 名称、参数、权限与重试不变；Prompt、Memory/RAG、Session、Context 预算和 Workflow 步骤不变。独立调用 DriftDetector 时，对无时区日期字符串的既有解释不变。

**【风险等级】** 低；优先级高。旧记录若来自其他时区的服务器，或服务器后来更换时区，仅凭无时区时间无法恢复原始偏移。本批不猜测来源时区，不改写历史，不开展全项目时间存储迁移。

**【验证方式】** 扩展 [HTTP 契约测试](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_api_contracts.py:160)：混合本地无时区、UTC+8、UTC-5 记录，以及带时区/无时区的过期记录。真实 ASGI 路由与直接 Tool 返回相同结果，只保留 3 条有效历史，斜率为每日 100，原始时间不变。首先复现 HTTP TypeError；进一步按实际写入规则构造旧记录后，复现错误纳入第 4 条记录，再修正两个入口。

**【为什么这是最优方案】** 在本批约束下直接修正两个消费入口，保留实际写入规则和现有接口，不引入公共时间框架或数据迁移。只修 HTTP 异常并将旧记录一律当作 UTC，会掩盖而非解决窗口偏移。

### 2. 无效测量值不再作为趋势证据

**【修改内容】** [DriftDetector](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/insights_engine.py:222)在数值转换前排除布尔值，并在原来的转换异常处理内补充 OverflowError。有效数字、可转换的数值字符串及原有非负/有限检查保留。

**【修改原因】** Python 会将 True/False 转成 1/0，三条布尔记录因而能够产生“稳定”结论；超大整数转换 float 则抛出未处理异常。两者都不是可靠的测量证据。

**【影响范围】** 共享漂移计算层，HTTP 和 Tool 同时受益；不改变其他分析器。无足够有效观测时沿用 `health=unknown`，不伪造零值。Prompt、Tool 参数、Memory、Context 和 Workflow 无结构性变化。

**【风险等级】** 低；优先级中。仅收紧不应被当作合法测量值的输入。

**【验证方式】** 在 [既有分析契约测试](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_insights_contracts.py:60)增加 True、False、超大整数三个场景。修改前分别出现错误 stable 或 OverflowError；修改后为空趋势、unknown，且可严格 JSON 序列化。

**【为什么这是最优方案】** 复用既有“跳过坏测量值”的边界，不吞掉整个分析异常，不新增任意业务数值上限，也不更改返回结构。

### 3. 计算溢出不再输出不可信趋势或健康结论

**【修改内容】** [趋势计算](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/insights_engine.py:248)在发布结果前检查均值、斜率、30 天预测和变化百分比是否有限。计算溢出的指标不进入斜率/预测字典，并附指标级警告。若其他指标没有明确恶化证据，整体不报 stable；已有可靠 drifting/degrading 结论继续保留。

**【修改原因】** 输入值有限不代表计算结果有限。均值累加或预测乘法可能溢出，旧逻辑会产生 NaN/Infinity，或因 NaN 比较均为 False 而报“稳定”。HTTP 最后的 JSON 清洗只能替换非有限数字，不能纠正错误的健康结论；Tool 还会直接收到这些数字。

**【影响范围】** HTTP/Tool 共用的 DriftReport 内容，前端沿用现有 unknown 展示分支，无需修改组件。正常回归公式、30 天窗口和告警阈值不变。Prompt 与 Memory/RAG 未变，无额外模型调用、检索、重试或 Workflow 步骤。异常结果增加必要的解释性警告；没有宣称实测 Token 节省。

**【风险等级】** 低；优先级中。发生计算溢出时宁可不提供该指标的推断，也不输出看似精确的异常值。本批未引入高精度回归，不能据此承诺处理所有极端数值的精度问题。

**【验证方式】** [六个参数化场景](D:/trae/项目/jmeter-agent/jmeter-agent-backend/tests/test_insights_contracts.py:74)覆盖均值溢出、预测溢出，各自组合无其他指标、其他指标稳定、其他指标恶化。验证指标级警告、剔除无效结果、保留可靠恶化及严格 JSON 序列化。另用固定种子的 300 组普通历史数据与修改前源码逐项比较，完整报告完全一致。

**【为什么这是最优方案】** 在产生派生结果的地方检查可信性，比只在 HTTP 序列化时置空更完整；同时不替换回归算法、不引入数值计算依赖，也不扩大为新的趋势产品规则。

## 二、验证结果与证据

测试运行器将数据库、模型配置、JTL/报告目录、Memory 与 RAG 重定向到仓库外的临时目录，禁用模型下载并阻止真实 JMeter 子进程。HTTP 测试使用 ASGI 内存传输。未使用用户数据库、凭据或真实压测目标。

| 检查 | 本次结果 |
|---|---|
| 首次新增缺陷用例 | 10 failed，4 个既有漂移用例 passed；失败原因已核对 |
| 旧本地时间二次复现 | 1 failed，错误将 4 条记录纳入窗口，预期 3 条 |
| 修复后的局部回归 | 105 passed，811 deselected |
| 后端全量 | **916 passed，524 warnings，79.95 秒** |
| 前端全量 | **6 个文件、93 passed** |
| Vue/TypeScript 类型检查 | 退出码 0 |
| Vite 构建 | 退出码 0，25.83 秒，输出至仓库外 |
| 普通数据兼容性比较 | **300 组完整漂移报告与修改前完全一致** |
| Python AST 复扫 | 116 个文件，0 个语法错误 |
| 项目源码模式复扫 | 138 个文件；已人工检查仍命中的 Tool handler 行，其前置参数校验及确认逻辑未改变 |
| 源码差异 | 3 个生产文件、2 个测试文件改变；无源码新增/删除，无配置或依赖变更 |
| Prompt 检查 | 7 个核心 Prompt/策略表达式 AST 与修改前一致；其他 Prompt 文件也未修改 |

完整证据在 [repair15 验证目录](C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair15)。包括 `drift-red.txt`、`timezone-local-red.txt`、`focused-green.txt`、`backend-full.txt`、前端测试/类型/构建日志、`normal-drift-comparison.txt`、`source-review.diff` 与 `rescan-results.json`；修改前源码也保存在该目录。

后端警告包括 datetime.utcnow、LanceDB table_names 弃用、Pydantic 字段命名及 multipart 警告；本批没有为了消除警告升级依赖。前端构建有第三方 PURE 注释提示，以及使用外部 outDir 不自动清空的提示。测试时长不是生产性能基线。

## 三、复扫结论与仍待处理范围

本批在所列回归和源码检查范围内未发现新增功能回归；这不是全项目无 Bug 的证明。Prompt、Tool 参数与授权、Memory/RAG 读写及 Workflow 编排文件没有被顺带重构，正常路径不增加模型调用、检索或上下文块。

此前待确认的架构项目仍按 [架构提案](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-28-architecture-next-proposal.md)保留：持久任务回执与恢复、项目 ACL、跨进程 Session/配额、出站与报告来源隔离、高级负载和真实环境验收。本批也不迁移历史时间存储，不声称这些项目或全部待修问题已完成。
