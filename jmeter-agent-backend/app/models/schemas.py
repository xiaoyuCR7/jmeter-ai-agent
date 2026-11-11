import hashlib
import json
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SerializationInfo, field_serializer, field_validator
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
    api_key: str = Field("", description="API Key；本机服务（Ollama 等）可以不填")
    api_base: Optional[str] = Field(None, description="API Base URL")
    adapter_type: Literal["openai_compatible", "anthropic", "azure_openai"] = Field(
        "openai_compatible", description="适配器类型: openai_compatible / anthropic / azure_openai")
    provider: Optional[str] = Field(None, max_length=40, description="设置页选择的服务商，如 deepseek、gemini、ollama")
    api_version: Optional[str] = Field(None, max_length=40, description="Azure OpenAI 的 API 版本")
    context_window: Optional[int] = Field(None, ge=4096, le=2_000_000,
                                          description="模型上下文窗口(tokens)；不填则用全局保守默认值")
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


class ResponseCheck(BaseModel):
    """A business result the response must show, beyond its HTTP status."""
    json_path: str = Field(..., min_length=1, description="JSON Path，如 $.code")
    expected: Optional[str] = Field(None, description="期望值；不填则只要求该字段存在")

    @field_validator('expected', mode='before')
    @classmethod
    def _wildcard_means_exists(cls, value):
        # Models write "*" for "any value", which JSON assertions compare literally and always fail.
        return None if value in ('*', '') else (str(value) if isinstance(value, (int, float, bool)) else value)


class APIEndpoint(BaseModel):
    name: str = Field(..., description="接口名称")
    method: str = Field(..., description="HTTP方法")
    path: str = Field(..., description="接口路径")
    description: Optional[str] = Field(None, description="接口描述")
    headers: Optional[Dict[str, str]] = Field(default_factory=dict)
    query_params: Optional[Dict[str, Any]] = Field(default_factory=dict)
    body: Optional[Any] = Field(None, description="请求体：JSON 对象或数组，表单接口用对象，也可以是原始字符串")
    content_type: Optional[str] = "application/json"
    expected_status: Optional[int] = 200
    response_checks: Optional[List[ResponseCheck]] = Field(default_factory=list, description="响应体业务断言")
    phase: Literal["iteration", "once_per_user", "setup"] = Field(
        "iteration", description="iteration 每轮执行；once_per_user 每个用户一次（登录）；setup 压测开始前执行一次（准备数据）")
    weight: Optional[int] = Field(None, ge=1, le=100, description="占迭代的百分比，不填表示每轮都执行")
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


SECRET_MASK = "******"
EnvironmentKind = Literal["development", "testing", "staging", "production"]
# What identified an environment before it had a kind, a thread cap or its own monitoring;
# the fingerprint stays over these so earlier runs remain comparable.
ENVIRONMENT_IDENTITY = ("id", "name", "base_url", "variables", "headers")


class EnvironmentConfig(BaseModel):
    """单个环境配置"""
    id: str = Field(..., description="环境唯一标识")
    name: str = Field("Development", description="环境名称")
    base_url: str = Field("", description="基础URL")
    variables: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    headers: Dict[str, str] = Field(default_factory=dict, description="全局请求头")
    kind: EnvironmentKind = Field("testing", description="环境类型；生产环境运行前需要确认")
    secret_keys: List[str] = Field(default_factory=list, description="值为敏感信息的变量名和请求头名")
    max_threads: Optional[int] = Field(None, ge=1, le=100000, description="在这个环境运行时允许的最大总线程数")
    prometheus_url: Optional[str] = Field(None, description="这个环境的 Prometheus 地址，覆盖项目的监控地址")

    @field_serializer("variables", "headers")
    def _mask_secrets(self, values: Dict[str, str], info: SerializationInfo):
        """Responses never carry a secret; only persistence and the run ask for the real values."""
        if (info.context or {}).get("reveal_secrets"):
            return values
        return {key: SECRET_MASK if key in self.secret_keys else value for key, value in values.items()}

    def revealed(self) -> dict:
        return self.model_dump(context={"reveal_secrets": True})

    def fingerprint(self) -> str:
        data = self.revealed()
        return hashlib.sha256(json.dumps({key: data[key] for key in ENVIRONMENT_IDENTITY},
                                         sort_keys=True).encode()).hexdigest()


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


_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}")


