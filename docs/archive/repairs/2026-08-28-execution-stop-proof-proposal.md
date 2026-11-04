# JMeter 执行所有权与停止证明：待确认方案

日期：2026-08-28。状态：**仅提案，等待用户确认架构与部署取舍；未实现执行所有权表、命令队列或进程隔离后端，未启用多 worker。**

本文件承接已批准的同机协调计划第三节。新增的选择在于：如何证明 JMeter 启动的整组本地进程、容器或远程工作已经停止，而不把一个父进程退出当成全部工作结束。该选择可能引入 Windows Job Object、Linux cgroup 或长期占用容量的行为，因此不能只按“增加协调表”自动实施。

本提案编写只读取代码及官方资料并新增本文件；没有据此新增执行所有权表、命令队列或停止后端，没有运行 JMeter、Docker 容器、模型请求或访问用户真实数据库。同期已完成的协调原语、Memory、调度与回执修改另见实施记录，其测试不能代替本方案的验收。

## 一、当前代码与返回契约

| 当前证据 | 已有行为 | 新协调层不能直接作出的推断 |
|---|---|---|
| [ExecutionLifecycle.prepare](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/execution_lifecycle.py:166) | 检查执行 ID，登记计划，创建报告目录并复制快照，然后持久化执行记录；返回 `TestExecutionInfo` | 仅在 `run()` 或 executor 中领取容量已经晚于 prepare 的文件和数据库写入 |
| [execute / submit](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/execution_lifecycle.py:350) | `execute()` 等待执行及后处理；`submit()` 返回已准备的记录并创建本地任务 | 本地 `_tasks`、`_prepared`、`_executors` 不提供跨进程所有权 |
| [ExecutionLifecycle.cancel](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/execution_lifecycle.py:390) | 返回 `bool`。已有记录非终态时，即使没有本地任务或子进程句柄，也可能落到写入 `CANCELLED` 的分支 | 在错误 worker 上看到 RUNNING 记录，不代表该 worker 能停止或确认另一 worker 的工作 |
| [JMeterExecutor.execute](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/executor.py:131) | 返回 `TestExecutionInfo`，取消会抛 `CancelledError`；局部清理失败时保留 registry 并返回 RUNNING | 业务终态、协程退出与完整执行范围停止是不同事实 |
| [启动子进程](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/executor.py:229) | 持有真实 `Popen`；Windows 直接启动 Java 并创建 process group，Unix 使用新 session | 创建 process group/session 不等于禁止后代脱离；PID 文本或 `poll()` 不能证明所有后代停止 |
| [完成等待](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/executor.py:338) | 真实句柄走 `Popen.wait()`；旧执行兼容路径还会参考 JTL/flag | JTL 稳定、flag 存在、旧 PID 不可访问都不是新的停止证明 |
| [进程清理](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/executor.py:446) | `_kill_process()` 返回 `bool`；Windows 用 taskkill，Unix 用 killpg/psutil；若根 `Popen` 已退出，可能跳过进程树清理并移除 registry | 当前 `True` 不是“无任何剩余后代”的证明；不能原样用来释放共享容量 |
| [容器清理](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/jmeter/executor.py:454) | `docker rm -f` 失败时再查询 daemon，查询错误或仍存在则保留清理失败 | Docker CLI 退出不代表 daemon 管理的容器停止；应绑定不可变容器 ID 并核实同一 daemon |
| [HTTP 取消](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/api/routes.py:826) | 返回 `{"success": bool, "message": str}`，并先查本地缓存 | 新入口不能用旧缓存判断是否存在、归属及终态，也不能把入队成功写成取消成功 |
| [Agent/MCP 共用工具](D:/trae/项目/jmeter-agent/jmeter-agent-backend/app/services/ai/agent/tools/jmeter.py:147) | 返回 `{"cancelled": bool, "execution_id": str}` | 应额外传达 pending/unknown，不能诱导模型自动重复取消或重跑压测 |
| [前端取消](D:/trae/项目/jmeter-agent/jmeter-agent-frontend/src/views/Executions.vue:170) | 任何成功 HTTP 响应都会提示“已取消”，尚未检查响应内的 success | 接入新接口时必须按回执状态提示；HTTP 202/200 本身都不是取消完成 |

现有 [执行边界说明](D:/trae/项目/jmeter-agent/docs/repairs/execution-isolation.md)明确本地模式仅运行可信 JMX，并不提供 OS 沙箱。本方案不删除或改写 JMX 脚本来伪造隔离。

## 二、必须分开的三个状态

