from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from enum import Enum


class TestType(str, Enum):
    BASELINE = "baseline"           # 基准测试
    CONCURRENCY = "concurrency"     # 高并发测试
    RAMP_UP = "ramp_up"             # 阶梯加压测试
    STRESS = "stress"               # 压力测试
    STABILITY = "stability"         # 稳定性测试
    SPIKE = "spike"                 # 尖峰测试
    CUSTOM = "custom"               # 自定义测试


class AIModelType(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    QWEN = "qwen"
    DEEPSEEK = "deepseek"


class AIModelConfig(BaseModel):
    """AI 模型配置"""
    id: str = Field(..., description="模型唯一标识，如 openai, deepseek, my-model")
    name: str = Field(..., description="显示名称")
    model: str = Field(..., description="实际模型 ID，如 gpt-4o, deepseek-chat")
    api_key: str = Field(..., description="API Key")
    api_base: Optional[str] = Field(None, description="API Base URL")
    adapter_type: str = Field("openai_compatible", description="适配器类型: openai_compatible / anthropic")
    is_builtin: bool = Field(False, description="是否为内置模型")
    enabled: bool = Field(True, description="是否启用")


class ExtractionType(str, Enum):
    """响应数据提取方式"""
    JSON_PATH = "json_path"
    REGEX = "regex"
    BOUNDARY = "boundary"
    X_PATH = "xpath"
    HEADER = "header"
    JSR223 = "jsr223"


class ExtractionRule(BaseModel):
    """从响应中提取数据的规则 — 对应 JMeter PostProcessor"""
    ref_name: str = Field(..., description="存储提取值的变量名")
    extract_type: ExtractionType = Field(..., description="提取方式")
    expression: str = Field(..., description="提取表达式")
    default_value: Optional[str] = Field("NOT_FOUND")
    match_number: Optional[int] = Field(1)
    template: Optional[str] = Field(None, description="模板，$1$表示第一个捕获组")
    target_field: Optional[str] = Field(None)


class VariableBinding(BaseModel):
    """将提取的变量绑定到下游请求的指定位置"""
    variable_name: str = Field(..., description="变量名")
    target_location: str = Field(..., description="绑定位置: header/path/query/body/url")
    target_key: str = Field("", description="具体位置的key")
    template: Optional[str] = Field(None, description="自定义模板")


class APIEndpoint(BaseModel):
    name: str = Field(..., description="接口名称")
    method: str = Field(..., description="HTTP方法")
    path: str = Field(..., description="接口路径")
    description: Optional[str] = Field(None, description="接口描述")
    headers: Optional[Dict[str, str]] = Field(default_factory=dict)
    query_params: Optional[Dict[str, Any]] = Field(default_factory=dict)
    body: Optional[Dict[str, Any]] = None
    content_type: Optional[str] = "application/json"
    expected_status: Optional[int] = 200
    extractions: Optional[List[ExtractionRule]] = Field(default_factory=list, description="从此接口响应中提取的变量")
    bindings: Optional[List[VariableBinding]] = Field(default_factory=list, description="注入到本请求的变量绑定")
    depends_on: Optional[List[str]] = Field(default_factory=list, description="本接口依赖的其他接口名称列表")
    order_hint: Optional[int] = Field(None, description="执行顺序提示")


class RequestDependency(BaseModel):
    """描述请求之间的依赖关系 — 下游请求需要上游响应的数据"""
    source_endpoint: str = Field(..., description="源接口名称（产生数据的请求）")
    source_extractions: List[ExtractionRule] = Field(
        default_factory=list, description="从源接口响应中提取的变量"
    )
    target_endpoint: str = Field(..., description="目标接口名称（消费数据的请求）")
    bindings: List[VariableBinding] = Field(
        default_factory=list, description="变量绑定到目标请求的位置"
    )


class CorrelationConfig(BaseModel):
    """请求关联/依赖配置 — 用于自动化测试场景编排"""
    dependencies: List[RequestDependency] = Field(
        default_factory=list, description="请求依赖关系列表"
    )
    # 自动检测常见认证模式
    auto_detect_auth: bool = Field(
        True, description="是否自动检测认证模式（如登录→token）"
    )
    auth_patterns: List[str] = Field(
        default_factory=lambda: ["login", "auth", "token", "signin", "oauth"],
        description="用于自动检测认证接口的路径关键词"
    )


class EnvironmentConfig(BaseModel):
    """单个环境配置"""
    id: str = Field(..., description="环境唯一标识")
    name: str = Field("Development", description="环境名称")
    base_url: str = Field("", description="基础URL")
    variables: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    headers: Dict[str, str] = Field(default_factory=dict, description="全局请求头")


class ProjectEnvironments(BaseModel):
    """项目多环境配置"""
    environments: List[EnvironmentConfig] = Field(default_factory=list, description="环境列表")
    active_id: Optional[str] = Field(None, description="当前激活的环境ID")


class ProjectEnvironment(BaseModel):
    """项目环境配置（向后兼容，deprecated）"""
    base_url: str = Field("", description="基础URL")
    variables: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    headers: Dict[str, str] = Field(default_factory=dict, description="全局请求头")


class ProjectInfo(BaseModel):
    """项目信息"""
    id: str = Field(..., description="项目唯一标识")
    name: str = Field(..., description="项目名称")
    description: Optional[str] = Field(None, description="项目描述")
    environment: ProjectEnvironment = Field(default_factory=ProjectEnvironment, description="环境配置(兼容旧版)")
    environments: Optional[ProjectEnvironments] = Field(None, description="多环境配置(新版)")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_at: Optional[datetime] = Field(None, description="更新时间")
    storage_version: int = Field(1, exclude=True, repr=False)


class CreateProjectRequest(BaseModel):
    """创建项目请求"""
    name: str = Field(..., description="项目名称")
    description: Optional[str] = Field(None, description="项目描述")
    environment: Optional[ProjectEnvironment] = Field(None, description="环境配置(兼容旧版)")
    environments: Optional[ProjectEnvironments] = Field(None, description="多环境配置(新版)")


class UpdateProjectRequest(BaseModel):
    """更新项目请求"""
    name: Optional[str] = Field(None, description="项目名称")
    description: Optional[str] = Field(None, description="项目描述")
    environment: Optional[ProjectEnvironment] = Field(None, description="环境配置(兼容旧版)")
    environments: Optional[ProjectEnvironments] = Field(None, description="多环境配置(新版)")


class CreateEnvironmentRequest(BaseModel):
    """创建环境请求"""
    name: str = Field(..., description="环境名称")
    base_url: str = Field("", description="基础URL")
    variables: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    headers: Dict[str, str] = Field(default_factory=dict, description="全局请求头")


class UpdateEnvironmentRequest(BaseModel):
    """更新环境请求"""
    name: Optional[str] = Field(None, description="环境名称")
    base_url: Optional[str] = Field(None, description="基础URL")
    variables: Optional[Dict[str, str]] = Field(None, description="环境变量")
    headers: Optional[Dict[str, str]] = Field(None, description="全局请求头")


class TestConfig(BaseModel):
    test_type: TestType = Field(..., description="测试类型")
    # No default rate: a throughput timer the user never asked for silently caps the load.
    target_rps: Optional[int] = Field(None, description="目标RPS（不填则不限速）")
    concurrent_users: Optional[int] = Field(100, description="并发用户数")
    ramp_up_seconds: Optional[int] = Field(60, description="Ramp-up时间(秒)")
    duration_seconds: Optional[int] = Field(300, description="测试持续时间(秒)")
    iterations: Optional[int] = Field(None, description="迭代次数(与duration二选一)")
    think_time_ms: Optional[int] = Field(0, description="思考时间(ms)")
    # 阶梯测试专用
    step_users: Optional[int] = Field(10, description="每阶梯增加用户数")
    step_duration: Optional[int] = Field(60, description="每阶梯持续时间(秒)")
    # 稳定性测试专用
    stability_hours: Optional[float] = Field(1.0, description="稳定性测试小时数")


class GenerateTestPlanRequest(BaseModel):
    api_doc: str = Field(..., description="API接口文档内容，支持OpenAPI/Swagger或纯文本")
    endpoints: Optional[List[APIEndpoint]] = Field(None, description="指定要测试的接口")
    test_config: TestConfig = Field(..., description="测试配置")
    ai_model: Optional[str] = Field(None, description="使用的AI模型")
    base_url: Optional[str] = Field(None, description="基础URL")
    lang: Optional[str] = Field(None, description="输出语言: zh-CN 或 en")


class TestPlanInfo(BaseModel):
    id: str
    project_id: str = Field(default="default", description="所属项目ID")
    name: str
    description: Optional[str] = None
    jmx_path: str
    created_at: datetime
    test_config: TestConfig
    endpoints_count: int
    storage_version: int = Field(1, exclude=True, repr=False)


class AIWorkflowRequest(BaseModel):
    """AI 工作流请求 —— 从API文档和测试需求自动创建项目和测试用例"""
    api_documentation: str = Field(..., description="API接口文档内容，支持OpenAPI/Swagger或纯文本")
    testing_requirements: str = Field(..., description="测试需求描述，如'测试登录和支付接口在高并发下的性能'")
    ai_model: Optional[str] = Field(None, description="使用的AI模型ID")
    lang: Optional[str] = Field(None, description="输出语言: zh-CN 或 en")


class AIWorkflowCreateResponse(BaseModel):
    """AI 工作流创建结果"""
    project: ProjectInfo
    test_plans: List[TestPlanInfo] = Field(default_factory=list)
    execution_ids: Optional[List[str]] = Field(None, description="如果同时执行，返回执行ID列表")
    diagnostic_summary: Optional[str] = Field(None, description="AI生成的诊断摘要")


class ExecuteTestRequest(BaseModel):
    test_plan_id: str
    ai_model: Optional[str] = None


class TestExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorDetail(BaseModel):
    error_type: str = Field(..., description="错误类型/响应码")
    count: int = Field(..., description="错误次数")
    percentage: float = Field(..., description="占总请求百分比")
    sample_messages: List[str] = Field(default_factory=list, description="错误示例消息")


class TimelineSegment(BaseModel):
    time_range: str = Field(..., description="时间段，如 0-30s, 30-60s")
    avg_response_time: float = Field(..., description="该时间段平均响应时间(ms)")
    max_response_time: float = Field(..., description="该时间段最大响应时间(ms)")
    throughput: float = Field(..., description="该时间段吞吐量(req/s)")
    error_rate: float = Field(..., description="该时间段错误率(%)")
    request_count: int = Field(..., description="该时间段请求数")


class PerformanceTrend(BaseModel):
    trend_direction: str = Field(..., description="趋势方向：improving(改善)/degrading(恶化)/stable(稳定)/fluctuating(波动)")
    description: str = Field(..., description="趋势描述")


class DetailedTestSummary(BaseModel):
    total_requests: int
    avg_response_time: float
    min_response_time: float
    max_response_time: float
    p95_response_time: float = 0.0   # populated from percentiles["p95"]; required by SLA engine
    p99_response_time: float = 0.0   # populated from percentiles["p99"]; required by SLA engine
    error_rate: float
    throughput: float  # req/s
    received_bytes_per_sec: float
    sent_bytes_per_sec: float
    percentiles: Dict[str, float] = Field(default_factory=dict)  # p50, p90, p95, p99
    error_details: List[ErrorDetail] = Field(default_factory=list, description="错误详情列表")
    timeline_segments: List[TimelineSegment] = Field(default_factory=list, description="时间段性能数据")
    performance_trend: Optional[PerformanceTrend] = Field(None, description="性能趋势分析")
    response_time_distribution: Dict[str, Any] = Field(default_factory=dict, description="响应时间分布")
    per_label: Optional[Dict[str, Dict[str, Any]]] = Field(None, description="按接口标签(采样器名称)的详细统计")


class TestSummary(BaseModel):
    total_requests: int
    avg_response_time: float
    min_response_time: float
    max_response_time: float
    error_rate: float
    throughput: float  # req/s
    received_bytes_per_sec: float
    sent_bytes_per_sec: float
    percentiles: Dict[str, float]  # p50, p90, p95, p99


class ReportAnalysis(BaseModel):
    analysis_status: Literal["completed", "invalid", "failed"] = "completed"
    evidence_status: Literal["unverified"] = "unverified"
    overall_assessment: str = Field(..., description="整体性能评估总结")
    key_findings: List[str] = Field(default_factory=list, description="关键发现列表")
    specific_issues: List[str] = Field(default_factory=list, description="具体问题定位，如'并发数达到500时响应时间突增到5s'")
    timeline_analysis: str = Field("", description="按时间段的性能变化分析")
    resource_bottlenecks: List[str] = Field(default_factory=list, description="可能的资源瓶颈（CPU/内存/网络/数据库/连接池等）")
    performance_bottlenecks: List[str] = Field(default_factory=list, description="性能瓶颈点")
    error_analysis: List[str] = Field(default_factory=list, description="错误类型分析及根因")
    recommendations: List[str] = Field(default_factory=list, description="优化建议列表")
    improvement_plan: List[str] = Field(default_factory=list, description="具体的分步改进计划")
    risk_areas: List[str] = Field(default_factory=list, description="风险区域")
    conclusion: str = Field(..., description="最终结论")
    # Requirement verdict when the analysis was written, so the report can flag it once requirements change.
    requirements_verdict: Optional[Dict[str, Any]] = None


class TestExecutionInfo(BaseModel):
    id: str
    project_id: str = Field(default="default", description="所属项目ID")
    test_plan_id: str
    status: TestExecutionStatus
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    jmx_path: str
    report_dir: Optional[str] = None
    log_path: Optional[str] = None
    error_message: Optional[str] = None
    progress: Optional[int] = 0  # 0-100
    summary: Optional[DetailedTestSummary] = None
    ai_analysis: Optional[ReportAnalysis] = None
    source_jmx_path: Optional[str] = None
    source_sha256: Optional[str] = None
    context_snapshot: Optional[Dict[str, Any]] = None
    report_status: str = "pending"
    report_error: Optional[str] = None
    resource_snapshot_pre: Optional[Dict[str, Any]] = None
    resource_snapshot_post: Optional[Dict[str, Any]] = None
    storage_version: int = Field(1, exclude=True, repr=False)


class TestReport(BaseModel):
    execution_id: str
    project_id: str = "default"
    test_plan_id: str
    test_plan_name: str
    start_time: Optional[datetime] = None
    end_time: datetime
    duration_seconds: float
    summary: DetailedTestSummary
    raw_data_path: Optional[str] = None
    html_report_path: Optional[str] = None
    ai_analysis: Optional[ReportAnalysis] = None
    sla_result: Optional[Dict[str, Any]] = Field(None, description="SLA评估结果（未录入性能需求时的默认阈值）")
    evidence: Optional[Dict[str, Any]] = Field(None, description="需求达标、基线对比、服务端指标等确定性证据")
    status: TestExecutionStatus


class AIChatMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str = ""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    pinned: bool = Field(default=False, exclude=True)

    def to_wire(self) -> dict:
        return self.model_dump(exclude_none=True)


class TestTemplate(BaseModel):
    id: str
    name: str
    test_type: TestType
    description: str
    default_config: TestConfig
    icon: Optional[str] = None


class JMXTreeNode(BaseModel):
    """JMX 树节点（JSON 表示）"""
    id: str
    testclass: str
    testname: str
    enabled: bool = True
    properties: Dict[str, Any] = Field(default_factory=dict)
    children: List["JMXTreeNode"] = Field(default_factory=list)


JMXTreeNode.model_rebuild()


class UpdateJMXTreeRequest(BaseModel):
    """保存 JMX 树请求"""
    tree: JMXTreeNode
    name: Optional[str] = None


# ===== Agent Framework Schemas =====

class AgentRunRequest(BaseModel):
    """请求运行 Agent"""
    agent_type: str = Field("test_plan", description="Agent 类型: test_plan / analysis / workflow")
    task: str = Field(..., description="任务描述")
    context: Optional[str] = Field(None, description="附加上下文信息")
    model: Optional[str] = Field(None, description="使用的 AI 模型ID")
    stream: bool = Field(False, description="是否使用 SSE 流式返回")
    session_id: Optional[str] = Field(None, description="会话 ID，用于多轮记忆持久化")
    execution_id: Optional[str] = Field(None, min_length=1, max_length=50,
        description="仅 analysis 可用：分析已登记执行的 JTL；省略时保留通用 Agent 行为")

    @model_validator(mode='after')
    def execution_requires_analysis(self):
        if self.execution_id is not None and self.agent_type != 'analysis':
            raise ValueError('execution_id is only supported by the analysis agent')
        return self


class AgentRunResponse(BaseModel):
    """Agent 运行结果"""
    agent_type: str
    task: str
    success: bool
    output: str
    plan_steps: Optional[list[dict]] = Field(None, description="执行步骤列表")
    iterations: int = 0
    error: Optional[str] = None
    duration_seconds: float = 0.0


class AgentAnalysisRunResponse(AgentRunResponse):
    """Optional registered analysis payload; legacy workflow responses stay unchanged."""
    analysis: Optional[dict] = None
    receipt: Optional[dict] = None


class AgentListItem(BaseModel):
    """可用 Agent 列表项"""
    name: str
    description: str
    agent_type: str


class AgentStatusResponse(BaseModel):
    """Agent 运行状态"""
    agent_id: str
    agent_type: str
    state: str  # idle / running / completed / failed / cancelled
    task: str
    started_at: Optional[str] = None


class WorkflowAgentRequest(BaseModel):
    """WorkflowAgent 专用请求"""
    api_documentation: str = Field(..., description="API 接口文档内容")
    testing_requirements: str = Field("", description="测试需求描述")
    test_types: Optional[List[str]] = Field(None, description="测试类型列表")
    ai_model: Optional[str] = Field(None, description="使用的 AI 模型")
    execute: bool = Field(False, description="生成后是否立即执行测试")
    stream: bool = Field(False, description="是否使用 SSE 流式返回")


# ===== Anti-Pollution Memory Schemas =====

class HypothesisStatus(str, Enum):
    """假设验证状态"""
    UNTESTED = "untested"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    DEPRECATED = "deprecated"


class HypothesisCategory(str, Enum):
    """假设类别"""
    BOTTLENECK = "bottleneck"
    ROOT_CAUSE = "root_cause"
    RECOMMENDATION = "recommendation"
    RISK = "risk"


class MetricClaim(BaseModel):
    """LLM 文本中提取出的指标声明"""
    metric_name: str = Field(..., description="指标名，如 p99、TPS、error_rate")
    claimed_value: float = Field(..., description="声明数值")
    unit: Optional[str] = Field(None, description="单位，如 ms、req/s、%")
    context: Optional[str] = Field(None, description="原文上下文")


class FactSheet(BaseModel):
    """一次测试执行的确定性事实表。

    所有数字均由代码从原始 JTL 计算得出，作为 ground truth。
    """
    execution_id: str = Field(..., description="执行 ID")
    module: Optional[str] = Field(None, description="模块名")
    version: Optional[str] = Field(None, description="版本")
    test_type: Optional[str] = Field(None, description="测试类型")
    jtl_path: Optional[str] = Field(None, description="原始 JTL 文件路径")
    context_scope: str = ""
    jtl_sha256: str = ""
    schema_version: str = Field("1.0", description="事实 schema 版本")
    computed_at: datetime = Field(default_factory=datetime.now, description="计算时间")

    # 聚合指标
    total_requests: int = Field(0, description="总请求数")
    avg_response_time: float = Field(0.0, description="平均响应时间 ms")
    min_response_time: float = Field(0.0, description="最小响应时间 ms")
    max_response_time: float = Field(0.0, description="最大响应时间 ms")
    error_rate: float = Field(0.0, description="错误率 %")
    throughput: float = Field(0.0, description="吞吐量 req/s")
    received_bytes_per_sec: float = Field(0.0, description="接收字节/s")
    sent_bytes_per_sec: float = Field(0.0, description="发送字节/s")
    percentiles: Dict[str, float] = Field(default_factory=dict, description="p50/p90/p95/p99")

    # 细分指标
    per_label: Optional[Dict[str, Dict[str, Any]]] = Field(None, description="按接口统计")
    timeline_segments: List[TimelineSegment] = Field(default_factory=list, description="时间段指标")
    error_details: List[ErrorDetail] = Field(default_factory=list, description="错误详情")
    performance_trend: Optional[PerformanceTrend] = Field(None, description="性能趋势")

    # 原始引用
    raw_summary: Optional[DetailedTestSummary] = Field(None, description="原始 DetailedTestSummary 快照")


class ValidationResult(BaseModel):
    """事实校验结果"""
    is_valid: bool = Field(..., description="是否全部数字校验通过")
    claims: List[MetricClaim] = Field(default_factory=list, description="提取到的声明")
    mismatches: List[Dict[str, Any]] = Field(default_factory=list, description="不匹配项")
    missing_fields: List[str] = Field(default_factory=list, description="无法回验的字段")
    corrected_summary: Optional[str] = Field(None, description="修正后的分析摘要")


class Hypothesis(BaseModel):
    """可证伪的假设条目"""
    hypothesis_id: str = Field(..., description="假设唯一 ID")
    source_execution_id: str = Field(..., description="来源执行 ID")
    module: str = Field(..., description="模块")
    version: Optional[str] = Field(None, description="版本")
    test_type: Optional[str] = Field(None, description="测试类型")

    category: HypothesisCategory = Field(HypothesisCategory.BOTTLENECK, description="假设类别")
    statement: str = Field(..., description="解释性结论，如'瓶颈在数据库连接池'")
    confidence: float = Field(0.5, ge=0.0, le=1.0, description="置信度 0-1")
    status: HypothesisStatus = Field(HypothesisStatus.UNTESTED, description="验证状态")

    # 结构化依赖与预测
    supporting_facts: List[str] = Field(default_factory=list, description="依赖的指标字段名")
    testable_prediction: Optional[str] = Field(None, description="可验证预测")
    falsification_design: Optional[str] = Field(None, description="建议的证伪实验设计")

    # 验证追踪
    verified_by_execution_id: Optional[str] = Field(None, description="证实它的执行 ID")
    refuted_by_execution_id: Optional[str] = Field(None, description="证伪它的执行 ID")
    verification_notes: Optional[str] = Field(None, description="验证备注")

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class StructuredAnalysis(BaseModel):
    """LLM 分析的结构化输出（供验证与归档）"""
    execution_id: Optional[str] = Field(None, description="执行 ID")
    module: Optional[str] = Field(None, description="模块")
    version: Optional[str] = Field(None, description="版本")
    overall_assessment: str = Field("", description="整体评估")
    metric_statements: List[str] = Field(default_factory=list, description="指标陈述（应可验证）")
    hypotheses: List[Hypothesis] = Field(default_factory=list, description="解释性假设")
    recommendations: List[str] = Field(default_factory=list, description="优化建议")