def check_environment_values(values: Optional[Dict[str, str]], pattern, what: str):
    """Values are literals or ${name} references: a JMeter function here would run code the plan's review never saw."""
    for key, value in (values or {}).items():
        if not pattern.fullmatch(key):
            raise ValueError(f"{what}名 {key!r} 不合法")
        if "${__" in value or len(value) > 4000 or "\n" in value or "\r" in value:
            raise ValueError(f"{what} {key} 的值不能包含 JMeter 函数或换行，且不超过 4000 字符")
    return values


def check_base_url(value: Optional[str], what: str = "base_url") -> Optional[str]:
    if not value or not value.strip():
        return value
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment:
        raise ValueError(f"{what} 必须是不带账号、查询参数的 http(s) 地址")
    return value.strip().rstrip("/")


class _EnvironmentFields(BaseModel):
    @field_validator("variables", check_fields=False)
    @classmethod
    def _variables(cls, value):
        return check_environment_values(value, _VARIABLE_NAME, "变量")

    @field_validator("headers", check_fields=False)
    @classmethod
    def _headers(cls, value):
        return check_environment_values(value, _HEADER_NAME, "请求头")

    @field_validator("base_url", check_fields=False)
    @classmethod
    def _base_url(cls, value):
        return check_base_url(value)

    @field_validator("prometheus_url", check_fields=False)
    @classmethod
    def _prometheus_url(cls, value):
        return check_base_url(value, "Prometheus 地址") or None


class CreateEnvironmentRequest(_EnvironmentFields):
    """创建环境请求"""
    name: str = Field(..., min_length=1, max_length=100, description="环境名称")
    base_url: str = Field("", description="基础URL")
    variables: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    headers: Dict[str, str] = Field(default_factory=dict, description="全局请求头")
    kind: EnvironmentKind = "testing"
    secret_keys: List[str] = Field(default_factory=list)
    max_threads: Optional[int] = Field(None, ge=1, le=100000)
    prometheus_url: Optional[str] = None
    copy_from: Optional[str] = Field(None, description="复制的来源环境；值为 ****** 的敏感项从它那里取")


class UpdateEnvironmentRequest(_EnvironmentFields):
    """更新环境请求；敏感值传回 ****** 表示保持原值"""
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="环境名称")
    base_url: Optional[str] = Field(None, description="基础URL")
    variables: Optional[Dict[str, str]] = Field(None, description="环境变量")
    headers: Optional[Dict[str, str]] = Field(None, description="全局请求头")
    kind: Optional[EnvironmentKind] = None
    secret_keys: Optional[List[str]] = None
    max_threads: Optional[int] = Field(None, ge=1, le=100000)
    clear_max_threads: bool = False
    prometheus_url: Optional[str] = None


class CsvDataFile(BaseModel):
    """Per-user test data: an existing file in test_data, or rows to store there."""
    filename: Optional[str] = Field(None, description="test_data 目录下的文件名")
    variables: List[str] = Field(..., min_length=1, description="列对应的变量名")
    rows: Optional[List[List[str]]] = Field(None, description="直接提供的数据行")
    delimiter: str = ","
    recycle: bool = True
    stop_thread: bool = False
    sharing: Literal["all", "current_thread", "current_thread_group"] = "all"


class TestConfig(BaseModel):
    __test__ = False  # A domain model, not a pytest test class.
    test_type: TestType = Field(..., description="测试类型")
    # No default rate: a throughput timer the user never asked for silently caps the load.
    target_rps: Optional[int] = Field(None, description="目标RPS（不填则不限速）")
    concurrent_users: Optional[int] = Field(100, description="并发用户数")
    ramp_up_seconds: Optional[int] = Field(60, description="Ramp-up时间(秒)")
    duration_seconds: Optional[int] = Field(300, description="测试持续时间(秒)")
    iterations: Optional[int] = Field(None, description="迭代次数(与duration二选一)")
    think_time_ms: Optional[int] = Field(0, description="思考时间(ms)")
    connect_timeout_ms: Optional[int] = Field(5000, description="连接超时(ms)")
    response_timeout_ms: Optional[int] = Field(30000, description="响应超时(ms)")
    csv_data_files: Optional[List[CsvDataFile]] = Field(None, description="参数化数据文件")
    # 阶梯测试专用
    step_users: Optional[int] = Field(10, description="每阶梯增加用户数")
    step_duration: Optional[int] = Field(60, description="每阶梯持续时间(秒)")
    # 稳定性测试专用
    stability_hours: Optional[float] = Field(None, description="稳定性测试小时数；填写后覆盖持续时间")