1. **业务结果**：现有 `pending / running / completed / failed / cancelled`，继续使用 `TestExecutionStatus`。
2. **停止与容量状态**：新增独立元数据 `active / stopping / released / unknown`。其中 unknown 对应持久 claim 隔离，不能领取该容量。
3. **取消命令回执**：`pending / cancelled / already_terminal / rejected / unknown`，描述某条请求的处理结果。

例如：JMeter 根进程正常退出、结果满足原有完成条件，可以保留业务 `COMPLETED`；如果无法证明其后代已经停止，则停止状态为 `unknown`，共享容量仍占用。不能把结果降成失败来隐藏真实成功，也不能把成功结果当成容量已释放。

本方案只证明执行系统管理范围停止，不声称已经撤销已发出的 HTTP 请求、被测系统中的事务或其他不可逆副作用。

## 三、StopProof 适配器的精确契约

建议增加独立的 `execution_stop.py`，由执行后端产生证据，由执行所有权服务消费。以下是拟议接口，不是已存在实现：

```python
@dataclass(frozen=True)
class ExecutionTarget:
    execution_id: str
    owner_generation: str
    claim_id: str
    execution_fence: int
    mode: str                 # local / container / remote
    launch_nonce: str         # 启动前提交，不能从用户正文覆盖

@dataclass(frozen=True)
class StopProof:
    target: ExecutionTarget
    state: str                # not_started / stopped / running / unknown
    basis: str                # 枚举，不接受任意说明文本作为证明
    launcher_exit_code: int | None
    all_managed_work_stopped: bool
    reason_code: str          # 有界代码；不持久化日志、命令或凭据

class StopProofAdapter(Protocol):
    async def observe(self, target: ExecutionTarget) -> StopProof: ...
    async def cancel_and_observe(self, target: ExecutionTarget) -> StopProof: ...
```

`StopProof` 只能由受信执行后端产生，不能来自 HTTP 参数、模型工具参数或数据库中未经验证的任意布尔值。所有权服务先核对 target 的 execution/owner/claim/fence，再接收结果。`not_started` 或 `stopped`、完整管理范围已停止、等待/清理/文件写入的真实 Future 均已结束，三者同时成立才允许释放。任何不一致、存储失败、探测失败、权限不足或超时都保持 unknown；旧 claim 的迟到证明无效。

| 可观察情形 | 证明结果 | 容量处理 |
|---|---|---|
| 本 owner 的准备工作已结束，持久 phase 仍为准备阶段，没有开始启动调用，没有排队的启动 Future | `not_started / preparation_finished_without_launch` | 可以释放；对应从未启动的取消可确认 cancelled |
| 启动前已提交 launching，但崩溃发生在 Popen/容器调用前后无法区分 | `unknown / launch_outcome_unknown` | 保留；不能因为尚未记下 PID 就断言未启动 |
| 真实 Popen.wait 返回退出码，但无完整后代约束 | 记录退出码；完整停止仍 unknown | 保留；正常业务结果按原规则保留 |
| Windows 受约束 Job 没有活动进程，启动关联无间隙、未允许 breakaway，且所有本地等待/清理 Future 已结束 | `stopped / windows_job_empty` | 可释放，不要求为了释放而把正常结束改成强杀 |
| Linux 独占 cgroup 子树为空，受管进程没有迁出权限，所有本地 Future 已结束 | `stopped / linux_cgroup_empty` | 可释放 |
| 同一 Docker daemon 对精确容器 ID 返回已停止，或强制删除后经成功查询确认该 ID 不存在；关联 CLI/Future 也已结束 | `stopped / container_stopped_or_absent` | 可释放；CLI 断连或 daemon 查询失败均不满足 |
| 已确认 owner 死亡，子进程仍活着或状态不明 | `unknown / owner_lost_after_launch` | 隔离；新 worker 不接管原 PID 或重放执行 |
| 本地 distributed JMeter 的客户端退出，但没有远端该次运行的权威停止确认 | `unknown / remote_completion_unproven` | 保留；不按本机 PID 释放 |

