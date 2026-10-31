/**
 * useI18n — lightweight i18n composable for Vue 3 components.
 *
 * Uses the Pinia app store for language state. Translations are defined
 * inline (no JSON loading for the frontend) to keep it zero-latency.
 *
 * Usage:
 *   const { t, currentLang, setLang } = useI18n()
 *   t('settings.language')                         // '界面语言' / 'Language'
 *   t('env.variableCount', { count: varCount })    // '{{ count }} 个变量'
 */

import { computed } from 'vue'
import { useAppStore, type SupportedLang } from '@/store'

type TranslationValue = Record<SupportedLang, string>
type Translations = Record<string, TranslationValue>

const messages: Translations = {
  'editor.view.tree': { 'zh-CN': 'JMX 树视图', en: 'JMX tree' },
  'editor.view.form': { 'zh-CN': '表单视图', en: 'Form' },
  'layout.start': { 'zh-CN': '从文档开始', en: 'Start from a document' },
  'layout.nav.projects': { 'zh-CN': '项目', en: 'Projects' },
  'route.start': { 'zh-CN': '从文档开始', en: 'Start from a document' },
  'route.projects': { 'zh-CN': '项目', en: 'Projects' },
  // ── Settings page (partial, view will fill the rest) ──
  'settings.language': { 'zh-CN': '界面语言', en: 'Language' },
  'settings.languageHint': { 'zh-CN': '切换后立即生效，测试报告和AI输出将使用所选语言。', en: 'Takes effect immediately. Test reports and AI output will use the selected language.' },
  'settings.language.chinese': { 'zh-CN': '中文', en: '中文' },
  'settings.appearance': { 'zh-CN': '外观', en: 'Appearance' },
  'settings.theme': { 'zh-CN': '界面主题', en: 'Theme' },
  'settings.themeHint': { 'zh-CN': '切换后会立即生效，并保存在本地。', en: 'Takes effect immediately and saved locally.' },
  'settings.light': { 'zh-CN': '浅色', en: 'Light' },
  'settings.dark': { 'zh-CN': '深色', en: 'Dark' },
  'settings.systemInfo': { 'zh-CN': '系统信息', en: 'System Info' },
  'settings.save': { 'zh-CN': '保存', en: 'Save' },
  'settings.cancel': { 'zh-CN': '取消', en: 'Cancel' },
  'settings.addModel': { 'zh-CN': '添加模型', en: 'Add Model' },
  'settings.aiModelConfig': { 'zh-CN': 'AI模型配置', en: 'AI Model Config' },
  'settings.info.platform': { 'zh-CN': '平台', en: 'Platform' },
  'settings.info.pythonVersion': { 'zh-CN': 'Python版本', en: 'Python Version' },
  'settings.info.executionMode': { 'zh-CN': '执行模式', en: 'Execution Mode' },
  'settings.info.modeLocal': { 'zh-CN': '本机进程', en: 'Local process' },
  'settings.info.modeContainer': { 'zh-CN': 'Docker 容器', en: 'Docker container' },
  'settings.info.jmeterImage': { 'zh-CN': 'JMeter镜像', en: 'JMeter Image' },
  'settings.info.notConfigured': { 'zh-CN': '未配置', en: 'Not configured' },
  'settings.message.pathsChangeTitle': { 'zh-CN': '更改目录？', en: 'Change folders?' },
  'settings.message.pathsChangeWarning': { 'zh-CN': '已有文件不会迁移，旧目录里的计划和报告可能无法在应用里打开。智能体工具也将可以访问新目录。', en: 'Existing files are not moved, so plans and reports in the old folders may stop opening in the app. Agent tools will also be able to reach the new folders.' },
  'settings.info.jmeterHome': { 'zh-CN': 'JMeter安装目录', en: 'JMeter Home' },
  'settings.info.jmeterExecutable': { 'zh-CN': 'JMeter可执行文件', en: 'JMeter Executable' },
  'settings.info.testPlansDir': { 'zh-CN': '测试计划目录', en: 'Test Plans Directory' },
  'settings.info.reportsDir': { 'zh-CN': '报告目录', en: 'Reports Directory' },
  'settings.info.notFound': { 'zh-CN': '未找到', en: 'Not found' },
  'settings.info.testPlansDirPlaceholder': { 'zh-CN': '请输入测试计划目录', en: 'Enter test plans directory' },
  'settings.info.reportsDirPlaceholder': { 'zh-CN': '请输入报告目录', en: 'Enter reports directory' },

  // ── Navigation ──

  // ── AI Workflow ──

  'editor.dnsHosts.title': { 'zh-CN': '静态主机映射', en: 'Static Host Mapping' },
  'editor.dnsHosts.hostname': { 'zh-CN': '主机名', en: 'Hostname' },
  'editor.dnsHosts.ipAddress': { 'zh-CN': 'IP 地址', en: 'IP Address' },
  'editor.dnsHosts.add': { 'zh-CN': '添加主机', en: 'Add Host' },

  'editor.auth.title': { 'zh-CN': '授权条目', en: 'Authorization Entries' },
  'editor.auth.username': { 'zh-CN': '用户名', en: 'Username' },
  'editor.auth.password': { 'zh-CN': '密码', en: 'Password' },
  'editor.auth.domain': { 'zh-CN': '域', en: 'Domain' },
  'editor.auth.add': { 'zh-CN': '添加授权', en: 'Add Authorization' },

  'editor.headers.title': { 'zh-CN': '请求头列表', en: 'Request Headers' },
  'editor.headers.name': { 'zh-CN': '名称', en: 'Name' },
  'editor.headers.value': { 'zh-CN': '值', en: 'Value' },
  'editor.headers.add': { 'zh-CN': '添加请求头', en: 'Add Header' },
  'editor.common.delete': { 'zh-CN': '删除', en: 'Delete' },

  'editor.cookies.title': { 'zh-CN': 'Cookie 列表', en: 'Cookie List' },
  'editor.cookies.name': { 'zh-CN': '名称', en: 'Name' },
  'editor.cookies.value': { 'zh-CN': '值', en: 'Value' },
  'editor.cookies.domain': { 'zh-CN': '域名', en: 'Domain' },
  'editor.cookies.path': { 'zh-CN': '路径', en: 'Path' },
  'editor.cookies.add': { 'zh-CN': '添加 Cookie', en: 'Add Cookie' },

  'editor.params.title': { 'zh-CN': '请求参数', en: 'Request Parameters' },
  'editor.params.name': { 'zh-CN': '名称', en: 'Name' },
  'editor.params.value': { 'zh-CN': '值', en: 'Value' },
  'editor.params.encode': { 'zh-CN': '编码', en: 'Encode' },
  'editor.params.add': { 'zh-CN': '添加参数', en: 'Add Parameter' },

  'editor.userParams.title': { 'zh-CN': '参数名称', en: 'Parameter Names' },
  'editor.userParams.values': { 'zh-CN': '用户值（每行对应一个线程用户）', en: 'User Values' },
  'editor.userParams.addColumn': { 'zh-CN': '+列', en: '+Column' },
  'editor.userParams.removeColumn': { 'zh-CN': '-列', en: '-Column' },
  'editor.userParams.addRow': { 'zh-CN': '添加用户行', en: 'Add User Row' },
  'editor.userParams.param': { 'zh-CN': '参数', en: 'Param' },

  'env.selector.placeholder': { 'zh-CN': '选择环境', en: 'Select Environment' },
  'env.selector.manage': { 'zh-CN': '管理环境', en: 'Manage Environments' },
  'env.selector.url': { 'zh-CN': '未配置URL', en: 'URL Not Configured' },
  'env.selector.variables': { 'zh-CN': '个变量', en: ' variables' },
  'env.selector.noVariables': { 'zh-CN': '暂无环境变量', en: 'No environment variables' },
  'env.selector.copy': { 'zh-CN': '复制环境', en: 'Copy Environment' },
  'env.selector.copySuccess': { 'zh-CN': '环境"{name}"已复制', en: 'Environment "{name}" copied' },

  'editor.assertionPatterns.title': { 'zh-CN': '要测试的模式', en: 'Patterns to Test' },
  'editor.assertionPatterns.add': { 'zh-CN': '添加模式', en: 'Add Pattern' },

  'editor.functionHelper.title': { 'zh-CN': '函数助手', en: 'Function Helper' },
  'editor.functionHelper.select': { 'zh-CN': '选择函数', en: 'Select Function' },
  'editor.functionHelper.preview': { 'zh-CN': '预览', en: 'Preview' },
  'editor.functionHelper.example': { 'zh-CN': '示例:', en: 'Example:' },
  'editor.functionHelper.cancel': { 'zh-CN': '取消', en: 'Cancel' },
  'editor.functionHelper.insert': { 'zh-CN': '插入', en: 'Insert' },

  'editor.variables.title': { 'zh-CN': '变量列表', en: 'Variable List' },
  'editor.variables.name': { 'zh-CN': '名称', en: 'Name' },
  'editor.variables.value': { 'zh-CN': '值', en: 'Value' },
  'editor.variables.desc': { 'zh-CN': '描述', en: 'Description' },
  'editor.variables.add': { 'zh-CN': '添加变量', en: 'Add Variable' },
  'editor.variables.export': { 'zh-CN': '导出CSV', en: 'Export CSV' },
  'editor.variables.import': { 'zh-CN': '导入CSV', en: 'Import CSV' },
  'editor.variables.exportSuccess': { 'zh-CN': '变量导出成功', en: 'Variables exported successfully' },
  'editor.variables.importSuccess': { 'zh-CN': '成功导入{{ count }}个变量', en: 'Successfully imported {{ count }} variables' },
  'editor.variables.importEmpty': { 'zh-CN': 'CSV文件中没有有效数据', en: 'No valid data in CSV file' },
  'editor.variables.importError': { 'zh-CN': 'CSV文件格式错误', en: 'CSV file format error' },

  'editor.csvDataset': { 'zh-CN': 'CSV数据集', en: 'CSV Data Set' },
  'editor.csv.filePath': { 'zh-CN': '文件路径', en: 'File Path' },
  'editor.csv.fileHint': { 'zh-CN': '相对于JMeter工作目录的路径', en: 'Path relative to JMeter working directory' },
  'editor.csv.variableNames': { 'zh-CN': '变量名称', en: 'Variable Names' },
  'editor.csv.delimiter': { 'zh-CN': '分隔符', en: 'Delimiter' },
  'editor.csv.encoding': { 'zh-CN': '编码', en: 'Encoding' },
  'editor.csv.ignoreFirstLine': { 'zh-CN': '忽略首行', en: 'Ignore First Line' },
  'editor.csv.recycle': { 'zh-CN': '循环读取', en: 'Recycle on EOF' },
  'editor.csv.preview': { 'zh-CN': '预览数据', en: 'Preview Data' },
  'editor.csv.previewError': { 'zh-CN': '预览失败', en: 'Preview failed' },

  'common.unnamed': { 'zh-CN': '未命名', en: 'Unnamed' },


  'mcpConnect.tools.claude': { 'zh-CN': 'Claude Code', en: 'Claude Code' },
  'mcpConnect.tools.cursor': { 'zh-CN': 'Cursor', en: 'Cursor' },
  'mcpConnect.tools.vscode': { 'zh-CN': 'VS Code / Copilot', en: 'VS Code / Copilot' },
  'mcpConnect.tools.trae': { 'zh-CN': 'Trae IDE', en: 'Trae IDE' },
  'mcpConnect.tools.generic': { 'zh-CN': '通用 / cURL', en: 'Generic / cURL' },
  'mcpConnect.claude.hint': { 'zh-CN': '配置完成后，重启 Claude Code 即可在聊天中使用 JMeter Agent。', en: 'After configuration, restart Claude Code to use JMeter Agent in chat.' },
  'mcpConnect.cursor.instruction': { 'zh-CN': '在 Cursor 中，创建或编辑 .cursor/mcp.json 文件，添加以下配置：', en: 'In Cursor, create or edit the .cursor/mcp.json file and add the following configuration:' },
  'mcpConnect.cursor.hint': { 'zh-CN': '配置完成后，重启 Cursor 即可在聊天中使用 JMeter Agent。', en: 'After configuration, restart Cursor to use JMeter Agent in chat.' },
  'mcpConnect.trae.instruction': { 'zh-CN': '在 Trae IDE 中，创建或编辑 mcp.json 文件，添加以下配置：', en: 'In Trae IDE, create or edit the mcp.json file and add the following configuration:' },
  'mcpConnect.trae.hint': { 'zh-CN': '配置完成后，重启 Trae IDE 或在 MCP 面板中刷新服务器列表。', en: 'After configuration, restart Trae IDE or refresh the server list in the MCP panel.' },
  'mcpConnect.generic.instruction': { 'zh-CN': '使用 cURL 或其他支持 JSON-RPC 的工具测试连接：', en: 'Use cURL or other JSON-RPC compatible tools to test the connection:' },
  'mcpConnect.state.checking': { 'zh-CN': '正在检查 MCP 服务', en: 'Checking the MCP server' },
  'mcpConnect.state.online': { 'zh-CN': 'MCP 服务在线', en: 'MCP server online' },
  'mcpConnect.state.offline': { 'zh-CN': 'MCP 服务离线', en: 'MCP server offline' },
  'mcpConnect.summary': { 'zh-CN': '提供 {{ tools }} 个工具、{{ resources }} 个资源和 {{ prompts }} 个提示词', en: '{{ tools }} tools, {{ resources }} resources and {{ prompts }} prompts available' },
  'mcpConnect.offlineHelp': { 'zh-CN': '后端没有响应。确认后端已启动，再点测试连接。', en: 'The backend is not responding. Make sure it is running, then test the connection.' },
  'mcpConnect.chooseClient': { 'zh-CN': '选择要接入的客户端', en: 'Choose your client' },
  'mcpConnect.terminal': { 'zh-CN': '终端', en: 'Terminal' },
  'mcpConnect.claude.cli': { 'zh-CN': '在终端执行一次：', en: 'Run once in a terminal:' },
  'mcpConnect.claude.file': { 'zh-CN': '或者添加到项目根目录的 .mcp.json：', en: 'Or add this to .mcp.json in the project root:' },
  'mcpConnect.vscode.instruction': { 'zh-CN': '添加到 .vscode/mcp.json：', en: 'Add this to .vscode/mcp.json:' },
  'mcpConnect.tokenNote': { 'zh-CN': '配置里含有你的访问令牌，不要提交到代码仓库或发给别人。', en: 'These configs contain your access token. Keep them out of repositories and messages.' },
  'mcpConnect.showToken': { 'zh-CN': '显示令牌', en: 'Show token' },
  'mcpConnect.hideToken': { 'zh-CN': '隐藏令牌', en: 'Hide token' },
  'mcpConnect.copied': { 'zh-CN': '已复制', en: 'Copied' },
  'mcpConnect.copyUrl': { 'zh-CN': '复制', en: 'Copy' },
  'mcpConnect.usageTitle': { 'zh-CN': '接入后', en: 'Once connected' },
  'mcpConnect.usage.approval': { 'zh-CN': '修改内容或运行压测前，助手会等你在这个窗口批准，最多两分钟，请保持窗口打开。', en: 'Before changing anything or running a test, the assistant waits up to two minutes for your approval in this window, so keep it open.' },
  'mcpConnect.usage.background': { 'zh-CN': '压测在后台运行，助手用 get_execution_status 查询结果。', en: 'Tests run in the background; the assistant checks them with get_execution_status.' },
  'mcpConnect.usage.analysis': { 'zh-CN': '分析结果时，让助手使用 analyze-execution 提示词。', en: 'To analyse a run, ask the assistant to use the analyze-execution prompt.' },
  'mcpConnect.usage.sse': { 'zh-CN': '只支持 SSE 的旧客户端可以连接 /api/v1/mcp/sse。', en: 'Older SSE-only clients can connect to /api/v1/mcp/sse.' },
  'mcpConnect.allTools': { 'zh-CN': '查看全部 {{ count }} 个工具', en: 'See all {{ count }} tools' },
  'mcpConnect.searchTools': { 'zh-CN': '搜索工具', en: 'Search tools' },
  'mcpConnect.needsApproval': { 'zh-CN': '需要你批准', en: 'Need your approval' },
  'mcpConnect.runsDirectly': { 'zh-CN': '直接运行', en: 'Run directly' },
  'mcpConnect.noTools': { 'zh-CN': '没有匹配的工具。', en: 'No tools match.' },
  'mcpConnect.copy': { 'zh-CN': '复制', en: 'Copy' },
  'mcpConnect.testConnection': { 'zh-CN': '测试连接', en: 'Test Connection' },
  'mcpConnect.message.copyFailed': { 'zh-CN': '复制失败', en: 'Copy failed' },
  'mcpConnect.message.connectionSuccess': { 'zh-CN': '连接成功：{{ name }} v{{ version }}', en: 'Connection successful: {{ name }} v{{ version }}' },
  'mcpConnect.message.connectionFailed': { 'zh-CN': '连接失败{{ message }}', en: 'Connection failed{{ message }}' },

  // ── Workflow ──

  // ── Executions ──

  // ── Reports ──

  // ── Route titles ──
  'route.createTestPlan': { 'zh-CN': '创建测试计划', en: 'Create Test Plan' },
  'route.testCaseEditor': { 'zh-CN': '编辑测试计划', en: 'Edit test plan' },
  'route.projectSettings': { 'zh-CN': '项目设置', en: 'Project Settings' },
  'route.executions': { 'zh-CN': '执行记录', en: 'Runs' },
  'route.report': { 'zh-CN': '测试报告', en: 'Test Report' },
  'route.settings': { 'zh-CN': '设置', en: 'Settings' },

  // ── General ──

  // ── Assertion patterns editor ──

  // ── Assertions tab ──
  'assertion.type': { 'zh-CN': '断言类型', en: 'Assertion Type' },

  // ── Auth editor ──

  // ── Cookies editor ──

  // ── DNS hosts editor ──

  // ── Endpoint list ──

  // ── Environment selector ──

  // ── Function helper ──

  // ── Headers editor ──

  // ── HTTP params editor ──
  'param.name': { 'zh-CN': '名称', en: 'Name' },

  // ── Post-processor editor ──

  // ── Pre-processor editor ──

  // ── Response panel ──

  // ── User parameters editor ──

  // ── Variables editor ──

  // ── AI Chat view ──
  'aiChat.fileSizeError': { 'zh-CN': '文件大小不能超过 10MB', en: 'File size cannot exceed 10MB' },
  'aiChat.fileReadError': { 'zh-CN': '文件读取失败', en: 'File read failed' },
  'aiChat.fillRequired': { 'zh-CN': '请填写API文档和测试需求', en: 'Please fill in API docs and test requirements' },
  'aiChat.workflowFailed': { 'zh-CN': '工作流执行失败: {{ message }}', en: 'Workflow execution failed: {{ message }}' },

  // ── AutoTune view ──

  // ── Create test plan view ──
  'createPlan.step.template': { 'zh-CN': '选择模板', en: 'Select Template' },
  'createPlan.step.api': { 'zh-CN': '配置接口', en: 'Configure APIs' },
  'createPlan.step.settings': { 'zh-CN': '测试设置', en: 'Test Settings' },
  'createPlan.step.generate': { 'zh-CN': '生成计划', en: 'Generate Plan' },
  'createPlan.typeTitle': { 'zh-CN': '选择测试类型', en: 'Select Test Type' },
  'createPlan.next': { 'zh-CN': '下一步', en: 'Next' },
  'createPlan.configApi': { 'zh-CN': '配置API接口', en: 'Configure API Endpoints' },
  'createPlan.baseUrl': { 'zh-CN': '基础URL', en: 'Base URL' },
  'createPlan.apiDoc': { 'zh-CN': 'API文档（可选，支持OpenAPI/Swagger或纯文本）', en: 'API Docs (optional, supports OpenAPI/Swagger or plain text)' },
  'createPlan.apiDocPlaceholder': { 'zh-CN': '粘贴API接口文档内容，AI将自动解析接口信息', en: 'Paste API doc content; AI will parse endpoint information' },
  'createPlan.orManual': { 'zh-CN': '或手动添加接口', en: 'or add endpoints manually' },
  'createPlan.apiTitle': { 'zh-CN': '接口 {{ number }}', en: 'Endpoint {{ number }}' },
  'createPlan.delete': { 'zh-CN': '删除', en: 'Delete' },
  'createPlan.name': { 'zh-CN': '名称', en: 'Name' },
  'createPlan.namePlaceholder': { 'zh-CN': '用户登录', en: 'User Login' },
  'createPlan.method': { 'zh-CN': '方法', en: 'Method' },
  'createPlan.path': { 'zh-CN': '路径', en: 'Path' },
  'createPlan.expectedStatus': { 'zh-CN': '预期状态码', en: 'Expected Status Code' },
  'createPlan.auth': { 'zh-CN': '认证', en: 'Authentication' },
  'createPlan.auth.none': { 'zh-CN': '无', en: 'None' },
  'createPlan.username': { 'zh-CN': '用户名', en: 'Username' },
  'createPlan.password': { 'zh-CN': '密码', en: 'Password' },
  'createPlan.headers': { 'zh-CN': '请求头', en: 'Headers' },
  'createPlan.headerNamePlaceholder': { 'zh-CN': 'Header名', en: 'Header name' },
  'createPlan.headerValuePlaceholder': { 'zh-CN': 'Header值', en: 'Header value' },
  'createPlan.addHeader': { 'zh-CN': '添加请求头', en: 'Add Header' },
  'createPlan.queryParams': { 'zh-CN': '查询参数', en: 'Query Parameters' },
  'createPlan.paramNamePlaceholder': { 'zh-CN': '参数名', en: 'Parameter name' },
  'createPlan.paramValuePlaceholder': { 'zh-CN': '参数值', en: 'Parameter value' },
  'createPlan.addParam': { 'zh-CN': '添加查询参数', en: 'Add Query Parameter' },
  'createPlan.requestBody': { 'zh-CN': '请求体 (JSON)', en: 'Request Body (JSON)' },
  'createPlan.addApi': { 'zh-CN': '添加接口', en: 'Add Endpoint' },
  'createPlan.prev': { 'zh-CN': '上一步', en: 'Previous' },
  'createPlan.testConfig': { 'zh-CN': '测试配置', en: 'Test Configuration' },
  'createPlan.concurrency': { 'zh-CN': '并发用户数', en: 'Concurrent Users' },
  'createPlan.rampUp': { 'zh-CN': 'Ramp-up时间 (秒)', en: 'Ramp-up Time (seconds)' },
  'createPlan.duration': { 'zh-CN': '持续时间 (秒)', en: 'Duration (seconds)' },
  'createPlan.targetRps': { 'zh-CN': '目标RPS', en: 'Target RPS' },
  'createPlan.targetRpsUnlimited': { 'zh-CN': '不填则不限速', en: 'Leave empty for no limit' },
  'createPlan.thinkTime': { 'zh-CN': '思考时间 (ms)', en: 'Think Time (ms)' },
  'createPlan.iterations': { 'zh-CN': '迭代次数 (可选)', en: 'Iterations (optional)' },
  'createPlan.selectModel': { 'zh-CN': '选择AI模型', en: 'Select AI Model' },
  'createPlan.selectModelPlaceholder': { 'zh-CN': '选择AI模型', en: 'Select AI model' },
  'createPlan.generating': { 'zh-CN': 'AI正在生成测试计划...', en: 'AI is generating the test plan...' },
  'createPlan.success': { 'zh-CN': '测试计划生成成功', en: 'Test plan generated successfully' },
  'createPlan.planId': { 'zh-CN': '测试计划ID: {{ id }}', en: 'Test Plan ID: {{ id }}' },
  'createPlan.apiCount': { 'zh-CN': '接口数量: {{ count }}', en: 'Endpoint Count: {{ count }}' },
  'createPlan.runNow': { 'zh-CN': '立即执行', en: 'Run Now' },
  'createPlan.backToList': { 'zh-CN': '返回列表', en: 'Back to List' },
  'createPlan.generate': { 'zh-CN': '生成测试计划', en: 'Generate Test Plan' },
  'createPlan.message.success': { 'zh-CN': '测试计划生成成功', en: 'Test plan generated successfully' },
  'createPlan.message.failed': { 'zh-CN': '生成失败', en: 'Generation failed' },
  'createPlan.message.runStarted': { 'zh-CN': '测试已开始执行', en: 'Test execution started' },
  'createPlan.message.runFailed': { 'zh-CN': '执行失败', en: 'Execution failed' },

  // ── Executions view ──
  'executions.status.pending': { 'zh-CN': '等待中', en: 'Pending' },
  'executions.status.running': { 'zh-CN': '运行中', en: 'Running' },
  'executions.status.completed': { 'zh-CN': '已完成', en: 'Completed' },
  'executions.status.failed': { 'zh-CN': '失败', en: 'Failed' },
  'executions.status.cancelled': { 'zh-CN': '已取消', en: 'Cancelled' },

  // ── JMX Editor view ──
  'jmxEditor.save': { 'zh-CN': '保存', en: 'Save' },
  'jmxEditor.run': { 'zh-CN': '运行', en: 'Run' },
  'jmxEditor.addComponent': { 'zh-CN': '添加组件', en: 'Add Component' },
  'jmxEditor.copy': { 'zh-CN': '复制', en: 'Copy' },
  'jmxEditor.delete': { 'zh-CN': '删除', en: 'Delete' },
  'jmxEditor.disable': { 'zh-CN': '禁用', en: 'Disable' },
  'jmxEditor.enable': { 'zh-CN': '启用', en: 'Enable' },
  'jmxEditor.functionHelper': { 'zh-CN': '函数助手', en: 'Function Helper' },
  'jmxEditor.viewJmx': { 'zh-CN': '查看 JMX', en: 'View JMX' },
  'jmxEditor.componentTree': { 'zh-CN': '组件树', en: 'Component Tree' },
  'jmxEditor.disabledTag': { 'zh-CN': '禁用', en: 'Disabled' },
  'jmxEditor.properties': { 'zh-CN': '属性', en: 'Properties' },
  'jmxEditor.name': { 'zh-CN': '名称', en: 'Name' },
  'jmxEditor.bodyData': { 'zh-CN': '请求体 (Body Data)', en: 'Body Data' },
  'jmxEditor.rawProperties': { 'zh-CN': '原始属性 (JSON)', en: 'Raw Properties (JSON)' },
  'jmxEditor.selectComponent': { 'zh-CN': '请选择一个组件', en: 'Please select a component' },
  'jmxEditor.addDialog': { 'zh-CN': '添加组件', en: 'Add Component' },
  'jmxEditor.selectType': { 'zh-CN': '选择组件类型', en: 'Select Component Type' },
  'jmxEditor.componentName': { 'zh-CN': '组件名称', en: 'Component Name' },
  'jmxEditor.namePlaceholder': { 'zh-CN': '请输入名称', en: 'Enter name' },
  'jmxEditor.cancel': { 'zh-CN': '取消', en: 'Cancel' },
  'jmxEditor.confirm': { 'zh-CN': '确定', en: 'Confirm' },
  'jmxEditor.jmxSourceDialog': { 'zh-CN': 'JMX 源码', en: 'JMX Source' },
  'jmxEditor.type.testPlan': { 'zh-CN': '测试计划', en: 'Test Plan' },
  'jmxEditor.type.thread': { 'zh-CN': '线程', en: 'Thread' },
  'jmxEditor.type.sampler': { 'zh-CN': '取样器', en: 'Sampler' },
  'jmxEditor.type.logicController': { 'zh-CN': '逻辑控制器', en: 'Logic Controller' },
  'jmxEditor.type.config': { 'zh-CN': '配置元件', en: 'Config Element' },
  'jmxEditor.type.assertion': { 'zh-CN': '断言', en: 'Assertion' },
  'jmxEditor.type.extractor': { 'zh-CN': '提取器', en: 'Extractor' },
  'jmxEditor.type.preProcessor': { 'zh-CN': '前置处理器', en: 'Pre Processor' },
  'jmxEditor.type.postProcessor': { 'zh-CN': '后置处理器', en: 'Post Processor' },
  'jmxEditor.type.timer': { 'zh-CN': '定时器', en: 'Timer' },
  'jmxEditor.type.listener': { 'zh-CN': '监听器', en: 'Listener' },
  'jmxEditor.type.other': { 'zh-CN': '其他', en: 'Other' },
  'jmxEditor.copySuffix': { 'zh-CN': '(副本)', en: '(Copy)' },
  'jmxEditor.copied': { 'zh-CN': '已复制组件', en: 'Component copied' },
  'jmxEditor.confirmDelete': { 'zh-CN': '确定删除该组件吗？', en: 'Are you sure you want to delete this component?' },
  'jmxEditor.deletePrompt': { 'zh-CN': '提示', en: 'Prompt' },
  'jmxEditor.deleted': { 'zh-CN': '已删除', en: 'Deleted' },
  'jmxEditor.functionCopied': { 'zh-CN': '已复制函数: {{ func }}', en: 'Copied function: {{ func }}' },
  'jmxEditor.propertiesUpdated': { 'zh-CN': '属性已更新', en: 'Properties updated' },
  'jmxEditor.jsonError': { 'zh-CN': 'JSON 格式错误', en: 'JSON format error' },
  'jmxEditor.saveSuccess': { 'zh-CN': '保存成功', en: 'Saved successfully' },
  'jmxEditor.saveFailed': { 'zh-CN': '保存失败: {{ msg }}', en: 'Save failed: {{ msg }}' },
  'jmxEditor.runStarted': { 'zh-CN': '测试已启动', en: 'Test started' },
  'jmxEditor.runFailed': { 'zh-CN': '启动失败: {{ msg }}', en: 'Start failed: {{ msg }}' },
  'jmxEditor.loadFailed': { 'zh-CN': '加载 JMX 失败: {{ msg }}', en: 'Failed to load JMX: {{ msg }}' },
  'jmxEditor.loadError': { 'zh-CN': '加载失败: {{ msg }}', en: 'Load failed: {{ msg }}' },

  // ── Layout view ──
  'layout.nav.executions': { 'zh-CN': '执行记录', en: 'Runs' },
  'layout.nav.mcp': { 'zh-CN': '外部助手接入（MCP）', en: 'Assistants (MCP)' },
  'layout.nav.aiChat': { 'zh-CN': 'AI工作流', en: 'AI Workflow' },
  'layout.nav.settings': { 'zh-CN': '设置', en: 'Settings' },
  'layout.ai.connected': { 'zh-CN': 'AI服务正常', en: 'AI service connected' },
  'layout.ai.disconnected': { 'zh-CN': 'AI服务未连接', en: 'AI service disconnected' },
  'layout.expand': { 'zh-CN': '展开侧边栏', en: 'Expand sidebar' },
  'layout.collapse': { 'zh-CN': '收起侧边栏', en: 'Collapse sidebar' },
  'layout.resources': { 'zh-CN': 'CPU / 内存', en: 'CPU / Memory' },

  // ── MCP Connect view ──

  // ── OpenAPI Import view ──

  // ── Project Settings view ──
  'project.description': { 'zh-CN': '项目描述', en: 'Project Description' },
  'project.copySuffix': { 'zh-CN': '(副本)', en: '(Copy)' },
  'project.message.copyFailed': { 'zh-CN': '复制失败', en: 'Copy failed' },

  // ── Report view ──
  'report.timeSeriesTable.timeRange': { 'zh-CN': '时间段', en: 'Time Range' },
  'report.timeSeriesTable.requests': { 'zh-CN': '请求数', en: 'Requests' },
  'report.errorType': { 'zh-CN': '错误类型', en: 'Error Type' },
  'report.errorCount': { 'zh-CN': '次数', en: 'Count' },
  'report.errorRatio': { 'zh-CN': '占比', en: 'Ratio' },
  'report.errorExample': { 'zh-CN': '示例', en: 'Example' },
  'report.evidenceWarning': { 'zh-CN': 'AI文本尚未逐项事实核验，请结合原始指标；推测不等于已验证根因。', en: 'AI text has not been fact-checked claim by claim. Review the measured metrics; hypotheses are not verified root causes.' },
  'report.evaluation': { 'zh-CN': '整体评估', en: 'Overall Evaluation' },
  'report.keyFindings': { 'zh-CN': '关键发现', en: 'Key Findings' },
  'report.issueLocation': { 'zh-CN': '具体问题定位', en: 'Issue Location' },
  'report.timelineAnalysis': { 'zh-CN': '性能时间线分析', en: 'Performance Timeline' },
  'report.resourceBottleneck': { 'zh-CN': '资源瓶颈推断', en: 'Resource Bottleneck' },
  'report.errorRootCause': { 'zh-CN': '错误根因分析', en: 'Error Root Cause' },
  'report.bottleneck': { 'zh-CN': '性能瓶颈点', en: 'Performance Bottleneck' },
  'report.optimization': { 'zh-CN': '优化建议', en: 'Optimization Suggestions' },
  'report.improvementPlan': { 'zh-CN': '分步改进计划', en: 'Step-by-step Improvement Plan' },
  'report.riskAreas': { 'zh-CN': '风险区域', en: 'Risk Areas' },
  'report.conclusion': { 'zh-CN': '最终结论', en: 'Conclusion' },
  'report.aiNotEnabled': { 'zh-CN': '未启用AI分析或分析未完成', en: 'AI analysis not enabled or not completed' },

  // ── Analysis Reports view ──
  'report.downloadJtl': { 'zh-CN': '下载 JTL 结果文件', en: 'Download JTL Results' },
  'report.viewHtml': { 'zh-CN': '查看 HTML 报告', en: 'View HTML Report' },
  'report.notFound': { 'zh-CN': '报告不存在或测试未完成', en: 'Report does not exist or test is not complete' },

  // ── Settings view (remaining strings) ──
  'settings.table.id': { 'zh-CN': '标识', en: 'ID' },
  'settings.table.contextWindow': { 'zh-CN': '上下文窗口', en: 'Context' },
  'settings.table.status': { 'zh-CN': '状态', en: 'Status' },
  'settings.table.enabled': { 'zh-CN': '启用', en: 'Enabled' },
  'settings.table.disabled': { 'zh-CN': '禁用', en: 'Disabled' },
  'settings.table.test': { 'zh-CN': '测试', en: 'Test' },
  'settings.table.edit': { 'zh-CN': '编辑', en: 'Edit' },
  'settings.table.delete': { 'zh-CN': '删除', en: 'Delete' },
  'settings.editPaths': { 'zh-CN': '编辑路径', en: 'Edit Paths' },
  'settings.add': { 'zh-CN': '添加', en: 'Add' },
  'settings.dialog.addTitle': { 'zh-CN': '添加模型配置', en: 'Add Model Config' },
  'settings.dialog.editTitle': { 'zh-CN': '编辑模型配置', en: 'Edit Model Config' },
  'settings.table.actions': { 'zh-CN': '操作', en: 'Actions' },
  'settings.form.model': { 'zh-CN': '模型', en: 'Model' },
  'settings.form.apiKey': { 'zh-CN': 'API Key', en: 'API Key' },
  'settings.form.contextWindow': { 'zh-CN': '上下文窗口', en: 'Context window' },
  'settings.form.contextWindowAuto': { 'zh-CN': '留空自动检测', en: 'Leave empty to detect' },
  'settings.form.contextWindowTip': { 'zh-CN': '单位 tokens。留空时保存会向服务商查询真实大小，查不到再用官方公布值；也可以手动填写。', en: 'In tokens. Leave empty and saving asks the provider for the real size, falling back to the documented size; or enter it yourself.' },
  'settings.form.contextWindowUnknown': { 'zh-CN': '没检测到这个模型的上下文大小，按 16K 保守值使用；编辑模型可以手动填写。', en: 'Size not detected, so a conservative 16K is used; edit the model to set it.' },
  'settings.form.apiKeyPlaceholder': { 'zh-CN': '输入 API Key', en: 'Enter API Key' },
  'settings.form.apiBase': { 'zh-CN': 'API 地址', en: 'API Base URL' },
  'settings.form.enabled': { 'zh-CN': '启用', en: 'Enabled' },
  'settings.form.provider': { 'zh-CN': '服务商', en: 'Provider' },
  'settings.form.providerCustom': { 'zh-CN': '其他', en: 'Other' },
  'settings.form.apiBaseCustomTip': { 'zh-CN': '任意 OpenAI 兼容接口地址', en: 'Any OpenAI-compatible endpoint' },
  'settings.form.modelSelectPlaceholder': { 'zh-CN': '选择或直接输入模型名', en: 'Pick or type a model name' },
  'settings.form.apiKeyKeep': { 'zh-CN': '留空则保持原 Key 不变', en: 'Leave blank to keep the current key' },
  'settings.rule.apiBaseRequired': { 'zh-CN': '请输入 API 地址', en: 'Please enter the API base URL' },
  'settings.rule.modelIdRequired2': { 'zh-CN': '请输入模型ID', en: 'Please enter model ID' },
  'settings.rule.apiKeyRequired': { 'zh-CN': '请输入 API Key', en: 'Please enter API Key' },
  'settings.message.loadFailed': { 'zh-CN': '加载数据失败', en: 'Failed to load data' },
  'settings.message.fillPaths': { 'zh-CN': '请填写完整的路径信息', en: 'Please fill in all path information' },
  'settings.message.pathsUpdated': { 'zh-CN': '路径配置已更新', en: 'Paths updated' },
  'settings.message.saveFailed': { 'zh-CN': '保存失败', en: 'Save failed' },
  'settings.message.updateSuccess': { 'zh-CN': '更新成功', en: 'Updated successfully' },
  'settings.message.addSuccess': { 'zh-CN': '添加成功', en: 'Added successfully' },
  'settings.message.updateFailed': { 'zh-CN': '更新失败', en: 'Update failed' },
  'settings.message.addFailed': { 'zh-CN': '添加失败', en: 'Add failed' },
  'settings.message.testSuccess': { 'zh-CN': '连接成功', en: 'Connection successful' },
  'settings.message.testFailed': { 'zh-CN': '连接失败', en: 'Connection failed' },
  'settings.confirmDeleteModel': { 'zh-CN': '确定要删除模型 "{{ name }}" 吗？', en: 'Are you sure you want to delete model "{{ name }}"?' },
  'settings.confirmDelete': { 'zh-CN': '确认删除', en: 'Confirm Delete' },
  'settings.message.deleteSuccess': { 'zh-CN': '删除成功', en: 'Deleted successfully' },
  'settings.message.deleteFailed': { 'zh-CN': '删除失败', en: 'Delete failed' },

  // ── Test Case Editor view ──

  // ── Test Plans view ──
  'testPlans.confirmDeleteProject': { 'zh-CN': '确定删除项目"{{ name }}"吗？该操作将同时删除项目下所有测试计划。', en: 'Delete project "{{ name }}"? This will also delete all test plans under it.' },
  'testPlans.message.projectDeleted': { 'zh-CN': '项目已删除', en: 'Project deleted' },

  // ── Project Settings view ──
  'projectSettings.cancel': { 'zh-CN': '取消', en: 'Cancel' },
  'projectSettings.save': { 'zh-CN': '保存', en: 'Save' },
  'projectSettings.create': { 'zh-CN': '创建', en: 'Create' },
  'projectSettings.delete': { 'zh-CN': '删除', en: 'Delete' },
  'projectSettings.confirmDelete': { 'zh-CN': '确认删除', en: 'Confirm Delete' },
  'projectSettings.tab.basic': { 'zh-CN': '基本信息', en: 'Basic Info' },
  'projectSettings.tab.environments': { 'zh-CN': '环境管理', en: 'Environment Management' },
  'projectSettings.form.name': { 'zh-CN': '项目名称', en: 'Project Name' },
  'projectSettings.form.namePlaceholder': { 'zh-CN': '项目名称', en: 'Project Name' },
  'projectSettings.form.description': { 'zh-CN': '项目描述', en: 'Project Description' },
  'projectSettings.form.descriptionPlaceholder': { 'zh-CN': '项目描述', en: 'Project Description' },
  'projectSettings.form.save': { 'zh-CN': '保存项目信息', en: 'Save Project Info' },
  'projectSettings.env.count': { 'zh-CN': '{{ count }} 个环境', en: '{{ count }} environments' },
  'projectSettings.env.add': { 'zh-CN': '添加环境', en: 'Add Environment' },
  'projectSettings.env.empty': { 'zh-CN': '暂无环境配置', en: 'No environment configuration' },
  'projectSettings.env.createFirst': { 'zh-CN': '创建第一个环境', en: 'Create First Environment' },
  'projectSettings.env.active': { 'zh-CN': '当前使用', en: 'Active' },
  'projectSettings.env.setActive': { 'zh-CN': '设为当前', en: 'Set as Active' },
  'projectSettings.env.variables': { 'zh-CN': '环境变量', en: 'Environment Variables' },
  'projectSettings.env.varName': { 'zh-CN': '变量名', en: 'Variable Name' },
  'projectSettings.env.varNamePlaceholder': { 'zh-CN': '变量名', en: 'Variable Name' },
  'projectSettings.env.varValue': { 'zh-CN': '变量值', en: 'Variable Value' },
  'projectSettings.env.varValuePlaceholder': { 'zh-CN': '变量值', en: 'Variable Value' },
  'projectSettings.env.addVar': { 'zh-CN': '添加变量', en: 'Add Variable' },
  'projectSettings.env.headers': { 'zh-CN': '全局请求头', en: 'Global Headers' },
  'projectSettings.env.headerName': { 'zh-CN': 'Header 名', en: 'Header Name' },
  'projectSettings.env.headerNamePlaceholder': { 'zh-CN': 'Header 名', en: 'Header Name' },
  'projectSettings.env.headerValue': { 'zh-CN': 'Header 值', en: 'Header Value' },
  'projectSettings.env.headerValuePlaceholder': { 'zh-CN': 'Header 值', en: 'Header Value' },
  'projectSettings.env.addHeader': { 'zh-CN': '添加请求头', en: 'Add Header' },
  'projectSettings.env.edit': { 'zh-CN': '编辑环境', en: 'Edit Environment' },
  'projectSettings.env.name': { 'zh-CN': '环境名称', en: 'Environment Name' },
  'projectSettings.env.namePlaceholder': { 'zh-CN': '如: 开发环境、测试环境、生产环境', en: 'e.g. Development, Testing, Production' },
  'projectSettings.env.nameRequired': { 'zh-CN': '请输入环境名称', en: 'Please enter environment name' },
  'projectSettings.env.copySuffix': { 'zh-CN': ' (副本)', en: ' (Copy)' },
  'projectSettings.env.atLeastOne': { 'zh-CN': '至少保留一个环境', en: 'Keep at least one environment' },
  'projectSettings.env.confirmDelete': { 'zh-CN': '确定删除环境"{{ name }}"吗？', en: 'Delete environment "{{ name }}"?' },
  'projectSettings.message.loadFailed': { 'zh-CN': '加载项目信息失败', en: 'Failed to load project info' },
  'projectSettings.message.saveSuccess': { 'zh-CN': '项目信息已保存', en: 'Project info saved' },
  'projectSettings.message.saveFailed': { 'zh-CN': '保存失败', en: 'Save failed' },
  'projectSettings.message.envUpdated': { 'zh-CN': '环境已更新', en: 'Environment updated' },
  'projectSettings.message.envCreated': { 'zh-CN': '环境已创建', en: 'Environment created' },
  'projectSettings.message.operationFailed': { 'zh-CN': '操作失败', en: 'Operation failed' },
  'projectSettings.message.envSwitched': { 'zh-CN': '已切换当前环境', en: 'Environment switched' },
  'projectSettings.message.switchFailed': { 'zh-CN': '切换失败', en: 'Switch failed' },
  'projectSettings.message.envCopied': { 'zh-CN': '环境已复制', en: 'Environment copied' },
  'projectSettings.message.copyFailed': { 'zh-CN': '复制失败', en: 'Copy failed' },
  'projectSettings.message.envDeleted': { 'zh-CN': '环境已删除', en: 'Environment deleted' },
  'projectSettings.message.deleteFailed': { 'zh-CN': '删除失败', en: 'Delete failed' },

  'mcpConnect.urlLabel': { 'zh-CN': 'MCP 端点', en: 'MCP Endpoint' },


  'createPlan.template.baseline.name': { 'zh-CN': '基准测试', en: 'Baseline Test' },
  'createPlan.template.baseline.desc': { 'zh-CN': '低并发长时间运行，获取系统基础性能指标', en: 'Low concurrency long-term run to get basic system performance metrics' },
  'createPlan.template.load.name': { 'zh-CN': '负载测试', en: 'Load Test' },
  'createPlan.template.load.desc': { 'zh-CN': '模拟正常业务负载，验证系统承载能力', en: 'Simulate normal business load to verify system capacity' },
  'createPlan.template.stress.name': { 'zh-CN': '压力测试', en: 'Stress Test' },
  'createPlan.template.stress.desc': { 'zh-CN': '超过正常负载的并发，测试系统极限', en: 'Exceed normal load concurrency to test system limits' },
  'createPlan.template.concurrency.name': { 'zh-CN': '高并发测试', en: 'Concurrency Test' },
  'createPlan.template.concurrency.desc': { 'zh-CN': '高并发用户数，测试系统最大承载能力', en: 'High concurrency users to test system maximum capacity' },
  'createPlan.template.ramp_up.name': { 'zh-CN': '阶梯加压测试', en: 'Ramp-Up Test' },
  'createPlan.template.ramp_up.desc': { 'zh-CN': '逐步增加负载，找到系统性能拐点', en: 'Gradually increase load to find system performance inflection point' },
  'createPlan.template.stability.name': { 'zh-CN': '稳定性测试', en: 'Stability Test' },
  'createPlan.template.stability.desc': { 'zh-CN': '中等并发长时间运行，验证系统稳定性', en: 'Medium concurrency long-term run to verify system stability' },
  'createPlan.template.spike.name': { 'zh-CN': '尖峰测试', en: 'Spike Test' },
  'createPlan.template.spike.desc': { 'zh-CN': '短时间内大量用户涌入，测试突发流量应对能力', en: 'Short-term massive user influx to test burst traffic handling' },
  'createPlan.template.custom.name': { 'zh-CN': '自定义测试', en: 'Custom Test' },
  'createPlan.template.custom.desc': { 'zh-CN': '完全自定义并发、Ramp-up、持续时间等所有测试参数', en: 'Fully customize concurrency, ramp-up, duration and all test parameters' },

  'editor.back': { 'zh-CN': '返回', en: 'Back' },
  'editor.save': { 'zh-CN': '保存', en: 'Save' },
  'editor.run': { 'zh-CN': '运行', en: 'Run' },
  'editor.viewJmx': { 'zh-CN': '查看 JMX', en: 'View JMX' },
  'editor.functionHelper': { 'zh-CN': '函数助手', en: 'Function Helper' },
  'editor.selectEndpoint': { 'zh-CN': '选择一个接口开始编辑', en: 'Select an endpoint to start editing' },
  'editor.send': { 'zh-CN': '发送', en: 'Send' },
  'editor.endpointName': { 'zh-CN': '接口名称', en: 'Endpoint Name' },
  'editor.inDevelopment': { 'zh-CN': '开发中', en: 'In Development' },
  'editor.endpointDescription': { 'zh-CN': '接口说明（支持 Markdown）', en: 'Endpoint Description (Markdown supported)' },
  'editor.requestDefinition': { 'zh-CN': '请求定义', en: 'Request Definition' },
  'editor.bodyType': { 'zh-CN': 'Body 类型', en: 'Body Type' },
  'editor.bodyStructure': { 'zh-CN': 'Body 结构', en: 'Body Structure' },
  'editor.paramName': { 'zh-CN': '参数名', en: 'Param Name' },
  'editor.paramValue': { 'zh-CN': '参数值', en: 'Param Value' },
  'editor.paramDescription': { 'zh-CN': '描述', en: 'Description' },
  'editor.addParam': { 'zh-CN': '添加参数', en: 'Add Param' },
  'editor.authType': { 'zh-CN': '认证类型', en: 'Auth Type' },
  'editor.noAuth': { 'zh-CN': '无认证', en: 'No Auth' },
  'editor.username': { 'zh-CN': '用户名', en: 'Username' },
  'editor.password': { 'zh-CN': '密码', en: 'Password' },
  'editor.preActions': { 'zh-CN': '前置操作', en: 'Pre-Actions' },
  'editor.postActions': { 'zh-CN': '后置操作', en: 'Post-Actions' },
  'editor.assertions': { 'zh-CN': '断言', en: 'Assertions' },
  'editor.protocol': { 'zh-CN': '协议', en: 'Protocol' },
  'editor.server': { 'zh-CN': '服务器/域名', en: 'Server/Domain' },
  'editor.port': { 'zh-CN': '端口', en: 'Port' },
  'editor.connectionTimeout': { 'zh-CN': '连接超时(ms)', en: 'Connection Timeout(ms)' },
  'editor.contentEncoding': { 'zh-CN': '内容编码', en: 'Content Encoding' },
  'editor.followRedirects': { 'zh-CN': '跟随重定向', en: 'Follow Redirects' },
  'editor.autoRedirect': { 'zh-CN': '自动重定向', en: 'Auto Redirect' },
  'editor.jmxSource': { 'zh-CN': 'JMX 源码', en: 'JMX Source' },
  'editor.view.edit': { 'zh-CN': '编辑', en: 'Edit' },
  'editor.view.debug': { 'zh-CN': '调试', en: 'Debug' },
  'editor.view.testcase': { 'zh-CN': '测试用例', en: 'Test Cases' },
  'editor.view.settings': { 'zh-CN': '设置', en: 'Settings' },
  'editor.defaultPlanName': { 'zh-CN': '测试计划', en: 'Test Plan' },
  'editor.rawBodyHint': { 'zh-CN': '请求体内容(支持任意格式)...', en: 'Request body content (any format)...' },
  'editor.bodyHint': { 'zh-CN': '请求体内容...', en: 'Request body content...' },
  'editor.newEndpoint': { 'zh-CN': '新接口', en: 'New Endpoint' },
  'editor.copySuffix': { 'zh-CN': ' (副本)', en: ' (Copy)' },
  'editor.message.endpointCopied': { 'zh-CN': '接口已复制', en: 'Endpoint copied' },
  'editor.message.functionCopied': { 'zh-CN': '已复制函数: {{ func }}', en: 'Function copied: {{ func }}' },
  'editor.message.singleEndpointNotSupported': { 'zh-CN': '单接口发送功能开发中，请使用「运行」执行完整测试计划', en: 'Single endpoint sending is under development. Use "Run" to execute the complete test plan.' },
  'editor.message.saveSuccess': { 'zh-CN': '保存成功', en: 'Saved successfully' },
  'editor.message.saveFailed': { 'zh-CN': '保存失败: {{ msg }}', en: 'Save failed: {{ msg }}' },
  'editor.message.runStarted': { 'zh-CN': '测试已启动', en: 'Test started' },
  'editor.message.runFailed': { 'zh-CN': '启动失败: {{ msg }}', en: 'Start failed: {{ msg }}' },
  'editor.message.loadJmxFailed': { 'zh-CN': '加载 JMX 失败: {{ msg }}', en: 'Failed to load JMX: {{ msg }}' },
  'editor.message.loadPlanFailed': { 'zh-CN': '加载测试计划失败: {{ msg }}', en: 'Failed to load test plan: {{ msg }}' },
  'editor.endpointList.title': { 'zh-CN': '接口列表', en: 'Endpoint List' },
  'editor.endpointList.unnamed': { 'zh-CN': '未命名接口', en: 'Unnamed Endpoint' },
  'editor.endpointList.duplicate': { 'zh-CN': '复制接口', en: 'Duplicate Endpoint' },
  'editor.endpointList.delete': { 'zh-CN': '删除接口', en: 'Delete Endpoint' },
  'editor.endpointList.empty': { 'zh-CN': '暂无接口', en: 'No endpoints' },
  'editor.endpointList.add': { 'zh-CN': '添加接口', en: 'Add Endpoint' },
  'editor.responsePanel.clickSend': { 'zh-CN': '点击「发送」查看响应结果', en: 'Click "Send" to view response' },
  'editor.responsePanel.console': { 'zh-CN': '控制台', en: 'Console' },
  'editor.responsePanel.noCookie': { 'zh-CN': '暂无 Cookie 数据', en: 'No Cookie data' },
  'editor.responsePanel.noConsole': { 'zh-CN': '暂无控制台数据', en: 'No console data' },
  'editor.preProcessor.title': { 'zh-CN': '前置脚本', en: 'Pre-Script' },
  'editor.preProcessor.desc': { 'zh-CN': '在发送请求之前执行的脚本，可用于设置变量、生成签名等', en: 'Script executed before sending the request, can be used for setting variables, generating signatures, etc.' },
  'editor.preProcessor.placeholder': { 'zh-CN': '// 前置脚本 — 在请求发送前执行\n// 可用对象: vars, props, log, ctx\n// 示例:\n// vars.put("timestamp", String.valueOf(System.currentTimeMillis()))', en: '// Pre-Script — executed before request\n// Available objects: vars, props, log, ctx\n// Example:\n// vars.put("timestamp", String.valueOf(System.currentTimeMillis()))' },
  'editor.postProcessor.title': { 'zh-CN': '后置脚本', en: 'Post-Script' },
  'editor.postProcessor.desc': { 'zh-CN': '在收到响应之后执行的脚本，可用于提取数据、断言等', en: 'Script executed after receiving the response, can be used for data extraction, assertions, etc.' },
  'editor.postProcessor.placeholder': { 'zh-CN': '// 后置脚本 — 在请求响应后执行\n// 可用对象: vars, props, log, ctx, prev\n// prev.getResponseCode() — 响应状态码\n// prev.getResponseDataAsString() — 响应体字符串', en: '// Post-Script — executed after response\n// Available objects: vars, props, log, ctx, prev\n// prev.getResponseCode() — response status code\n// prev.getResponseDataAsString() — response body string' },
  'editor.scriptLanguage': { 'zh-CN': '脚本语言', en: 'Script Language' },
  'editor.scriptContent': { 'zh-CN': '脚本内容', en: 'Script Content' },
  'editor.quickInsert': { 'zh-CN': '快速插入', en: 'Quick Insert' },
  'editor.snippet.setVar': { 'zh-CN': '设置变量', en: 'Set Variable' },
  'editor.snippet.getVar': { 'zh-CN': '获取变量', en: 'Get Variable' },
  'editor.snippet.timestamp': { 'zh-CN': '时间戳', en: 'Timestamp' },
  'editor.snippet.random': { 'zh-CN': '随机数', en: 'Random' },
  'editor.snippet.getResponse': { 'zh-CN': '获取响应', en: 'Get Response' },
  'editor.snippet.assert': { 'zh-CN': '断言', en: 'Assert' },
  'editor.snippet.log': { 'zh-CN': '打印日志', en: 'Log' },
  'editor.extractor.title': { 'zh-CN': '数据提取器', en: 'Data Extractors' },
  'editor.extractor.jsonPath': { 'zh-CN': 'JSONPath 提取器', en: 'JSONPath Extractor' },
  'editor.extractor.regex': { 'zh-CN': '正则表达式提取器', en: 'Regex Extractor' },
  'editor.extractor.boundary': { 'zh-CN': '边界提取器', en: 'Boundary Extractor' },
  'editor.extractor.varName': { 'zh-CN': '变量名', en: 'Variable Name' },
  'editor.extractor.jsonPathExpr': { 'zh-CN': 'JSONPath 表达式', en: 'JSONPath Expression' },
  'editor.extractor.regexExpr': { 'zh-CN': '正则表达式', en: 'Regex Expression' },
  'editor.extractor.template': { 'zh-CN': '模板', en: 'Template' },
  'editor.extractor.matchNo': { 'zh-CN': '匹配编号', en: 'Match Number' },
  'editor.extractor.defaultValue': { 'zh-CN': '默认值', en: 'Default Value' },
  'editor.extractor.leftBoundary': { 'zh-CN': '左边界', en: 'Left Boundary' },
  'editor.extractor.rightBoundary': { 'zh-CN': '右边界', en: 'Right Boundary' },
  'editor.extractor.addJson': { 'zh-CN': '添加 JSONPath 提取器', en: 'Add JSONPath Extractor' },
  'editor.extractor.addRegex': { 'zh-CN': '添加正则提取器', en: 'Add Regex Extractor' },
  'editor.extractor.addBoundary': { 'zh-CN': '添加边界提取器', en: 'Add Boundary Extractor' },
  'editor.message.jsonPathRequired': { 'zh-CN': '请填写变量名和JSONPath表达式', en: 'Please fill in variable name and JSONPath expression' },
  'editor.message.regexRequired': { 'zh-CN': '请填写变量名和正则表达式', en: 'Please fill in variable name and regex expression' },
  'editor.message.boundaryRequired': { 'zh-CN': '请填写变量名和边界', en: 'Please fill in variable name and boundaries' },
  'editor.assertion.title': { 'zh-CN': '断言规则', en: 'Assertion Rules' },
  'editor.assertion.desc': { 'zh-CN': '定义响应断言，验证接口返回是否符合预期', en: 'Define response assertions to verify if API responses meet expectations' },
  'editor.assertion.empty': { 'zh-CN': '暂无断言规则', en: 'No assertion rules' },
  'editor.assertion.add': { 'zh-CN': '添加断言', en: 'Add Assertion' },
  'editor.assertion.index': { 'zh-CN': '断言 #{{ idx }}', en: 'Assertion #{{ idx }}' },
  'editor.assertion.type': { 'zh-CN': '断言类型', en: 'Assertion Type' },
  'editor.assertion.type.statusCode': { 'zh-CN': '状态码', en: 'Status Code' },
  'editor.assertion.type.responseTime': { 'zh-CN': '响应时间', en: 'Response Time' },
  'editor.assertion.type.bodyContains': { 'zh-CN': '响应体包含文本', en: 'Body Contains' },
  'editor.assertion.type.bodyRegex': { 'zh-CN': '响应体匹配正则', en: 'Body Regex' },
  'editor.assertion.type.header': { 'zh-CN': '响应头', en: 'Header' },
  'editor.assertion.operator': { 'zh-CN': '比较方式', en: 'Comparison' },
  'editor.assertion.op.lt': { 'zh-CN': '小于', en: 'Less Than' },
  'editor.assertion.op.lte': { 'zh-CN': '小于等于', en: 'Less Than Or Equal' },
  'editor.assertion.op.gt': { 'zh-CN': '大于', en: 'Greater Than' },
  'editor.assertion.op.gte': { 'zh-CN': '大于等于', en: 'Greater Than Or Equal' },
  'editor.assertion.op.eq': { 'zh-CN': '等于', en: 'Equals' },
  'editor.assertion.op.neq': { 'zh-CN': '不等于', en: 'Not Equals' },
  'editor.assertion.op.contains': { 'zh-CN': '包含', en: 'Contains' },
  'editor.assertion.op.notContains': { 'zh-CN': '不包含', en: 'Not Contains' },
  'editor.assertion.op.regex': { 'zh-CN': '匹配正则', en: 'Match Regex' },
  'editor.assertion.expected': { 'zh-CN': '预期值', en: 'Expected Value' },
  'editor.assertion.jsonPathExpr': { 'zh-CN': 'JSONPath 表达式', en: 'JSONPath Expression' },
  'editor.assertion.headerName': { 'zh-CN': '响应头名称', en: 'Header Name' },
  'editor.assertion.type.duration': { 'zh-CN': '持续时间', en: 'Duration' },
  'editor.assertion.type.size': { 'zh-CN': '响应大小', en: 'Response Size' },
  'editor.assertion.type.jsr223': { 'zh-CN': 'JSR223断言', en: 'JSR223 Assertion' },
  'editor.assertion.op.custom': { 'zh-CN': '自定义脚本', en: 'Custom Script' },
  'editor.assertion.maxDuration': { 'zh-CN': '最大时长(ms)', en: 'Max Duration (ms)' },
  'editor.assertion.sizeBytes': { 'zh-CN': '大小(字节)', en: 'Size (bytes)' },
  'editor.assertion.script': { 'zh-CN': '脚本内容', en: 'Script Content' },
  'editor.assertion.jsr223Placeholder': { 'zh-CN': '// 编写断言脚本\n// 使用 AssertionResult.setFailure(true) 设置断言失败', en: '// Write assertion script\n// Use AssertionResult.setFailure(true) to set assertion failure' },
  'editor.assertion.parameters': { 'zh-CN': '参数', en: 'Parameters' },
  'editor.assertion.filename': { 'zh-CN': '脚本文件', en: 'Script File' },

  'editor.extractor.xpath': { 'zh-CN': 'XPath提取器', en: 'XPath Extractor' },
  'editor.extractor.xpathQuery': { 'zh-CN': 'XPath表达式', en: 'XPath Expression' },
  'editor.extractor.namespaces': { 'zh-CN': '命名空间', en: 'Namespaces' },
  'editor.extractor.addXpath': { 'zh-CN': '添加XPath提取器', en: 'Add XPath Extractor' },

  'editor.message.xpathRequired': { 'zh-CN': '请填写变量名和XPath表达式', en: 'Please fill in variable name and XPath expression' },

  'editor.threadGroup': { 'zh-CN': '线程组配置', en: 'Thread Group' },
  'editor.tg.numThreads': { 'zh-CN': '线程数', en: 'Number of Threads' },
  'editor.tg.rampTime': { 'zh-CN': 'Ramp-Up时间', en: 'Ramp-Up Period' },
  'editor.tg.loops': { 'zh-CN': '循环次数', en: 'Number of Loops' },
  'editor.tg.infinite': { 'zh-CN': '(-1=永远)', en: '(-1=Forever)' },
  'editor.tg.onSampleError': { 'zh-CN': '取样器错误后', en: 'On Sample Error' },
  'editor.tg.errorContinue': { 'zh-CN': '继续', en: 'Continue' },
  'editor.tg.errorStartNextLoop': { 'zh-CN': '启动下一循环', en: 'Start Next Loop' },
  'editor.tg.errorStopThread': { 'zh-CN': '停止线程', en: 'Stop Thread' },
  'editor.tg.errorStopTest': { 'zh-CN': '停止测试', en: 'Stop Test' },
  'editor.tg.errorStopTestNow': { 'zh-CN': '立即停止测试', en: 'Stop Test Now' },
  'editor.tg.scheduler': { 'zh-CN': '调度器', en: 'Scheduler' },
  'editor.tg.duration': { 'zh-CN': '持续时间', en: 'Duration' },
  'editor.tg.delay': { 'zh-CN': '启动延迟', en: 'Startup Delay' },

}

export function useI18n() {
  const appStore = useAppStore()

  const currentLang = computed(() => appStore.currentLang)

  function t(
    key: string,
    params?: Record<string, string | number> | string,
    fallback?: string
  ): string {
    let values: Record<string, string | number> | undefined
    let fb: string | undefined

    if (typeof params === 'string') {
      fb = params
    } else {
      values = params
      fb = fallback
    }

    const entry = messages[key]
    let text = entry?.[appStore.currentLang as SupportedLang] ?? entry?.['zh-CN'] ?? fb ?? key

    if (values) {
      text = text.replace(/\{\{\s*(\w+)\s*\}\}/g, (match: string, name: string) =>
        String(values![name] ?? match)
      )
    }

    return text
  }

  function setLang(lang: SupportedLang) {
    appStore.setLang(lang)
  }

  return { t, currentLang, setLang }
}
