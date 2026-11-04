# Agent 安全与性能后续修复报告

日期：2026-08-30。本批继续上一轮已列明的上传资源风险、弃用迁移和依赖安全检查；不改变 Agent 业务目标、Prompt、Tool Schema、Memory、Workflow、Context、JMeter 隔离或自动恢复边界。

## 1. CSV 上传有界流式处理

- **【修改内容】** 新增 `TEST_DATA_MAX_UPLOAD_BYTES`，默认 100 MiB，可通过环境变量覆盖且必须大于零；上传按 1 MiB 分块读取到 `SpooledTemporaryFile`，超限返回 413。最终磁盘复制和 CSV 行扫描移出事件循环；写入使用独占创建避免并发同名覆盖，取消或复制失败时清理输出。
- **【修改原因】** 原实现 `await file.read()` 将 Starlette 已暂存的整个上传再次一次性载入进程内存；大文件可造成内存耗尽，磁盘复制和全文件行数统计还会阻塞事件循环。
- **【影响范围】** 测试数据上传端点、一个后端配置项和 `.env.example`。成功响应、CSV 位置、列/行统计、项目 ACL、Prompt、Tool、Memory、Workflow 与 Context 不变；超过配置上限的新请求改为 413。
- **【风险等级】** 高。默认 100 MiB 对多数 JMeter 数据集保留兼容性，超大数据集可由部署者显式调高。Multipart 在进入 route 前仍由 ASGI 栈解析，因此反向代理/服务器层仍应设置相同或更小的请求体上限。
- **【验证方式】** 修复前测试分别复现“超限仍落盘”和“单次无界读取”；修复后 7 项上传/ACL 测试通过，覆盖精确上限、分块、拒绝无残留、ACL 变化、取消等待线程并清理输出。
- **【为什么这是最优方案】** 复用 FastAPI `UploadFile` 的磁盘暂存能力，不引入新上传协议；限额可配置，内存保持有界，并保持授权检查先于正文消费、正文消费后再次授权的既有安全顺序。

## 2. UTC 弃用迁移但保持存储契约

- **【修改内容】** 新增 `utc_now_naive()`，内部使用 timezone-aware UTC 时钟后明确移除 tzinfo；数据库默认值、认证、调度、通知和 Prometheus 参数中的 33 个 `datetime.utcnow` 引用统一改用该函数。
- **【修改原因】** `datetime.utcnow()` 已弃用并在完整测试中产生数千条 warning；直接改为 aware datetime 会与现有 SQLite naive `DateTime`、JWT/session 比较和 ISO 字符串契约冲突。
- **【影响范围】** 时间来源实现，不改变数据库字段、JSON 字符串格式、JWT claim、Cron 语义或 API 响应。Prompt、Tool、Memory、Workflow 与 Context 不变。
- **【风险等级】** 中。该步骤只移除弃用 API并集中旧契约，不宣称数据库已经完成 timezone-aware schema 迁移。
- **【验证方式】** 新增时钟契约测试，确认返回值仍为 naive UTC；认证、项目数据库、Cron、调度组合 110 项通过；完整后端 warning 从 4446 降至 3，剩余均来自第三方导入。
- **【为什么这是最优方案】** 先集中时钟再迁移存储，可消除运行时弃用风险，同时避免一次性改变持久化和序列化格式。

## 3. LanceDB 表枚举分页与版本兼容

- **【修改内容】** 当前 LanceDB 使用分页 `list_tables()` 并遍历 page token；旧版本没有该 API 时才回退 `table_names()`。
- **【修改原因】** `table_names()` 已弃用且默认分页可能只返回前 10 个表；当 `rag_documents` 不在首批时，初始化可能误判表不存在并尝试重复创建。
- **【影响范围】** 仅 RAG store 初始化。文档 schema、检索、embedding、RAG Prompt、Memory、Workflow 和 Context 不变。
- **【风险等级】** 中。误判会导致 RAG 初始化失败；直接只调用新 API 又会破坏 requirements 允许的旧 LanceDB 客户端。
- **【验证方式】** 新增当前 API 两页枚举与旧客户端回退两项测试；实际 LanceDB CRUD 与完整后端回归通过，相关弃用 warning 消失。
- **【为什么这是最优方案】** 同时解决分页正确性和弃用问题，不强制在本批做 LanceDB 主版本迁移。

