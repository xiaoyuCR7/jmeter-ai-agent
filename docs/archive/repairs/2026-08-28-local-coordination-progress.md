# 第十九批：同机协调实施记录

**状态：本批五项已实现并通过整批回归；完整协调架构未完成，执行停止方案待确认，不是多 worker 上线说明。** 前置项目 ACL 报告见同目录 `2026-08-28-project-acl-implementation.md`。本批扩展现有 SQLite、Session 和工作流边界，未启用多 worker，未运行真实 JMeter/模型，未操作用户真实数据库或升级全局 Python。

## 1. 协调原语（完成独立复核）

- **【修改内容】** 四张新增表保存协议/容量/主机域、进程身份、操作及资源租约。短事务原子领取资源和容量，独立 claim/fence；重复 operation ID 不再授予执行权。拒绝不一致记录、未知文件系统、数据库硬链接和未修复 SQLite。真实 PID 与原始创建时间用于 ALIVE/DEAD/UNKNOWN 探测。
- **【修改原因】** 进程内信号量无法约束多个 worker；失联、权限不足、损坏 PID 身份不能解释为安全接管。
- **【影响范围】** `coordination.py`、`process_identity.py` 和新增 ORM 表。Prompt、Tool Schema、业务 Memory 数据无变化。接入方仍须实际跟踪线程/进程，不因调用一个 release 方法就能证明工作结束。
- **【风险等级】** 高。租约及资源未知时保守占用，可能降低可用容量，需要核实未决结果。
- **【验证方式】** 最新组件 **61 passed / 7.17s**。独立 Windows OS 进程明确使用 SQLite **3.53.4 / WAL**，验证同键、容量、重复 ID、实际死亡、停止心跳仍存活、旧 claim、真实线程 drain 和数据库故障回滚。Linux 分支仅模拟测试及官方文档核查，没有真实 Linux 服务验收。
- **【为什么这是最优方案】** 复用现有主数据库，在单机部署边界内补足持久执行权；不引入 Redis/任务集群，不用超时替代死亡证明。

独立复核发现并关闭：损坏原始创建时间导致错误 DEAD、released 租约与操作不一致、Linux 网络盘识别遗漏、hidepid 下 `/proc` 缺失误判、数据库硬链接、测试未启用 WAL，以及非规范 tick/叠加挂载的残留情况。较早 DELETE journal 测试不能作为最终 WAL 验收证据。

## 2. Agent 会话、分析名额和真实工作 drain（可选注入集成）

- **【修改内容】** `AgentOperationRuntime` 在 Memory 读取前原子取得 session + agent + 可选 analysis 容量；不再取得全局资源后又等待第二个本地信号量。claim 覆盖历史保存及实际 `concurrent.futures.Future`。关闭 asyncio waiter 或事件循环不冒充线程结束。模型/Tool/归档前提交副作用边界；未知外部结果保留 quarantine。
- **【修改原因】** 取消协程不会停止其同步线程；早放名额会造成超限、同会话同时写入以及未知外部操作被重复启动。
- **【影响范围】** BaseAgent、AnalysisAgent/AnalysisRuntime、模型适配器、ToolRegistry、分析阶段检查点。Prompt 定义和 Tool 参数未改，不增加模型调用；只有显式注入协调器时使用新增协调路径，主启动入口仍未配置它。
- **【风险等级】** 高。外部调用异常可能保留未决名额，不能把错误自动解释为远端已停止；整体启动/收尾集成尚未完成。
- **【验证方式】** 已有真实两个 PID 的 Runtime/实际线程 gate 测试；根任务另验证 BaseAgent 入口、SDK/Tool/归档提交失败不调用、SDK 取消 quarantine、实际 AnalysisLease 取消 drain、会话保存期间持有及不等待第二信号量。最终全部包含在后端 1400 项通过的完整回归中。
- **【为什么这是最优方案】** 保留现有 Agent 循环、线程池和事件语义，只把资源所有权提升为持久记录；不重写 Planner 或增加推理步骤。

## 3. Memory CAS 与维护互斥

