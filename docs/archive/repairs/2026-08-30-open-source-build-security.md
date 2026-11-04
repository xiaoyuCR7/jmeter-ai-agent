# 开源构建、跨平台 CI 与前端安全升级报告

本批实施基于已批准设计：
`docs/superpowers/specs/2026-08-30-open-source-build-security-design.md`。
未实施 JMeter/平台进程隔离，也未改变 Agent 的业务目标、Prompt、Tool、Memory、Workflow 或 Context。

## 1. Python 运行时与开发依赖锁

【修改内容】新增 `requirements.in`、`requirements-dev.in`；将 `requirements.txt` 与
`requirements-dev.txt` 改为 pip-tools 在 Python 3.11 上生成的完整精确锁；LanceDB 从
`>=0.5.0` 固定为已经过全量测试的 `0.34.0`。

【修改原因】原文件只固定直接依赖且允许 LanceDB 任意升级，同一次安装在不同日期可能得到
不同的 RAG、Arrow、SQLite 相关实现，也无法完整审计传递依赖。

【影响范围】依赖安装、RAG/LanceDB 运行时、开发测试环境；不修改业务代码调用方式。

【风险等级】中。全量精确锁会把传递版本变化从安装时转移到显式更新时，维护者需要主动更新锁。

【验证方式】运行时锁与开发锁连续重建 SHA-256 完全一致；Python 3.11 干净环境安装 94 个包，
`pip check` 无冲突，便携后端测试 1296 通过；同一锁在 Python 3.13 外置依赖目录完成 1440 项
全量测试。

【为什么这是最优方案】保留用户原有 `pip install -r requirements.txt` 入口，只增加源文件和生成
约定；没有引入新的包管理器或平台专用生产锁。

## 2. Python 最低版本与 SSE 语法兼容

【修改内容】最低 Python 版本从 3.10 更正为 3.11；把工作流完成 SSE 事件的多行字典 f-string
改为先构造字典再序列化。

【修改原因】干净 Python 3.10 验证发现项目现有运行时使用 `asyncio.timeout`、
`Task.cancelling()`，测试契约使用 `ExceptionGroup`，均依赖 Python 3.11；另有一处 f-string
需要 Python 3.12 才能解析。原“3.10+”声明不真实。

【影响范围】安装文档、CI 版本矩阵、`app/api/routes.py` 的工作流完成 SSE 序列化。

【风险等级】中。仍在使用 Python 3.10 的用户需升级解释器；SSE JSON 字段和值完全不变。

【验证方式】Python 3.11 对 `app` 与 `tests` 完整解析成功；干净 3.11 便携测试 1296 通过；
Python 3.13 + SQLite 3.53.4 全量 1440 通过。

【为什么这是最优方案】用文档和 CI 表达代码的真实并发语义，避免为已不存在的 3.10 支持添加
无法准确模拟 `Task.cancelling()` 的兼容分支；SSE 仅做局部等价改写。

## 3. 跨平台质量 CI

【修改内容】新增 `.github/workflows/quality.yml`，包含锁漂移检查、Ubuntu Python 3.11/3.13、
Windows Python 3.13、macOS Python 3.13 便携测试、Linux 安全 SQLite 全量测试，以及前端审计、
测试和构建。

【修改原因】原 GitHub workflow 只调用已部署 Agent 执行远程性能计划，不能阻止代码、依赖锁、
跨平台或前端构建回归。

【影响范围】GitHub Actions；不改变原性能门禁、Secrets 或定时任务。

【风险等级】低。增加 CI 时间与 PyPI/npm 网络依赖；矩阵采用边界版本而非所有版本的笛卡尔积，
控制资源消耗。

【验证方式】YAML 已解析为 4 个 job；每条核心命令已在本地等价执行。GitHub 托管的 Linux/macOS
实例本机无法代跑，首次推送仍需观察真实 Actions 结果。

【为什么这是最优方案】新建独立 workflow，不改已有性能门禁；三平台运行普通测试，只在 Linux
提供额外安全 SQLite 绑定执行完整协调回归，避免虚假宣称 macOS 已支持本地跨进程协调。

## 4. 协调测试通道与 SQLite 引导

【修改内容】给真实协调测试增加 `requires_patched_sqlite` 标记；新增 Linux
`pysqlite3-binary` 测试引导、与生产完全一致的 WAL-reset 安全版本判断，并让子进程测试继承该
绑定。

【修改原因】常见 Python 发行版可能绑定仍受 WAL-reset 缺陷影响的 SQLite；直接跑全量会安全
失败，而跳过整个后端又会失去大部分跨平台覆盖。

【影响范围】pytest 配置、9 个协调测试模块、5 个惰性测试子进程；生产协调实现不变。

【风险等级】低。标记错误可能减少便携通道覆盖，因此完整 Linux job 不使用标记过滤。

【验证方式】本机未修补 SQLite 的便携通道 1296 通过、精确跳过 144；官方 SQLite 3.53.4
Windows DLL 经官方 SHA3-256 校验后，全量 1440 通过。版本判断边界测试覆盖 3.44.6、3.50.7、
3.51.3。

【为什么这是最优方案】既不放宽生产故障关闭，也不在仓库中分发平台二进制；CI 专用包有固定
版本且运行前再次检查实际 SQLite 绑定。

## 5. Vite 安全迁移

【修改内容】Vite 从 5.x 升至精确版本 6.4.3；保留现有 `@vitejs/plugin-vue 5.2.4` 和
`vite.config.ts`。

【修改原因】旧 Vite/esbuild 命中开发服务器跨源读取、路径遍历和 Windows UNC/路径处理公告。