class GenerateTestPlanRequest(BaseModel):
    api_doc: str = Field(..., description="API接口文档内容，支持OpenAPI/Swagger或纯文本")
    endpoints: Optional[List[APIEndpoint]] = Field(None, description="指定要测试的接口")
    test_config: TestConfig = Field(..., description="测试配置")
    ai_model: Optional[str] = Field(None, description="使用的AI模型")
    base_url: Optional[str] = Field(None, description="基础URL")
    lang: Optional[str] = Field(None, description="输出语言: zh-CN 或 en")


class TestPlanInfo(BaseModel):
    __test__ = False  # A domain model, not a pytest test class.
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
    api_documentation: str = Field(..., max_length=10_000_000, description="API接口文档内容，支持OpenAPI/Swagger或纯文本")
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
    __test__ = False  # A domain model, not a pytest test class.
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
    labels: Dict[str, int] = Field(default_factory=dict, description="出现该错误最多的接口及次数")


class TimelineSegment(BaseModel):
    time_range: str = Field(..., description="时间段，如 0-30s, 30-60s")
    avg_response_time: float = Field(..., description="该时间段平均响应时间(ms)")
    max_response_time: float = Field(..., description="该时间段最大响应时间(ms)")
    throughput: float = Field(..., description="该时间段吞吐量(req/s)")
    error_rate: float = Field(..., description="该时间段错误率(%)")
    request_count: int = Field(..., description="该时间段请求数")
    active_threads: Optional[int] = Field(None, description="该时间段内的最大活跃线程数")
    avg_connect_time: Optional[float] = Field(None, description="该时间段平均建连耗时(ms)")
    avg_latency: Optional[float] = Field(None, description="该时间段平均 Latency(ms)，含建连")
    avg_wait_time: Optional[float] = Field(None, description="该时间段建连之后到首字节的平均等待(ms)")


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
    success: Optional[Dict[str, Any]] = Field(None, description="只按成功样本计算的时延和吞吐")
    diagnostics: Optional[Dict[str, Any]] = Field(None, description="负载、稳定段、耗时拆解和最慢样本")


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
    evidence_status: Literal["unverified", "verified", "mismatch"] = "unverified"
    fact_check: Optional[Dict[str, Any]] = Field(None, description="AI 文本中的数字与实测值的核对结果")
    usage: Optional[Dict[str, int]] = Field(None, description="写这份分析用的模型调用次数和 token")
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
    limitations: List[str] = Field(default_factory=list, description="这次分析受到的限制，只在这里说")
    hypotheses: List[Dict[str, Any]] = Field(default_factory=list, description="可以被后续运行验证的假设")
    conclusion: str = Field(..., description="最终结论")
    # Requirement verdict when the analysis was written, so the report can flag it once requirements change.
    requirements_verdict: Optional[Dict[str, Any]] = None
    follow_ups: List[Dict[str, Any]] = Field(default_factory=list, description="用户对这份分析的追问和回答")
    language: str = Field("zh-CN", description="分析所用的语言：zh-CN 或 en")


class ReanalyzeRequest(BaseModel):
    model: Optional[str] = Field(None, description="使用的 AI 模型ID")
    lang: Optional[str] = Field(None, description="分析语言 zh-CN / en；不填用 REPORT_LANGUAGE")


class RunQuestion(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="对这次运行的追问")
    model: Optional[str] = Field(None, description="使用的 AI 模型ID")


class TestExecutionInfo(BaseModel):
    __test__ = False  # A domain model, not a pytest test class.
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
    # False on a headline copy from a scan of the run cache: no per-request detail, no AI report, never saved.
    detail_loaded: bool = Field(True, exclude=True, repr=False)


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
    failure_diagnosis: Optional[Dict[str, Any]] = Field(None, description="运行失败时从错误信息和日志得出的诊断")
    status: TestExecutionStatus
    report_status: str = Field("pending", description="结果解析和运行后分析的状态：running 时 AI 分析还在写")
    report_error: Optional[str] = None
    analysis_enabled: bool = Field(True, description="运行结束后是否自动写 AI 分析")
    environment: Optional[Dict[str, Any]] = Field(None, description="这次运行用的环境：名称、类型、地址和替换了哪些主机")


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
    # Where and under what load it ran, so runs of other plans and other loads can be compared.
    project_id: str = ""
    environment_fingerprint: str = ""
    load: Dict[str, Any] = Field(default_factory=dict, description="实际线程组的总线程数、时长和 think time")
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
    unbound_claims: int = Field(0, description="没有写明接口或整体、只能对照任意范围的声明数")
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
    prediction: Optional[Dict[str, Any]] = Field(None, description="程序能检查的预测：指标、接口、方向、数值和运行条件")
    project_id: str = Field("", description="所属项目；同项目其它计划的运行也能验证它")
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