- **【修改内容】** 会话新增 version/incarnation，旧表扩展迁移保留历史；新对象只能插入，已加载对象只可 CAS 更新，拒绝迟到覆盖和删除重建后的旧版本。过期读取仅删除已过期代次，以便新会话按原语义保存。反复取消仍等待 SQLite open/close 队列真实结束。配置协调器后读写验证 session claim；删除/清理跳过占用项。
- **【修改原因】** `INSERT OR REPLACE` 会覆盖新内容并复活删除历史；仅等待被取消的异步包装不能证明 SQLite 后台线程已提交或关闭。
- **【影响范围】** ConversationMemory、OperationRuntime 维护路径和相关测试。压缩 Prompt、消息格式及 to_dict/from_dict 序列化不变。未配置协调时清理保留单条批量 SQL，不增加按会话连接开销。
- **【风险等级】** 中至高。冲突保存返回 False 并保留 dirty，不擅自把旧对象升级到当前版本；调用方沿用 memory_not_saved 提示。
- **【验证方式】** Memory 实现阶段 255 项相关回归通过；独立 OS 进程验证首写仅一胜者、旧版本及旧 incarnation 写入失败。接入共享 session 后根任务相关组 234 项通过（各组包含重叠测试，不相加）。取消前后真实 SQLite 队列测试、活动会话维护跳过测试均已覆盖。
- **【为什么这是最优方案】** 少量扩展元数据加版本比较即可防止静默覆盖；不迁移到新 Memory 服务、不重算摘要、不读取更多历史。

## 4. 调度批次永久去重与停止证明边界

- **【修改内容】** 新增永久调度批次表；同一事务插入唯一 `(schedule_id, planned_at)` 并 CAS 推进 next_run/count，领取失败不启动。任务创建前或提交后崩溃不重放该批次。调度资源保留到实际工作有明确停止证明；仅显式同步 `completion_verifier(...) is True` 可确认完成，未配置/异常/其他值一律保留 unknown/quarantine。
- **【修改原因】** 多 worker 会重复扫描同一到期计划；协程返回、主进程退出及业务 COMPLETED 都不能证明派生进程或容器停止。错误释放会允许重叠压测。
- **【影响范围】** ScheduleRunner、新 ScheduleDispatchStore/表及隔离测试。创建者项目 ACL、通知及业务结果语义保留；COMPLETED 可以与清理 unknown 并存。Prompt、Tool Schema、Memory 不变。生产未接入协调器或停止证明实现。
- **【风险等级】** 高。缺少停止证明会长期占用调度资源，不能用 TTL、删除记录或重启消除不确定性；因此不是可立即上线的多 worker 功能。
- **【验证方式】** 组件相关组 **65 passed / 23.05s**；实际两个 Windows OS 进程在 SQLite 3.53.4/WAL 竞争仅一批次、一次数、一条惰性测试调用。提交后 exit 73、恢复旧 due 时间也不重放。停止证明缺失等五类回归先失败后修复通过，业务 COMPLETED 保留。
- **【为什么这是最优方案】** 在既有调度器增加唯一批次与显式证明边界，不引入新任务平台、不改变 cron 业务；执行器后端约束作为单独架构选择。

## 5. 分析回执的持久执行所有者

- **【修改内容】** 新回执与进程 owner 原子绑定；Agent admission 后、Memory/模型/Tool 前一次性绑定 operation/claim/fence。其他 worker 查询通过真实进程身份和持久租约判断 running，死亡或不明返回 unknown，不创建新执行权。写入需同时满足用户/项目权限、服务 generation、进程 owner 和精确 claim；旧句柄不能写入。已释放操作可完成自己的最终回执，quarantine 仅允许有限元数据收尾。首次身份初始化放在写事务前；事务内授权复核保留。
- **【修改原因】** 本地 `_active` 缺少另一个 worker 的任务时，不能将请求视为可重试；复制 request_id/generation 或使用迟到结果，也不能取得新 owner 的写权限。权限版本首次初始化会经另一连接写入，不能在持有回执 SQLite 写锁后才触发，否则发生锁冲突。
- **【影响范围】** AnalysisReceipts、新回执 owner 表、BaseAgent 绑定钩子。仅新增身份/状态元数据，不存任务输入、Token、API Key 或事件正文；不修改 Prompt、Tool 参数或 Memory 内容。
- **【风险等级】** 高。历史未绑定回执不自动升级为可重放任务；存储/绑定失败不启动工作。未决状态需要后续证明与核实，不能自动修复成成功。
- **【验证方式】** 回执相关组 **59 passed / 16.29s**，实际 Windows 子进程 PID 24848 使用 SQLite 3.53.4/WAL。覆盖另一服务无本地任务仍识别 running、复制句柄拒写、确认进程终止后 unknown 且重复请求无新 handle。根任务另验证 BaseAgent 绑定失败不调用工作。首次权限版本初始化的三入口回归先复现 database is locked，再修复通过；仅写入路径提前初始化身份，事务内仍复核，预初始化后撤权返回 403。GET 轮询不增加这次预刷新。
- **【为什么这是最优方案】** 扩展现有持久回执，与已实现的 claim 复用同一主数据库；保留原 request_id 去重，不迁移历史结果、不引入重放队列。