【影响范围】前端开发服务器、测试转换和生产打包；不影响 Vue 业务源码或后端通信协议。

【风险等级】中。属于构建工具主版本升级。

【验证方式】`npm audit` 为 0；8 个测试文件、104 项断言通过；vue-tsc 与 Vite 6.4.3 生产构建
成功，转换 2545 个模块。

【为什么这是最优方案】选择首个越过当前受影响范围且仍被现有 Vue 插件明确支持的 6.4.3，
没有直接跳到 Vite 8，也没有改构建配置。

## 6. ECharts / vue-echarts 安全迁移

【修改内容】ECharts 升至 6.1.0，vue-echarts 升至匹配的 8.2.0；Report 页面仍只注册 BarChart
与 LineChart。

【修改原因】ECharts 6.1.0 前版本存在 Lines 系列名称进入默认 tooltip 的 XSS 公告；旧
vue-echarts 版本又限制 ECharts 主版本，需成对升级。

【影响范围】报告图表渲染依赖；图表数据、组件 props、业务页面不变。

【风险等级】中。两个前端运行时依赖均跨主版本。

【验证方式】`npm audit` 为 0；104 项前端测试、类型检查和生产构建全部通过；应用源码零修改。

【为什么这是最优方案】只更新必要的匹配依赖，不借机重写图表组件；现有代码没有注册公告中的
Lines 系列，但开源依赖树仍消除了漏洞版本。

## 7. FastAPI / Starlette 安全迁移

【修改内容】将 FastAPI 从 0.115.0 升至 0.136.3，显式固定 Starlette 1.3.1；删除启动时全局替换
`APIRouter` 的旧兼容补丁，改用该版本组合的原生路由实现。

【修改原因】旧 Starlette 锁命中多条已知安全公告；旧补丁为不受支持的版本拼接而存在，继续保留
会绕过新版 FastAPI 的路由初始化字段和生命周期逻辑。

【影响范围】全部 HTTP 路由、中间件、上传、静态报告、SSE、WebSocket、异常响应和 OpenAPI；
Prompt、Tool、Memory、Workflow、Context 与业务接口未改。

【风险等级】中。依赖跨越多个版本，但变更集中在框架边界并由合同测试覆盖。

【验证方式】新依赖目录中 199 项认证/API/OpenAPI/上传/SSE/WebSocket 定向测试通过；Python 3.13
全量 1440 项和 Python 3.11 便携 1296 项测试通过；实际版本打印为 0.136.3/1.3.1。

【为什么这是最优方案】使用官方支持的版本组合并删除失去必要性的全局猴子补丁，不在业务路由
中添加兼容分支，也不借升级重构接口。

## 8. PyJWT 替换 python-jose / ecdsa

【修改内容】用 `PyJWT[crypto]==2.13.0` 替换 `python-jose[cryptography]`；认证服务仅把 JWT 导入和
异常基类映射为 PyJWT 对应接口，保留载荷、算法白名单、过期时间、Session 绑定和刷新轮换逻辑。

【修改原因】`python-jose` 的依赖树引入没有修复版本的 `ecdsa` Minerva 侧信道公告；项目默认
HS256 不需要该实现，可配置的非对称算法则继续由 `cryptography` 提供。

【影响范围】Access Token 的签发、解析和无效 Token 异常处理；Refresh Token 本身仍是随机串并
以 SHA-256 摘要持久化，数据库结构和前后端协议不变。

【风险等级】中。不同 JWT 库的 claim 默认校验细节可能不同，因此加入旧格式 Token 兼容契约。

【验证方式】标准方式构造的旧 HS256 Access Token 可由新实现完整解码；登录、轮换、注销、过期、
角色、Cookie、SSE 与 WebSocket 撤销测试通过；锁内不再包含 `python-jose` 或 `ecdsa`；
`pip-audit --no-deps -r requirements.txt` 报告无已知漏洞。

【为什么这是最优方案】只替换存在问题的 JWT 实现层，以异常别名保持调用结构稳定；没有改变
Token 格式、认证职责、配置入口或权限模型。

## 验证汇总

| 验证项 | 结果 |
|---|---|
| Python 3.11 干净安装 | 94 个精确开发包；`pip check` 无冲突 |
| 锁文件重建 | runtime/dev 两个 SHA-256 均稳定 |
| Python 3.11 便携后端 | 1296 passed / 144 deselected / 3 warnings / 154.21s |
| Python 3.13 + SQLite 3.53.4 全量后端 | 1440 passed / 3 warnings / 148.17s |
| Python 运行时锁审计 | 0 known vulnerabilities |
| 前端审计 | 0 vulnerabilities |
| 前端测试 | 8 files / 104 passed |
| 前端构建 | vue-tsc + Vite 6.4.3；2545 modules transformed |
| YAML | 4 个质量 job 解析成功 |

## 最终复扫结论

- 本批未修改 Prompt、Tool Calling、Memory、Workflow、Planning/ReAct、RAG 查询或 Context。
- 没有新增模型/API 调用、Tool 调用、Memory 查询或 Token 消耗。
- Python 3.11/3.13 编译与全量回归未发现本批引入的业务 Bug。
- 冻结后 AST 复扫成功解析 162 个后端源码与测试文件（删除了一个旧兼容补丁文件）。
- 前端迁移没有修改应用源码，测试、类型检查、构建和审计全部通过。
- 旧 FastAPI 补丁、`python-jose` 与 `ecdsa` 在当前源码和依赖锁中均无残留；历史修复报告保留
  当时事实，不作为当前依赖说明。
- 本批复扫未发现新增 Bug、Prompt/Tool/Memory/Workflow 问题或 Token 浪费；不能由测试证明项目
  永远不存在未知缺陷。