Windows Job 默认可包含后代，但 breakaway 设置会改变这一约束；若采用此方案必须明确禁止逃离并处理嵌套 Job 兼容性。[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject)。Linux cgroup v2 的 `populated=0` 表示该子树无存活进程，但必须配合禁止迁出的权限边界。[内核 cgroup v2 文档](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)。容器证据应来自 daemon 的容器状态查询，不能来自 CLI 是否退出。[Docker inspect](https://docs.docker.com/reference/cli/docker/container/inspect/)、[Docker container wait](https://docs.docker.com/reference/cli/docker/container/)。

## 四、需要确认的两种方案

### 方案 A：增加可选的受约束执行后端

**证明范围限制：** Job/cgroup 不是任意 JMX 脚本的安全沙箱。Microsoft 文档明确指出，借助 `Win32_Process.Create` 创建的进程不会自动加入原 Job；经系统服务代为启动、主动迁出或远端委派的工作也不能仅凭本地容器为空证明停止。因此首版仅支持可信 JMX 与可验证的直接进程派生路径；存在这些委派行为或不能确定其范围时，仍须拒绝启用相应执行模式或保持 unknown，不能宣称所有后代均被约束。[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

在已确认的平台上使用 Windows Job Object、运维委派的 Linux cgroup，或已配置的容器模式作为完整执行范围。正常完成也检查该范围为空，满足时正常释放容量；异常及无法确认的情况仍隔离。

- **Windows**：需要在程序执行前完成 Job 关联，避免先运行再加入的间隙；需验证当前宿主已有 Job、嵌套 Job、创建/分配/终止权限及控制句柄生命周期。普通同用户进程可能不需要管理员，但不能保证所有部署都可用；本方案不自动提权、不全局修改宿主 Job。
- **Linux**：需要可使用的 cgroup v2 和运维委派的目录/权限，且执行子进程不能自行迁出。不能假定应用用户可写系统 cgroup；不自动挂载、不修改系统服务配置、不用 root 作为错误回退。
- **容器**：继续要求本机可用的固定摘要镜像、非 root 容器用户和现有最小挂载。Docker daemon 的访问权限本身敏感，不新增 docker.sock 挂载或 privileged 运行。改为精确 ID 绑定与 daemon 证据后仍需实际容器验收。
- **兼容性**：保留原始 JMX；禁止脱离执行范围可能改变自建子进程、特定插件及启动定制的行为，须在用户环境验证。远程 `-R` 不由 Job/cgroup 自动解决，首版协调模式应在准备写入前拒绝没有停止适配器的远程执行；默认未配置的旧模式不因此改变。
- **失败策略**：选择某后端后，缺少权限、关联失败或后端不可用就拒绝该次启动。不能静默退回不受约束本地启动，也不能自动切到方案 B。

**适合希望正常压测完成后自动归还全局容量、长期运行同机多 worker 的部署。** 成本是平台代码、部署前置条件及真实平台验收。建议作为最终可持续运行的方向，但应另行确认后分平台实现，不能在当前批次顺带加入完整执行器重写。

### 方案 B：沿用当前启动方式，对无法证明清理的执行保持隔离

不新增 Job/cgroup；可持久化所有权、路由取消，并在有充分既有证据时释放，例如确认未启动或将来补齐的容器停止证明。无完整后代证明的本地根进程退出，保留其已知 `COMPLETED` 业务结果，同时停止状态为 unknown、容量仍被占用。

- 不引入新系统权限，JMX 和默认启动方式兼容性较高。
- 代价必须明确：**正常本地执行也可能长期占用名额；连续执行可能耗尽全部全局容量。** 这不是可用性上等价于方案 A 的替代品。
- 不设置 TTL 自动释放，不通过重启 worker、清空协调表或删除命令回执来恢复容量。人工核实的解决机制需独立设计，不能添加一个无证据的“强制释放”按钮。
- 仍然可能有无法停止的后代/远端工作；本方案只防止系统错误宣称资源空闲，不宣称已终止这些工作。

**适合愿意以人工核实和容量保守占用换取较小平台改动的场景。** 如不接受“成功但容量未释放”，不应启用这个模式。

**两种方案共同默认：** 现有单 worker、未注入 Coordinator 的路径保持不变；不修改 `main` 自动启用，不把提案写成已支持多 worker。用户确认方案前，执行所有权服务、OwnerCommand 表及执行器接线均暂停实施。

## 五、所有权、命令与最小实施边界（确认后）

建议优先独立落地协议与测试，再接业务入口；不得先接取消入口再补停止语义。

### 1. 持久记录

- `ExecutionOwner`：execution ID、project ID、owner generation、operation ID、claim ID、execution fence、phase、launch nonce、后端类型、可选的原始 PID 创建时间/不可变容器 ID、停止状态及有界 reason code。持有资源期间不改绑 owner。不保存完整命令、Token、API Key 或日志。
- `OwnerCommand`：command ID、execution ID、目标 owner/claim/fence、固定 action=cancel、请求人稳定 user ID/incarnation、queued/processing/终态、结果代码及时间。主键去重，同一 command ID 的不同参数拒绝。
- execution claim 必须早于 `prepare()` 的计划写入、目录与快照创建；operation ID 使用 `execution:<id>`，避免与调度 dispatch 当前使用的 execution ID 冲突。原子领取 `execution:<id>` 资源与一个固定 `jmeter` 容量槽。
- owner 记录需要先于业务 execution 行存在，因此不能盲目增加“必须已存在 executions 行”的外键。可先完成只读归属解析，再领取资源及绑定 owner；两次短事务也必须都成功后才能开始准备副作用，失败不得启动或重放。
- `launching` 必须先持久提交，再调用启动后端。PID/容器 ID 是后续确认信息，不是回推“此前没有启动”的依据。
- 通过精确 claim 校验的短事务更新 execution owner、命令及终态；普通旧缓存不能覆盖这些记录。持有 claim 不代表跨外部调用持有 SQL 写事务。

### 2. 服务接口与执行顺序

拟议 `ExecutionOwnership(coordinator=None)` 与现有 Agent 可选运行时一致：未配置时不创建线程、不写表、不拦截旧执行；配置后存储故障不回退到本地 semaphore。

服务接口划分为 `acquire_before_prepare()`、`mark_launching()`、`record_target()`、`finish_with_proof()`、`enqueue_cancel()`、`poll_once()` 和 `stop_admission()/drain()`。领取、绑定与阶段更新使用现有 Coordinator 事务/claim 校验；共享核心文件仍由其所有者维护。

执行入口需在调用真正执行器之前持有 claim；`run()` 不能仅凭“数据库有 pending 行”领取旧运行重放。全局名额已领取后不再等待第二个本地 jmeter semaphore。首版可以保留整个 claim 到生命周期及真实后处理 Future 排空，代价是名额占用更长；不能为了提前归还容量而部分释放仍在使用的执行锁。

### 3. 取消路由与身份

公共入口先刷新真实用户及项目 ACL，从 DB 读取 execution/owner，不能依赖本地缓存存在性。请求人身份取受信安全上下文中的 user ID 与数据库 incarnation，不接受模型参数指定身份，也不持久化原访问 Token。

错误 worker 只向精确 owner/claim/fence 投递命令并返回 pending，绝不调用本地 PID 猜测、改写 CANCELLED 或自动接管。owner 的 `poll_once()` 用短事务将 queued CAS 为 processing，退出事务后重新验证用户仍活动、incarnation 未变化、当前角色与该项目写权限，然后调用停止适配器。撤权、删除重建账户或 DB 不可用均不执行命令。

可复用现有按稳定用户/incarnation 重建权限的逻辑，但命令不允许匿名/legacy user 分支，也不能借 `internal_scope()` 绕过请求人的权限。owner 自身的关停清理是单独、仅限已持有资源的能力，不依赖被撤销用户继续授权其释放资源。

命令执行完成后，仅凭匹配的 StopProof CAS 提交 cancelled 或 already_terminal；停止未证实则 pending/unknown，保留资源。owner 丢失后的 processing 命令不在新 generation 重放。重复读取回执不触发新的取消。

### 4. 命令消费者生命周期

构造函数不启动轮询；只允许测试/未来显式集成调用 `start()`，默认 `main` 不接线。轮询调用有界 `poll_once(limit)`，每条命令的数据库事务在 OS/daemon 操作前结束。

关停顺序是停止新执行 admission、保留取消/清理能力、排空已持有任务及真实清理 Future、对未确认项持久化 unknown，最后停止命令消费者。轮询失败或超时不能转成“所有工作已结束”。若进程退出，未决记录保留，新 worker 仍不能据此重放执行。

### 5. 文件与分批边界

| 批次 | 拟修改文件 | 验收产物 |
|---|---|---|
| 协议与存储 | 新 `app/services/execution_ownership.py`、`execution_stop.py`；在 `app/models/db.py` 追加两表；新独立测试及惰性测试 worker | 无业务入口接线，重复命令/旧 claim/存储故障/原子容量的隔离测试 |
| 受控接线 | `execution_lifecycle.py`、`jmeter/executor.py`；只有显式注入实例启用 | prepare 前 admission，真实停止证明，清理取消排空，默认路径回归 |
| 取消表示 | `api/routes.py`、`agent/tools/jmeter.py`、前端 API 类型/Executions/i18n | pending/unknown 不假装 cancelled，DB 权威查询与命令回执轮询 |
| 选择 A 时的后端 | 另列 Windows/Linux/容器适配器及对应平台测试 | 实际平台能力与权限验收；不以模拟测试声称 Job/cgroup/容器验证完成 |

上述均是待确认设计；本提案阶段不增加这些执行所有权代码或表。

## 六、HTTP、工具与 UI 的精确 pending 契约

保留现有 `ExecutionLifecycle.cancel() -> bool` 的旧接口形态，但其当前实现存在第一节列出的停止证明缺口，不能直接作为新协调层的证明。新协调入口使用独立 `request_cancel() -> CancelReceipt`；不返回可被误当成 True 的 dataclass 来替代旧 bool。新回执的 cancelled=true 必须由停止适配器证明，不能直接复制旧 bool。

`CancelReceipt` 包含 `execution_id, command_id, accepted, cancelled, state, message`。`accepted` 只表示命令已经提交；`cancelled` 只在该次取消完成时为 true。正常执行抢先结束返回 already_terminal，不能声称取消导致了结束。

```json
{
  "execution_id": "run_123",
  "command_id": "32位随机命令ID",
  "accepted": true,
  "cancelled": false,
  "success": false,
  "state": "pending",
  "message": "取消请求已提交，等待执行进程确认；请查询回执，不要重跑测试"
}
```

HTTP 保留 `success` 字段并令其严格等于 `cancelled`，不重新定义旧 success 的含义。pending 使用 202；确认 cancelled、already_terminal、已有 unknown 回执使用 200；身份/ACL 失败使用 401/403，资源不可见为 404，提交前协调存储失败为 503，且不伪造 command ID 或 accepted=true。新入口上线时前端必须同时更新，不能让旧前端把 202 当成“已取消”。

拟新增 `GET /executions/{id}/cancel-requests/{command_id}` 读取同一回执，重新核对当前项目写权限及命令请求人身份；其他用户只可通过既有授权执行状态接口看业务结果，不暴露命令提交者。poll 不重新投递命令。回执返回 unknown 时停止自动轮询，显示人工核实提示。

Agent/MCP 工具保留 `cancelled` 与 execution ID，增加 accepted/state/command ID/message；不加入会被 ToolRegistry 当作业务失败并诱导重试的 `success:false` 字段。工具返回 pending 只说明请求提交，不说明压测已经停止；后续只能查询状态，不自动生成新的 execution ID 重跑。

| 回执/停止状态 | UI 行为 |
|---|---|
| pending / stopping | 信息提示“取消请求已提交，等待执行进程确认”；保留原业务状态，禁止重复点击同一命令，轮询回执 |
| cancelled | 才提示“已取消”，重新从服务端读取执行状态 |
| already_terminal | 提示“执行已结束”，保留真实 completed/failed/cancelled，不改写为 cancelled |
| unknown | 警告“停止状态未确认，资源仍占用，请人工核实”；不提示已取消，不显示容量可用 |
| completed + stop unknown | 同时显示“执行完成”和“清理未确认/容量仍占用”两个信息；报告仍按现有 ACL 查看 |
| 401/403/404/503 | 显示相应失败，不做本地成功状态更新；权限失效后停止回执轮询 |

## 七、确认后必须完成的验证

- 两个真实 OS 进程、隔离 WAL 数据库及已修复的 SQLite；同 execution ID、全局容量与重复命令竞争只能有一个领取者。
- admission/owner 绑定/launching/Popen 返回/子身份保存各边界故障，证明没有提前文件副作用、重复启动或错误释放。
- 使用惰性的 Python 子进程：owner 父进程退出而子进程仍活，另一个 worker 必须拒绝接管并保留容量；测试进程最终由测试控制器准确清理。不得启动真实 JMeter。
- 错误 worker 取消只返回 pending，不写 CANCELLED；owner 消费后才可确认。命令排队期间撤权、删除重建用户、claim 改变必须拒绝执行。
- 真正的等待/清理线程尚未完成时反复取消 asyncio wrapper，容量必须仍占用；旧结果迟到不能释放新 claim。
- 正常 launcher 成功与完整范围停止分别断言；方案 B 必须测试 `COMPLETED + unknown`，而不是只测异常根进程退出。
- 容器/远端 daemon 响应失败、假阴性、CLI 先退出、PID 复用、后代存活以及探测权限不足均按 unknown 处理。模拟测试与真实平台测试分开报告。
- 默认未注入路径的 execute/submit/cancel、HTTP/工具旧返回契约、现有单 worker 启动与前端行为回归；全部入口验收之前不得修改多 worker 部署说明。

## 八、待用户确认

请选择后续执行协调采用的方向：**A，增加经平台验收的可选执行范围约束，使正常停止后可以自动释放容量；或 B，沿用当前启动方式，接受已成功执行仍可能长期占用容量并需要人工核实。**

在确认之前，默认单 worker 保持不变，执行所有权/OwnerCommand/停止后端不实施；当前已经完成的协调原语和其他可独立验证工作不据此宣称执行协调已经上线。