## 尚未完成的部署门槛

- 执行 owner / 取消路由 / 子进程及容器停止证明 / 全局压测容量。新发现的部署取舍见 [执行停止证明提案](D:/trae/项目/jmeter-agent/docs/repairs/2026-08-28-execution-stop-proof-proposal.md)，等待用户确认，不直接改写执行后端。
- 跨 worker 权威数据与缓存一致性、RAG 初始化/文件写协调、共享工具确认。
- MCP/WS 路由、受协调的启动迁移与可靠关闭。
- 后续执行/数据/传输/启动切片实施后的再次全量回归和真实平台验收；本批验证结果如下，不能覆盖尚未实现的切片。
- 上述完成后，才实施显式启用、加密输入、次数有界且只在副作用前恢复的未来请求。旧回执与 unknown 状态不自动重放。

## 最终整批验证与扫描

源码冻结后根任务重新执行，命令退出码均为 0：

| 验证 | 本次实际结果 |
|---|---|
| 后端完整 pytest | **1400 passed，133.10 秒**；含前批 ACL 和空项目目录删除兼容修复 |
| 前端完整 Vitest | **8 files / 104 passed，6.62 秒** |
| vue-tsc --noEmit | 通过 |
| Vite 生产构建 | 通过，21.95 秒；产物仅写工作区外 |
| Python AST | **148 个文件**全部解析成功 |
| 已知风险模式扫描 | **148 个项目源码文件**；仅命中统一 ToolRegistry 内受参数/授权/确认/副作用边界保护的 handler 调用，非 MCP 绕过 |
| 相对本批源码基线 | **13 个修改、11 个新增、0 个删除**；本表不计文档及外部验证产物 |
| Prompt 与接口比较 | 7 个关键 Prompt AST、Memory 消息/压缩/序列化方法 AST、全部 Tool 定义均保持一致 |
| 默认部署及配置 | main、前端源码/测试、既有 JSON/INI/TOML 配置与基线一致；协调仍未接入启动入口 |

后端有 4377 条 warning，主要为现有/新增测试经过的 datetime、ORM、Pydantic、LanceDB 等兼容与弃用提示；没有把“测试通过”写成“无警告”。未发现 RuntimeWarning、未等待协程或销毁 pending task 的日志。前端构建保留依赖 PURE 注释提示，未为消除提示而升级依赖。

新增协调会引入有界的本地 SQL 事务和 OS 身份探测成本；这不是免费性能提升。它减少重复批次和错误并发，不增加模型推理步骤、Prompt 长度或 Tool Schema。未运行真实模型、真实 JMeter、Docker 或远端系统，未测量实际 Token 节省比例；Linux OS 分支尚未做真实平台验收。

本次测试和静态复核未发现验收范围内的新增回归，**不能证明整个项目没有任何 Bug**。历史未知回执不重放；自动恢复、完整跨 worker 路由与执行协调不记为完成。当前 SQLite 验证使用隔离 3.53.4，未替换用户全局运行时，也未执行生产数据库迁移。

证据根目录：`C:/Users/熊小雨/.codex/visualizations/2026/08/27/01a042d8-9b64-70a0-b6c9-3f109e596c7d/repair19/`。`backend-final.txt`、`frontend-full.txt`、`frontend-typecheck.txt`、`frontend-build.txt` 是本次完整命令日志；`rescan-results.json` / `source-review.diff` / `prompt-check.json` / `contract-check.json` 保存最终扫描。`source-before/` 是不可覆盖基线；`coordination-core/`、`memory-cas/`、`schedule-dispatch/`、`receipt-ownership/` 及根目录定向日志保存组件、独立进程与故障注入证据。重叠测试组不累加。