## 4. Python 直接依赖安全升级

- **【修改内容】** `python-jose` 3.3.0→3.5.0、`python-multipart` 0.0.17→0.0.31、`lxml` 5.3.0→6.1.0、`Jinja2` 3.1.4→3.1.6。
- **【修改原因】** 固定旧版本包含已发布修复：python-jose 3.4.0 修复 CVE-2024-33663/33664；python-multipart 旧版包含 multipart 头部 CPU DoS；lxml 6.1.0 修复 XML 外部实体风险；Jinja2 3.1.5+ 修复 sandbox 绕过。参考 [python-jose changelog](https://github.com/mpdavis/python-jose/blob/master/CHANGELOG.md)、[python-multipart advisory](https://github.com/advisories/GHSA-pp6c-gr5w-3c5g)、[lxml 6.1.0 release](https://github.com/lxml/lxml/releases/tag/lxml-6.1.0)、[Jinja advisory](https://github.com/advisories/GHSA-q2x7-8rv6-6q7h)。
- **【影响范围】** 后端安装解析、JWT、multipart、XML/JMX 和模板运行库。业务 API、认证算法限制、Prompt、Tool、Memory、Workflow 与 Context 不变。
- **【风险等级】** 高。安全升级使用了 Python 3.10+ 兼容版本，符合项目 README 的最低版本声明。
- **【验证方式】** 四个准确版本安装到外部隔离目录；76 项认证、上传、JMX/XML 定向测试及 1433 项完整后端测试通过。固定直接依赖的 `pip-audit --no-deps` 结果为“无已知漏洞”。完整传递依赖解析因 `lancedb>=0.5.0` 未锁定而未被当作已完成审计。
- **【为什么这是最优方案】** 使用上游明确修复版本，不自制密码学、multipart 或 XML 补丁；只更新 requirements，不改变认证协议。

## 5. 前端兼容范围内的依赖修复

- **【修改内容】** 运行非强制 `npm audit fix`，只更新 `package-lock.json` 中现有 semver 范围可接受的版本：包括 Axios 1.20.0、Markdown-it 14.3.1、linkify-it 5.0.2、form-data 4.0.6、PostCSS 8.5.26、nanoid 3.3.18、Undici 7.29.0 等。
- **【修改原因】** 初次审计发现 13 个漏洞节点，涉及请求构造、CRLF、原型污染、路径读取和多种 CPU/内存 DoS。
- **【影响范围】** 前端依赖锁文件；`package.json`、Vue 源码、组件/API 逻辑、Prompt、Tool、Memory、Workflow 与 Context 均未修改。
- **【风险等级】** 高。锁文件更新会改变全新安装结果，因此必须同时验证测试、类型检查和生产构建。
- **【验证方式】** 前端 8 个测试文件、104 项测试全部通过；`vue-tsc --noEmit` 与 Vite production build 成功，2503 modules transformed。审计从 13 个节点降至 4 个，仅剩需要主版本迁移的 ECharts/Vite 链路。
- **【为什么这是最优方案】** 先消除无需业务迁移的漏洞，不运行 `npm audit fix --force`，避免把 ECharts、vue-echarts 和 Vite 跨主版本风险混入缺陷修复。

## 6. POSIX 协调模式复核结论

- **【修改内容】** 未修改运行时代码。确认启动锁在非 Windows 使用标准 `fcntl.flock`，但完整协调还依赖本地持久文件系统证明和不会因 PID 复用误判的精确进程创建时间。
- **【修改原因】** macOS/BSD 没有当前 Linux `/proc` 身份来源；使用 `ps` 文本或 `psutil.create_time()` 浮点值会降低死进程判定证明，可能错误回收仍活跃 owner。
- **【影响范围】** 无代码影响；Windows/Linux 既有范围不变，macOS/BSD 协调模式继续 fail closed，默认单进程使用不受影响。
- **【风险等级】** 中。开源文档必须明确支持边界，不能把“POSIX 有 flock”误写成完整协调已支持。
- **【验证方式】** 复核 `_require_local_disk`、`coordination_domain`、`current_identity/probe` 和真实双进程锁测试；完整回归继续通过。
- **【为什么这是最优方案】** 在没有真实平台和精确身份来源时保持安全拒绝，优于实现一个可能错误接管活进程的伪跨平台方案；这与暂缓 JMeter 进程隔离是两件独立事项。

## 7. 最终验证

| 检查 | 结果 |
|---|---|
| 后端完整隔离回归 | **1433 passed / 0 failed / 147.62s**；加载本批准确安全依赖与 SQLite 3.53.4 |
| 后端 warning | **3**；本批前为 4446。剩余为 Starlette import 与两个可选 embedding 类的第三方 warning |
| 前端测试 | **8 files / 104 tests passed** |
| 前端类型与构建 | `vue-tsc --noEmit`、Vite build 成功；2503 modules transformed |
| Python 静态复扫 | 161 个 Python 文件解析成功；152 个源码文件扫描；0 syntax error |
| Prompt/Tool/MCP/Memory | Prompt 常量、Tool 定义、MCP Prompt 注册/渲染、Memory 实现均未修改 |
| Python 直接依赖审计 | 固定本批实际 LanceDB 0.34.0 后，`pip-audit --no-deps`：**No known vulnerabilities found** |
| 前端审计 | 13 个漏洞节点降至 4 个；剩余均要求主版本迁移 |

## 8. 仍需确认的架构/主版本迁移

### 8.1 ECharts 6 + vue-echarts 8

- **影响范围：** 报告页图表组件、option 类型、渲染和视觉回归。优先级：**中**。
- **利弊分析：** 升级可清除审计中的 CVE-2026-45249；但属于两个直接依赖的主版本迁移。该漏洞要求 ECharts `Lines` series、默认 tooltip 和不可信 `series.data[i].name` 同时存在；当前项目只注册 `BarChart` 与 `LineChart`，没有注册受影响的 `Lines` series，因此当前代码路径不可达。参考 [ECharts advisory](https://github.com/advisories/GHSA-fgmj-fm8m-jvvx)。
- **推荐方案：** 独立升级 `echarts>=6.1.0` 与兼容的 `vue-echarts>=8`，增加报告图表挂载和 tooltip 内容测试，并做浏览器视觉验证后合并。
- **为什么等待确认：** 主版本迁移可能改变图表展示，直接实施违反“影响多个模块先确认”的约束。

### 8.2 Vite 主版本迁移

- **影响范围：** 本地开发服务器、Vue 插件、构建链和 Node 版本矩阵。优先级：**高（开发环境）/低（生产产物）**。
- **利弊分析：** 升级能清除 Vite 路径读取/UNC 与 esbuild dev-server 风险；当前生产构建不包含 Vite server，配置也未把 dev server 暴露到非 loopback。直接按 audit 建议跳到 Vite 8 会跨多个主版本。
- **推荐方案：** 先验证 Vite 6.4.3 或最新受支持修复线能否与 `@vitejs/plugin-vue`、Node 版本和现有配置兼容；若不能，再规划 Vite 8。增加开发服务器 host/CORS/文件边界测试。参考 [esbuild advisory](https://github.com/advisories/GHSA-67mh-4wv8-2f99)。
- **为什么等待确认：** 这是开发工具链迁移，不应由 `npm audit fix --force` 自动决定。

### 8.3 可复现依赖锁与开源 CI

- **影响范围：** Python 跨平台安装、SQLite 协调测试、发布流程与 GitHub Actions。优先级：**高**。
- **利弊分析：** 当前 `lancedb>=0.5.0` 使同一 requirements 在不同时间安装不同实现，也阻止无需解析的完整漏洞审计；现有 GitHub workflow 只调用外部 Agent 执行性能计划，没有运行后端单测、前端测试或构建。引入按 Python/OS 生成的哈希锁和 Windows/Linux/macOS CI 可显著提高开源可信度，但需要决定 Python 版本矩阵、可选 AI 依赖分组及 patched SQLite 的供应方式。
- **推荐方案：** 使用 `pip-compile`/`uv lock` 生成可审计的基础与 AI 可选依赖锁；新增独立 CI，Windows/Linux 跑完整协调测试，macOS 先跑默认单进程测试并明确协调 fail-closed 预期，前端统一跑 `npm ci`、test、build、audit policy。
- **为什么等待确认：** 锁格式、平台矩阵和 SQLite 供应链会影响所有开源使用者，应作为单独可回滚的交付项确认后实施。

平台/JMeter 进程隔离、执行停止证明、自动恢复、跨进程执行接管和默认多 worker 继续保持延期。
