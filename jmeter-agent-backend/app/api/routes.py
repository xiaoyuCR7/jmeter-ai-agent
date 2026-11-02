import os
import shutil
import uuid
import json
import asyncio
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Optional, Any

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query, Depends, Response
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter()

from app.core import local_access
from app.core.config import settings
from app.middleware.local_guard import REPORT_POLICY
from app.services.db_store import db_store
from app.models.schemas import (
    GenerateTestPlanRequest, TestPlanInfo, ExecuteTestRequest,
    TestExecutionInfo, TestReport, TestExecutionStatus,
    AIChatMessage, TestTemplate, TestType, AIModelConfig,
    JMXTreeNode, UpdateJMXTreeRequest,
    ProjectInfo, ProjectEnvironment, ProjectEnvironments, EnvironmentConfig,
    CreateProjectRequest, UpdateProjectRequest,
    CreateEnvironmentRequest, UpdateEnvironmentRequest,
    AIWorkflowRequest, AIWorkflowCreateResponse,
    APIEndpoint,
    RunQuestion, ReanalyzeRequest,
)
from app.core.config import (
    get_all_model_configs, load_user_model_configs, save_user_model_configs,
    sanitize_model_config,
    load_paths_config, save_paths_config
)
from app.services.ai.adapters.factory import AIAdapterFactory, HEALTH_CHECK_TIMEOUT_SECONDS
from app.services.jmeter import jmx_lint
from app.services.jmeter.jmx_generator import JMXGenerator
from app.services.jmeter.executor import get_executor
from app.services.jmeter.report_parser import ReportParser
from app.services.jmeter.jmx_parser import parse_jmx
from app.services.jmeter.jmx_builder import build_jmx
from app.services.jmeter.script_risk import ScriptApprovalRequired
from app.services.jmeter.component_registry import get_all_components, get_allowed_children
from app.services.jmeter.sla_engine import SLAEngine, SLAConfig
from app.services.jmeter.function_registry import get_all_functions
from app.services.ai.agent.tools.jmeter import resource_hooks
from app.services.ai.agents import AnalysisAgent
from app.services.ai.agents.analysis_agent import AnalysisExecutionError
from app.services.openapi_importer import OpenAPIImporter
from app.services.ml_analysis import ml_detector
from app.services.storage import store
from app.services.execution_lifecycle import execution_lifecycle, persist_project, persist_execution
from app.services import run_diagnostics
from app.services import post_run
from app.services.post_run import save_analysis_markdown

# JMeter executor — obtained lazily via get_executor() at each call site so
# importing this module never probes the filesystem for the JMeter binary.
_report_parser = ReportParser()


DEFAULT_PROJECT_ID = "default"
MAX_FOLLOW_UP_CONTEXT = 3  # Earlier answers a follow-up question can build on.
_question_locks: dict[str, asyncio.Lock] = {}  # One answer saved at a time per run.
WORKFLOW_PROGRESS = {'create_project': (35, '正在创建项目...', 'Creating the project...'),
                     'create_jmx': (60, '正在生成测试计划...', 'Generating the test plan...')}


# ═══════════════════════════════════════════════════════════
# Persistence helpers — write to DBStore + sync in-memory cache
# ═══════════════════════════════════════════════════════════

async def _persist_project(project: ProjectInfo):
    await persist_project(project, create=False)


async def _persist_test_plan(plan: TestPlanInfo):
    await execution_lifecycle.register_plan(plan)


async def _persist_execution(execution: TestExecutionInfo):
    await persist_execution(execution)


async def _delete_project_from_db(project_id: str):
    """Delete project from both DB and in-memory store."""
    await db_store.delete_project(project_id)
    store.delete_project(project_id)


async def _delete_test_plan_from_db(plan_id: str):
    """Delete test plan from both DB and in-memory store."""
    await db_store.delete_test_plan(plan_id)
    store.delete_test_plan(plan_id)


# ═══════════════════════════════════════════════════════════
# Startup Helpers
# ═══════════════════════════════════════════════════════════

async def ensure_default_project():
    """确保默认项目存在，并持久化到数据库"""
    if DEFAULT_PROJECT_ID not in store.projects:
        default_project = ProjectInfo(
            id=DEFAULT_PROJECT_ID,
            name="默认项目",
            description="系统自动创建的默认项目，包含未分类的测试计划",
            environment=ProjectEnvironment(),
            created_at=datetime.now()
        )
        await persist_project(default_project, create=True)


def scan_reports():
    """启动时扫描 reports 目录，恢复未在 store 中的执行记录。

    每个执行目录中应有:
    - _done.flag 或 result.jtl 表示测试已完成
    - run_*.bat 记录执行命令
    - jmeter.log 执行日志
    """
    if not os.path.exists(settings.REPORTS_DIR):
        return

    recovered = 0
    for exec_id in os.listdir(settings.REPORTS_DIR):
        exec_dir = os.path.join(settings.REPORTS_DIR, exec_id)
        if not os.path.isdir(exec_dir):
            continue
        if exec_id in store.executions:
            continue  # already tracked

        jtl_path = os.path.join(exec_dir, "result.jtl")
        log_path = os.path.join(exec_dir, "jmeter.log")
        done_flag = os.path.join(exec_dir, "_done.flag")

        if not os.path.exists(jtl_path):
            continue  # no results, skip

        # A partial JTL is not evidence that the process exited successfully.
        marker = ""
        if os.path.exists(done_flag):
            with open(done_flag, encoding="utf-8") as stream:
                marker = stream.read().strip()
        status = (TestExecutionStatus.COMPLETED if marker == "COMPLETED"
                  else TestExecutionStatus.FAILED)

        execution = TestExecutionInfo(
            id=exec_id,
            project_id=DEFAULT_PROJECT_ID,
            test_plan_id="",
            status=status,
            jmx_path="",
            error_message=None if status == TestExecutionStatus.COMPLETED else "Recovered run has no successful completion marker",
            start_time=datetime.fromtimestamp(os.path.getctime(exec_dir)),
            end_time=datetime.fromtimestamp(os.path.getmtime(exec_dir)),
            report_dir=exec_dir,
            log_path=log_path if os.path.exists(log_path) else "",
        )
        store.executions[exec_id] = execution
        recovered += 1

    if recovered > 0:
        import logging
        logging.getLogger(__name__).info(
            f"Recovered {recovered} orphan execution(s) from reports directory"
        )


# ===== Test Templates =====
TEST_TEMPLATES = [
    TestTemplate(
        id="baseline",
        name="基准测试",
        test_type=TestType.BASELINE,
        description="低并发长时间运行，获取系统基础性能指标",
        default_config={"test_type": TestType.BASELINE, "concurrent_users": 5, "ramp_up_seconds": 30, 
                       "duration_seconds": 600, "target_rps": 10, "think_time_ms": 1000},
        icon="📊"
    ),
    TestTemplate(
        id="concurrency",
        name="高并发测试",
        test_type=TestType.CONCURRENCY,
        description="高并发用户数，测试系统最大承载能力",
        default_config={"test_type": TestType.CONCURRENCY, "concurrent_users": 500, "ramp_up_seconds": 120,
                       "duration_seconds": 300, "target_rps": 1000, "think_time_ms": 0},
        icon="🔥"
    ),
    TestTemplate(
        id="ramp_up",
        name="阶梯加压测试",
        test_type=TestType.RAMP_UP,
        description="逐步增加负载，找到系统性能拐点",
        default_config={"test_type": TestType.RAMP_UP, "concurrent_users": 200, "ramp_up_seconds": 600,
                       "duration_seconds": 600, "target_rps": 500, "think_time_ms": 100,
                       "step_users": 20, "step_duration": 60},
        icon="📈"
    ),
    TestTemplate(
        id="stress",
        name="压力测试",
        test_type=TestType.STRESS,
        description="超过正常负载的并发，测试系统极限",
        default_config={"test_type": TestType.STRESS, "concurrent_users": 1000, "ramp_up_seconds": 60,
                       "duration_seconds": 300, "target_rps": 2000, "think_time_ms": 0},
        icon="⚡"
    ),
    TestTemplate(
        id="stability",
        name="稳定性测试",
        test_type=TestType.STABILITY,
        description="中等并发长时间运行，验证系统稳定性",
        default_config={"test_type": TestType.STABILITY, "concurrent_users": 100, "ramp_up_seconds": 60,
                       "duration_seconds": 3600, "target_rps": 200, "think_time_ms": 500},
        icon="🛡️"
    ),
    TestTemplate(
        id="spike",
        name="尖峰测试",
        test_type=TestType.SPIKE,
        description="短时间内大量用户涌入，测试突发流量应对能力",
        default_config={"test_type": TestType.SPIKE, "concurrent_users": 2000, "ramp_up_seconds": 10,
                       "duration_seconds": 120, "target_rps": 5000, "think_time_ms": 0},
        icon="🚀"
    ),
    TestTemplate(
        id="custom",
        name="自定义测试",
        test_type=TestType.CUSTOM,
        description="完全自定义并发、Ramp-up、持续时间等所有测试参数",
        default_config={"test_type": TestType.CUSTOM, "concurrent_users": 100, "ramp_up_seconds": 60,
                       "duration_seconds": 300, "target_rps": 100, "think_time_ms": 0},
        icon="⚙️"
    ),
]


async def _fill_context_window(config: dict) -> dict:
    """Keep a user-entered size; otherwise ask the provider or use the documented size."""
    if not config.get("context_window") and config.get("api_key") not in (None, "", "***"):
        from app.services.ai.model_limits import detect_context_window
        config = {**config, "context_window": await detect_context_window(config)}
    return config


def _load_and_merge_model_config(model_id: str, payload: dict) -> dict:
    """Merge an update payload with the existing config, preserving api_key when
    the frontend sends the redacted placeholder.
    """
    configs = load_user_model_configs()
    existing = next((c for c in configs if c.get("id") == model_id), None)
    if existing and payload.get("api_key") == "***":
        payload = {**payload, "api_key": existing.get("api_key", "")}
    return payload


# ===== AI Models =====
@router.get("/ai/models", response_model=list)
async def get_models():
    """获取可用的 AI 模型列表（仅用户已添加的模型，API Key 已脱敏）"""
    return [sanitize_model_config(c) for c in get_all_model_configs()]


@router.get("/ai/models/user", response_model=list)
async def get_user_models():
    """获取用户自定义 AI 模型列表（API Key 已脱敏）"""
    return [sanitize_model_config(c) for c in load_user_model_configs()]


@router.post("/ai/models/user")
async def add_user_model(model: AIModelConfig):
    """添加用户自定义 AI 模型"""
    configs = load_user_model_configs()
    data = model.model_dump()
    # 检查是否已存在
    for i, config in enumerate(configs):
        if config.get("id") == model.id:
            config = await _fill_context_window(_load_and_merge_model_config(model.id, data))
            configs[i] = config
            save_user_model_configs(configs)
            await AIAdapterFactory.invalidate(model.id)
            return {"success": True, "message": "模型已更新", "config": sanitize_model_config(config)}

    data = await _fill_context_window(data)
    configs.append(data)
    save_user_model_configs(configs)
    await AIAdapterFactory.invalidate(model.id)
    return {"success": True, "message": "模型已添加", "config": sanitize_model_config(data)}


@router.delete("/ai/models/user/{model_id}")
async def delete_user_model(model_id: str):
    """删除用户自定义 AI 模型"""
    configs = load_user_model_configs()
    configs = [c for c in configs if c.get("id") != model_id]
    save_user_model_configs(configs)
    await AIAdapterFactory.invalidate(model_id)
    return {"success": True, "message": "模型已删除"}


# ===== AI Model Configs (frontend compatible REST API) =====
@router.get("/ai/model-configs", response_model=list)
async def get_model_configs():
    """获取所有 AI 模型配置（供前端设置页面使用，API Key 已脱敏）"""
    return [sanitize_model_config(c) for c in get_all_model_configs()]


@router.post("/ai/model-configs")
async def add_model_config(model: AIModelConfig):
    """添加或更新 AI 模型配置"""
    configs = load_user_model_configs()
    data = model.model_dump()
    for i, config in enumerate(configs):
        if config.get("id") == model.id:
            config = await _fill_context_window(_load_and_merge_model_config(model.id, data))
            configs[i] = config
            save_user_model_configs(configs)
            await AIAdapterFactory.invalidate(model.id)
            return {"success": True, "message": "模型配置已更新", "config": sanitize_model_config(config)}
    data = await _fill_context_window(data)
    configs.append(data)
    save_user_model_configs(configs)
    await AIAdapterFactory.invalidate(model.id)
    return {"success": True, "message": "模型配置已添加", "config": sanitize_model_config(data)}


@router.put("/ai/model-configs/{model_id}")
async def update_model_config(model_id: str, model: AIModelConfig):
    """更新 AI 模型配置"""
    configs = load_user_model_configs()
    for i, config in enumerate(configs):
        if config.get("id") == model_id:
            data = await _fill_context_window(_load_and_merge_model_config(model_id, model.model_dump()))
            configs[i] = data
            save_user_model_configs(configs)
            await AIAdapterFactory.invalidate(model_id)
            return {"success": True, "message": "模型配置已更新", "config": sanitize_model_config(data)}
    raise HTTPException(status_code=404, detail="Model config not found")


@router.delete("/ai/model-configs/{model_id}")
async def delete_model_config(model_id: str):
    """删除 AI 模型配置"""
    configs = load_user_model_configs()
    configs = [c for c in configs if c.get("id") != model_id]
    save_user_model_configs(configs)
    await AIAdapterFactory.invalidate(model_id)
    return {"success": True, "message": "模型配置已删除"}


@router.post("/ai/model-configs/{model_id}/test")
async def test_model_config(model_id: str):
    """测试 AI 模型配置是否可用"""
    try:
        config = AIAdapterFactory.get_model_config(model_id)
        if not config:
            raise HTTPException(status_code=404, detail="Model config not found")

        adapter = AIAdapterFactory.create(model_id)
        response = await asyncio.wait_for(adapter.chat([
            AIChatMessage(role="user", content="Hello, please reply with a short greeting.")
        ], max_tokens=32, temperature=0, max_retries=0), timeout=HEALTH_CHECK_TIMEOUT_SECONDS)
        return {"success": True, "message": "连接成功", "response": response}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接失败: {type(e).__name__}")


# ===== Test Plans =====
@router.get("/templates", response_model=list)
async def get_templates():
    """获取测试模板列表"""
    return TEST_TEMPLATES


@router.post("/test-plans/generate", response_model=TestPlanInfo)
@router.post("/projects/{project_id}/test-plans/generate", response_model=TestPlanInfo)
async def generate_test_plan(request: GenerateTestPlanRequest, project_id: str = DEFAULT_PROJECT_ID):
    """生成测试计划（支持指定的项目ID或路径参数）"""
    try:
        # Ensure project exists
        if project_id not in store.projects:
            await ensure_default_project()

        generator = JMXGenerator(model_type=request.ai_model)

        # Determine base_url
        base_url = request.base_url
        if project_id in store.projects:
            proj_env = store.projects[project_id].environment
            base_url = base_url or proj_env.base_url

        # Use AI to generate if endpoints are provided or for complex scenarios
        if request.endpoints and len(request.endpoints) > 0 and base_url:
            # Use basic generator for simple cases
            jmx_content = generator.generate_basic_jmx(
                endpoints=request.endpoints,
                test_config=request.test_config,
                base_url=base_url,
                test_name=f"{request.test_config.test_type.value}_test"
            )
        else:
            # Use AI for complex generation
            jmx_content = await generator.generate_from_ai(
                api_doc=request.api_doc,
                endpoints=request.endpoints,
                test_config=request.test_config,
                base_url=base_url
            )

        # Nothing is saved until the plan can actually run.
        failures = jmx_lint.errors(jmx_lint.lint(jmx_content))
        loaded = await asyncio.to_thread(jmx_lint.load_check, jmx_content) if not failures else None
        if failures or (loaded and loaded['checked'] and not loaded['ok']):
            raise HTTPException(status_code=422, detail=jmx_lint.describe(failures) or f"JMeter 无法加载：{loaded['error']}")

        # Save JMX under project directory
        plan_id = str(uuid.uuid4())[:8]
        plan_name = f"{request.test_config.test_type.value}_test_{plan_id}"
        jmx_path = generator.save_jmx(jmx_content, name=plan_name, project_id=project_id, plan_id=plan_id)

        info = TestPlanInfo(
            id=plan_id,
            project_id=project_id,
            name=plan_name,
            description=f"AI生成的{request.test_config.test_type.value}测试计划",
            jmx_path=jmx_path,
            created_at=datetime.now(),
            test_config=request.test_config,
            endpoints_count=len(request.endpoints) if request.endpoints else 0
        )

        await _persist_test_plan(info)
        return info
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/test-plans", response_model=dict)
async def list_test_plans(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """列出所有测试计划，支持分页"""
    plans = list(store.test_plans.values())
    total = len(plans)
    return {"test_plans": plans[offset:offset + limit], "total": total, "offset": offset, "limit": limit}


@router.get("/projects/{project_id}/test-plans", response_model=list)
async def list_project_test_plans(project_id: str):
    """列出指定项目下的测试计划"""
    await ensure_default_project()
    return [p for p in store.test_plans.values() if p.project_id == project_id]


@router.get("/test-plans/{plan_id}", response_model=TestPlanInfo)
async def get_test_plan(plan_id: str):
    """获取测试计划详情"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")
    return store.test_plans[plan_id]


@router.delete("/test-plans/{plan_id}")
async def delete_test_plan(plan_id: str):
    """删除测试计划"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")
    
    plan = store.test_plans[plan_id]
    await _delete_test_plan_from_db(plan_id)
    # Delete JMX file
    if os.path.exists(plan.jmx_path):
        os.remove(plan.jmx_path)
    
    return {"success": True}


@router.get("/test-plans/{plan_id}/jmx/tree")
async def get_jmx_tree(plan_id: str):
    """获取 JMX 文件的树形结构"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")

    plan = store.test_plans[plan_id]
    if not os.path.exists(plan.jmx_path):
        raise HTTPException(status_code=404, detail="JMX file not found")

    try:
        tree = parse_jmx(plan.jmx_path)
        return tree
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse JMX: {str(e)}")


@router.put("/test-plans/{plan_id}/tree")
async def update_jmx_tree(plan_id: str, request: UpdateJMXTreeRequest):
    """更新 JMX 文件的树形结构"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")

    plan = store.test_plans[plan_id]
    temp_path = plan.jmx_path + ".tmp"

    try:
        jmx_content = build_jmx(request.tree.model_dump() if hasattr(request.tree, "model_dump") else request.tree)

        # Write beside the plan, then swap in one step: a failed save leaves the old file untouched.
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(jmx_content)
        os.replace(temp_path, plan.jmx_path)

        if request.name and request.name != plan.name:
            await _persist_test_plan(plan.model_copy(update={"name": request.name}))

        return {"success": True, "message": "JMX updated successfully"}
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=500, detail=f"Failed to update JMX: {str(e)}")


@router.get("/test-plans/{plan_id}/parse")
async def parse_test_plan(plan_id: str):
    """解析测试计划 JMX 为树结构（兼容前端）"""
    return await get_jmx_tree(plan_id)


@router.get("/test-plans/{plan_id}/jmx/content")
async def get_jmx_content(plan_id: str):
    """获取 JMX 文件的原始内容"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")
    
    plan = store.test_plans[plan_id]
    if not os.path.exists(plan.jmx_path):
        raise HTTPException(status_code=404, detail="JMX file not found")
    
    with open(plan.jmx_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    return {"content": content}


@router.get("/test-plans/{plan_id}/download")
async def download_test_plan(plan_id: str):
    """下载测试计划 JMX 文件"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")
    
    plan = store.test_plans[plan_id]
    if not os.path.exists(plan.jmx_path):
        raise HTTPException(status_code=404, detail="JMX file not found")
    
    return FileResponse(
        plan.jmx_path,
        media_type="application/xml",
        filename=os.path.basename(plan.jmx_path)
    )


# ===== Test Execution =====
@router.post("/test-plans/{plan_id}/execute", response_model=TestExecutionInfo)
async def execute_test_plan(
    plan_id: str,
    background_tasks: BackgroundTasks,
    ai_model: Optional[str] = Query(None, description="AI model for analysis"),
    approved_sha256: Optional[str] = Query(None, description="The plan content the user approved to run on this machine"),
):
    """执行测试计划"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")
    
    plan = store.test_plans[plan_id]
    # The user chose to run it now, so a busy machine is recorded for the report, not waited on.
    before_execute, postprocess = resource_hooks(False, {}, wait=False)
    try:
        return await execution_lifecycle.submit(
            plan.jmx_path, test_plan_id=plan_id, approved_sha256=approved_sha256,
            before_execute=before_execute, postprocess=postprocess, ai_model=ai_model)
    except ScriptApprovalRequired as error:
        # The UI lists these and asks before retrying with this exact content's hash.
        raise HTTPException(status_code=409, detail={
            "code": "scripts_need_approval", "message": str(error), "findings": error.findings,
            "sha256": error.sha256}) from error
    except (ValueError, FileExistsError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/executions", response_model=dict)
async def list_executions(
    project_id: Optional[str] = Query(None, description="按项目过滤"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """列出执行记录（新的在前），每行附带计划名、需求判定与 AI 分析状态，供执行列表直接展示。"""
    from app.services import requirements as requirements_service
    execs = [e for e in store.executions.values() if not project_id or e.project_id == project_id]
    execs.sort(key=lambda e: e.start_time or datetime.min, reverse=True)
    page = execs[offset:offset + limit]
    project_requirements = {}
    for pid in {e.project_id for e in page}:
        try:
            project_requirements[pid] = await requirements_service.get_requirements(pid)
        except Exception:
            logger.warning("Failed to load requirements for project %s", pid, exc_info=True)
            project_requirements[pid] = None
    rows = []
    for e in page:
        plan = store.test_plans.get(e.test_plan_id)
        project = store.projects.get(e.project_id)
        summary = e.summary
        verdict = None
        if summary and summary.total_requests:
            evaluation = requirements_service.evaluate(project_requirements.get(e.project_id), summary,
                                                       requirements_service.sampler_routes(e.jmx_path))
            verdict = {"verdict": evaluation.verdict, "passed": evaluation.passed,
                       "failed": evaluation.failed, "missing": evaluation.missing}
        rows.append({
            "id": e.id,
            "project_id": e.project_id,
            "project_name": project.name if project else "",
            "test_plan_id": e.test_plan_id,
            "test_plan_name": plan.name if plan else "",
            "status": e.status.value if hasattr(e.status, 'value') else str(e.status),
            "start_time": e.start_time.isoformat() if e.start_time else None,
            "end_time": e.end_time.isoformat() if e.end_time else None,
            "progress": e.progress,
            "error_message": e.error_message,
            "metrics": {
                "total_requests": summary.total_requests,
                "avg_response_time": summary.avg_response_time,
                "p95_response_time": summary.p95_response_time,
                "error_rate": summary.error_rate,
                "throughput": summary.throughput,
            } if summary else None,
            "requirements": verdict,
            "has_ai_analysis": bool(e.ai_analysis),
            "has_markdown": bool(e.report_dir and os.path.isfile(os.path.join(e.report_dir, "ai_analysis.md"))),
        })
    return {"executions": rows, "total": len(execs), "offset": offset, "limit": limit}


@router.get("/projects/{project_id}/executions", response_model=list)
async def list_project_executions(project_id: str):
    """列出指定项目下的执行记录"""
    await ensure_default_project()
    return [e for e in store.executions.values() if e.project_id == project_id]


@router.get("/executions/{execution_id}", response_model=TestExecutionInfo)
async def get_execution(execution_id: str):
    """获取执行状态"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    return store.executions[execution_id]


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str):
    """取消测试执行"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")

    success = await execution_lifecycle.cancel(execution_id)
    return {"success": success, "message": "" if success else "Execution already ended or could not be stopped"}


@router.get("/executions/{execution_id}/report", response_model=TestReport)
async def get_report(execution_id: str):
    """获取测试报告"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    execution = store.executions[execution_id]
    # Reports are available for completed, failed, and cancelled executions
    # so users can inspect partial results and logs.
    if execution.status not in (
        TestExecutionStatus.COMPLETED,
        TestExecutionStatus.FAILED,
        TestExecutionStatus.CANCELLED,
    ):
        raise HTTPException(status_code=400, detail="Test not completed yet")
    
    # Parse JTL file — fall back to cached summary on parse failure
    jtl_path = os.path.join(execution.report_dir, "result.jtl") if execution.report_dir else None
    summary = getattr(execution, "summary", None)
    if summary is None and jtl_path and os.path.exists(jtl_path):
        try:
            summary = await asyncio.to_thread(_report_parser.parse_jtl, jtl_path)
        except Exception as e:
            logger.warning("Report JTL re-parse failed for %s: %s", execution_id, e)
    if summary is None:
        summary = _report_parser._empty_summary()

    # Get HTML report path
    html_path = _report_parser.get_html_report_url(execution.report_dir)

    # SLA evaluation (best-effort)
    sla_result = None
    if summary and summary.total_requests > 0:
        try:
            sla_config = SLAConfig.standard_api_sla()
            sla_engine = SLAEngine()
            sla_result = asdict(sla_engine.evaluate(summary, sla_config))
        except Exception as e:
            logger.warning("SLA evaluation failed for %s: %s", execution_id, e)

    plan = store.test_plans.get(execution.test_plan_id)

    failure_diagnosis = None
    if (execution.status == TestExecutionStatus.FAILED or summary.total_requests == 0
            or (execution.status == TestExecutionStatus.CANCELLED and execution.error_message)):
        log_text = await asyncio.to_thread(_report_parser.read_log, execution.log_path, 200) if execution.log_path else ''
        failure_diagnosis = run_diagnostics.diagnose_failure(execution.error_message, log_text)

    evidence = None
    if summary.total_requests > 0:
        from app.services.evidence import build_evidence
        try:
            evidence = (await build_evidence(execution, summary)).model_dump()
        except Exception as e:
            logger.warning("Evidence assembly failed for %s: %s", execution_id, e)

    report = TestReport(
        execution_id=execution_id,
        project_id=execution.project_id or "default",
        test_plan_id=execution.test_plan_id,
        test_plan_name=plan.name if plan else "Unknown",
        start_time=execution.start_time,
        end_time=execution.end_time or datetime.now(),
        duration_seconds=(execution.end_time - execution.start_time).total_seconds() if execution.end_time and execution.start_time else 0,
        summary=summary,
        raw_data_path=jtl_path,
        html_report_path=html_path,
        ai_analysis=getattr(execution, 'ai_analysis', None),
        sla_result=sla_result,
        evidence=evidence,
        failure_diagnosis=failure_diagnosis,
        status=execution.status,
        report_status=execution.report_status,
        report_error=execution.report_error,
        analysis_enabled=settings.AUTO_ANALYSIS_ENABLED,
    )

    return report


@router.post("/executions/{execution_id}/analysis", status_code=202)
async def reanalyze_execution(execution_id: str, request: Optional[ReanalyzeRequest] = None):
    """重新生成这次运行的 AI 分析；后台进行，报告页轮询 report_status"""
    try:
        execution = await post_run.start_reanalysis(execution_id, request.model if request else None)
    except LookupError:
        raise HTTPException(status_code=404, detail="Execution not found") from None
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {'report_status': execution.report_status}


@router.post("/executions/{execution_id}/questions")
async def ask_about_execution(execution_id: str, request: RunQuestion):
    """追问一次已有分析的运行；问答跟着这份分析保存"""
    try:
        evidence = await AnalysisAgent.resolve_execution(execution_id)
    except AnalysisExecutionError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    execution = store.get_execution(execution_id)
    if execution is None or execution.ai_analysis is None:
        raise HTTPException(status_code=409, detail="This run has no analysis to ask about")
    if execution.report_status == 'running':
        raise HTTPException(status_code=409, detail="The analysis is being written; ask when it is done")
    earlier = execution.ai_analysis.follow_ups[-MAX_FOLLOW_UP_CONTEXT:]
    context = json.dumps({'report': execution.ai_analysis.model_dump(exclude={'follow_ups', 'fact_check'}),
                          'earlier_questions': earlier}, ensure_ascii=False, default=str)
    try:
        agent = AnalysisAgent(AIAdapterFactory.create(request.model))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    result = await agent.run_analysis(request.question, evidence, context=context)
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "The model did not answer")
    validation = (result.analysis or {}).get('validation') or {}
    answer = {'question': request.question, 'answer': result.output, 'asked_at': datetime.now().isoformat(),
              'mismatches': validation.get('mismatches') or []}
    async with _question_locks.setdefault(execution_id, asyncio.Lock()):
        # Read again: the run may have been deleted or re-analysed while the model was answering.
        current = store.get_execution(execution_id)
        if current is None or current.ai_analysis is None or current.report_status == 'running':
            raise HTTPException(status_code=409, detail="The run or its analysis changed while answering; ask again")
        execution = current.model_copy(deep=True)
        execution.ai_analysis.follow_ups.append(answer)
        await persist_execution(execution)
    save_analysis_markdown(execution.report_dir, execution_id, execution.ai_analysis)
    return answer


@router.get("/executions/{execution_id}/log")
async def get_execution_log(execution_id: str, lines: int = 100):
    """获取执行日志"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    execution = store.executions[execution_id]
    if not execution.log_path or not os.path.exists(execution.log_path):
        return {"log": "No log available"}
    
    log_content = _report_parser.read_log(execution.log_path, lines)
    return {"log": log_content}


@router.get("/executions/{execution_id}/html-report")
async def get_html_report(execution_id: str):
    """获取HTML报告"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    execution = store.executions[execution_id]
    html_path = _report_parser.get_html_report_url(execution.report_dir)
    
    if not html_path or not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="HTML report not found")
    
    return FileResponse(html_path, headers={'Content-Security-Policy': REPORT_POLICY})


@router.get("/executions/{execution_id}/dashboard-data")
async def get_dashboard_data(execution_id: str):
    """获取JMeter仪表盘图表数据(statistics.json)及AI洞察"""
    execution = store.executions.get(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if not execution.report_dir:
        raise HTTPException(status_code=404, detail="Report directory not available")
    result = _report_parser.parse_dashboard_json(execution.report_dir)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ===== Project CRUD =====
@router.post("/projects", response_model=ProjectInfo)
async def create_project(request: CreateProjectRequest):
    """创建新项目"""
    project_id = str(uuid.uuid4())[:8]
    now = datetime.now()
    environment = request.environment or ProjectEnvironment()
    environments = request.environments

    # If new multi-env format not provided, auto-create default env from legacy config
    if not environments:
        default_env = EnvironmentConfig(
            id="env_default",
            name="默认环境",
            base_url=environment.base_url,
            variables=environment.variables,
            headers=environment.headers
        )
        environments = ProjectEnvironments(
            environments=[default_env],
            active_id="env_default"
        )

    project = ProjectInfo(
        id=project_id,
        name=request.name,
        description=request.description,
        environment=environment,
        environments=environments,
        created_at=now,
        updated_at=now
    )
    await persist_project(project, create=True)

    # Ensure project directory exists
    project_dir = os.path.join(settings.TEST_PLANS_DIR, project_id)
    os.makedirs(project_dir, exist_ok=True)

    return project


@router.get("/projects", response_model=dict)
async def list_projects(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """列出所有项目，支持分页"""
    await ensure_default_project()
    projects = list(store.projects.values())
    # Add test plan count to each project
    all_result = []
    for p in projects:
        plan_count = len([tp for tp in store.test_plans.values() if tp.project_id == p.id])
        all_result.append({
            **p.model_dump(),
            "test_plan_count": plan_count,
            "execution_count": len([e for e in store.executions.values() if e.project_id == p.id])
        })
    total = len(all_result)
    return {"projects": all_result[offset:offset + limit], "total": total, "offset": offset, "limit": limit}


@router.get("/projects/{project_id}", response_model=ProjectInfo)
async def get_project(project_id: str):
    """获取项目详情"""
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    project = store.projects[project_id]
    plan_count = len([tp for tp in store.test_plans.values() if tp.project_id == project_id])
    return {
        **project.model_dump(),
        "test_plan_count": plan_count,
        "execution_count": len([e for e in store.executions.values() if e.project_id == project_id])
    }


@router.put("/projects/{project_id}", response_model=ProjectInfo)
async def update_project(project_id: str, request: UpdateProjectRequest):
    """更新项目"""
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")

    project = store.projects[project_id]
    if request.name is not None:
        project.name = request.name
    if request.description is not None:
        project.description = request.description
    if request.environment is not None:
        project.environment = request.environment
    if request.environments is not None:
        project.environments = request.environments
    project.updated_at = datetime.now()
    await _persist_project(project)
    return project


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目及其所有测试计划和执行记录"""
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(status_code=400, detail="Cannot delete default project")
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")

    plans = [p for p in store.test_plans.values() if p.project_id == project_id]
    executions = [e for e in store.executions.values() if e.project_id == project_id]
    if any(e.status in (TestExecutionStatus.PENDING, TestExecutionStatus.RUNNING) for e in executions):
        raise HTTPException(409, 'Stop active executions before deleting their project')
    project_dir = os.path.join(settings.TEST_PLANS_DIR, project_id)
    await db_store.delete_project(project_id)
    for plan in plans:
        store.test_plans.pop(plan.id, None)
    for execution in executions:
        store.executions.pop(execution.id, None)
    store.projects.pop(project_id, None)
    for plan in plans:
        if os.path.isfile(plan.jmx_path):
            os.remove(plan.jmx_path)
    if os.path.exists(project_dir):
        shutil.rmtree(project_dir, ignore_errors=True)
    return {"success": True}


# ===== Environment CRUD =====
@router.get("/projects/{project_id}/environments", response_model=ProjectEnvironments)
async def list_environments(project_id: str):
    """获取项目所有环境配置"""
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    project = store.projects[project_id]
    return project.environments or ProjectEnvironments()


@router.post("/projects/{project_id}/environments", response_model=EnvironmentConfig)
async def create_environment(project_id: str, request: CreateEnvironmentRequest):
    """创建新环境"""
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    project = store.projects[project_id]
    if not project.environments:
        project.environments = ProjectEnvironments()

    env_id = str(uuid.uuid4())[:8]
    new_env = EnvironmentConfig(
        id=env_id,
        name=request.name,
        base_url=request.base_url,
        variables=request.variables,
        headers=request.headers
    )
    project.environments.environments.append(new_env)
    if not project.environments.active_id:
        project.environments.active_id = env_id
    project.updated_at = datetime.now()
    await _persist_project(project)
    return new_env


@router.put("/projects/{project_id}/environments/{env_id}", response_model=EnvironmentConfig)
async def update_environment(project_id: str, env_id: str, request: UpdateEnvironmentRequest):
    """更新环境配置"""
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    project = store.projects[project_id]
    if not project.environments:
        raise HTTPException(status_code=404, detail="No environments found")

    env = next((e for e in project.environments.environments if e.id == env_id), None)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")

    if request.name is not None:
        env.name = request.name
    if request.base_url is not None:
        env.base_url = request.base_url
    if request.variables is not None:
        env.variables = request.variables
    if request.headers is not None:
        env.headers = request.headers
    project.updated_at = datetime.now()
    await _persist_project(project)
    return env


@router.delete("/projects/{project_id}/environments/{env_id}")
async def delete_environment(project_id: str, env_id: str):
    """删除环境配置"""
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    project = store.projects[project_id]
    if not project.environments:
        raise HTTPException(status_code=404, detail="No environments found")

    env = next((e for e in project.environments.environments if e.id == env_id), None)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")

    project.environments.environments = [e for e in project.environments.environments if e.id != env_id]
    if project.environments.active_id == env_id:
        project.environments.active_id = project.environments.environments[0].id if project.environments.environments else None
    project.updated_at = datetime.now()
    await _persist_project(project)
    return {"success": True}


@router.put("/projects/{project_id}/environments/active/{env_id}")
async def set_active_environment(project_id: str, env_id: str):
    """设置当前激活的环境"""
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    project = store.projects[project_id]
    if not project.environments:
        raise HTTPException(status_code=404, detail="No environments found")

    env = next((e for e in project.environments.environments if e.id == env_id), None)
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")

    project.environments.active_id = env_id
    project.updated_at = datetime.now()
    await _persist_project(project)
    return {"success": True, "active_id": env_id}


# ===== AI Workflow =====
@router.post("/ai/workflow/stream")
async def ai_workflow_stream(request: AIWorkflowRequest):
    """AI 工作流 SSE 接口 —— 从API文档自动创建项目和测试用例。

    Delegates to WorkflowAgent which handles all tool calling, error retry,
    and resource checks via the unified agent framework.
    """
    from app.services.ai.agents.workflow_agent import WorkflowAgent

    lang = request.lang or "zh-CN"
    is_en = lang == "en"

    async def generate():
        try:
            adapter = AIAdapterFactory.get_adapter(request.ai_model)
            agent = WorkflowAgent(adapter)

            # ── Step 1: Analyzing ─────────────────────────
            msg = "Analyzing API documentation..." if is_en else "正在分析API文档..."
            yield f"data: {json.dumps({'step': 'analyzing_doc', 'message': msg, 'progress': 10})}\n\n"

            # Forward each tool as it starts; generation takes minutes and a static
            # "analyzing" line looked stuck. Unchanged step keys update one progress line.
            result = None
            async for kind, payload in agent.stream_from_doc(
                api_documentation=request.api_documentation,
                testing_requirements=request.testing_requirements,
                test_types=None,
            ):
                if kind == "result":
                    result = payload
                elif payload.type == "tool_call" and (payload.data or {}).get("tool") in WORKFLOW_PROGRESS:
                    tool = payload.data["tool"]
                    progress, zh, en = WORKFLOW_PROGRESS[tool]
                    yield f"data: {json.dumps({'step': f'agent_{tool}', 'message': en if is_en else zh, 'progress': progress}, ensure_ascii=False)}\n\n"

            steps_done = bool(result.get("plan_steps")) and all(step["success"] for step in result["plan_steps"])
            if not result["success"] and steps_done and result.get("project_id"):
                # Every step finished and the project exists; only the closing summary failed.
                # Reporting failure here made people retry and create a duplicate project.
                note = ("All steps finished, but the closing summary could not be written: "
                        if is_en else "所有步骤都已完成，只是最后的总结没能生成：")
                result["diagnostic_summary"] = note + str(result.get("error") or "")
            elif not result["success"]:
                err_msg = result.get("error", "Unknown error")
                yield f"data: {json.dumps({'step': 'error', 'message': err_msg, 'progress': 100, 'error': True}, ensure_ascii=False)}\n\n"
                return

            pid = result.get("project_id", "")
            plan_ids = result.get("test_plan_ids", [])

            # Always draft requirements from the same text, for review; never saved here.
            requirements_draft = None
            if pid:
                from app.services import requirements as requirement_service
                try:
                    already_saved = await requirement_service.get_requirements(pid)
                    if already_saved is None or not already_saved.items:
                        msg = "Drafting performance requirements..." if is_en else "正在起草性能需求..."
                        yield f"data: {json.dumps({'step': 'drafting_requirements', 'message': msg, 'progress': 92}, ensure_ascii=False)}\n\n"
                        source = f"{request.testing_requirements}\n\n{request.api_documentation}".strip()
                        draft = await requirement_service.extract_requirements(
                            source, adapter, "start-from-document")
                        requirements_draft = draft.model_dump()
                except Exception as error:
                    logger.warning("Requirement draft failed for project %s: %s", pid, error)

            # ── Complete ──────────────────────────────────
            plan_count = len(plan_ids)
            complete_msg = (
                f"Workflow complete! Created project with {plan_count} test plans"
                if is_en else f"工作流完成！已创建项目，共 {plan_count} 个测试计划"
            )
            complete_event = {
                'step': 'complete',
                'message': complete_msg,
                'progress': 100,
                'project_id': pid,
                'test_plan_ids': plan_ids,
                'diagnostic_summary': result.get('diagnostic_summary', ''),
                'requirements_draft': requirements_draft,
            }
            yield f"data: {json.dumps(complete_event, ensure_ascii=False)}\n\n"

        except Exception as e:
            err_msg = f"Workflow failed: {str(e)}" if is_en else f"工作流执行失败: {str(e)}"
            yield f"data: {json.dumps({'step': 'error', 'message': err_msg, 'progress': 100, 'error': True}, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ===== OpenAPI/Swagger Import =====

@router.post("/openapi/import")
async def openapi_import(request: dict):
    """Import an OpenAPI/Swagger spec from URL or JSON body.

    Accepts:
    - {"url": "https://petstore.swagger.io/v2/swagger.json"} — fetches spec from URL
    - {"spec": {...}} — parses spec dict directly
    - {"url": "...", "project_name": "...", "execute": true/false}

    Returns ImportResult with project_id, endpoints_found, auth_detected, jmx_paths.
    """
    url = request.get("url", "")
    spec_dict = request.get("spec")
    project_name = request.get("project_name", "")
    execute = request.get("execute", False)
    if not isinstance(execute, bool):
        raise HTTPException(status_code=422, detail="execute must be a JSON boolean")

    importer = OpenAPIImporter()

    if spec_dict:
        result = await importer.import_from_dict(spec_dict, source_url=url, execute_tests=execute)
    elif url:
        result = await importer.import_from_url(url, execute_tests=execute)
    else:
        raise HTTPException(status_code=400, detail="Either 'url' or 'spec' is required")

    if project_name and result.success:
        p = store.get_project(result.project_id)
        if p:
            p = p.model_copy(deep=True)
            p.name = project_name
            await persist_project(p, create=False)
            result.project_name = project_name

    return result.model_dump() if not result.success else {
        **result.model_dump(),
        "test_plans": [{"name": p.name, "jmx_path": p.jmx_path, "id": p.id}
                       for p in store.test_plans.values()
                       if getattr(p, 'project_id', '') == result.project_id],
    }


@router.get("/analysis/reports")
async def list_analysis_reports(
    project_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """列出所有AI分析报告（有完成分析的执行记录）。

    返回每个报告的摘要：执行ID、项目名、测试计划名、关键发现前3条等。
    """
    execs = list(store.executions.values())
    reports = []
    for e in execs:
        if not hasattr(e, 'ai_analysis') or not e.ai_analysis:
            continue
        if project_id and getattr(e, 'project_id', '') != project_id:
            continue

        plan_name = ""
        if e.test_plan_id and e.test_plan_id in store.test_plans:
            plan_name = store.test_plans[e.test_plan_id].name

        proj_name = ""
        pid = getattr(e, 'project_id', '')
        if pid in store.projects:
            proj_name = store.projects[pid].name

        analysis = e.ai_analysis
        reports.append({
            "execution_id": e.id,
            "project_id": pid,
            "project_name": proj_name,
            "test_plan_name": plan_name,
            "status": e.status.value if hasattr(e.status, 'value') else str(e.status),
            "start_time": e.start_time.isoformat() if e.start_time else None,
            "end_time": e.end_time.isoformat() if e.end_time else None,
            "overall_assessment": getattr(analysis, 'overall_assessment', '')[:200],
            "key_findings": getattr(analysis, 'key_findings', [])[:3],
            "recommendations": getattr(analysis, 'recommendations', [])[:3],
            "has_markdown": bool(e.report_dir and os.path.isfile(os.path.join(e.report_dir, "ai_analysis.md"))),
        })
    reports.sort(key=lambda r: r.get('start_time') or '', reverse=True)
    return {"reports": reports[:limit], "total": len(reports)}


@router.get("/analysis/reports/{execution_id}")
async def get_analysis_report(execution_id: str):
    """获取单个AI分析报告详情"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    e = store.executions[execution_id]
    if not hasattr(e, 'ai_analysis') or not e.ai_analysis:
        raise HTTPException(status_code=404, detail="AI analysis not available for this execution")

    analysis = e.ai_analysis
    return {
        "execution_id": e.id,
        "project_id": getattr(e, 'project_id', ''),
        "test_plan_id": e.test_plan_id,
        "status": e.status.value if hasattr(e.status, 'value') else str(e.status),
        "start_time": e.start_time.isoformat() if e.start_time else None,
        "end_time": e.end_time.isoformat() if e.end_time else None,
        "ai_analysis": analysis.model_dump() if hasattr(analysis, 'model_dump') else analysis,
        "has_markdown": bool(e.report_dir and os.path.isfile(os.path.join(e.report_dir, "ai_analysis.md"))),
    }


@router.get("/analysis/reports/{execution_id}/download")
async def download_analysis_report(execution_id: str):
    """下载AI分析报告的Markdown文件"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    e = store.executions[execution_id]
    if not e.report_dir:
        raise HTTPException(status_code=404, detail="Report directory not found")

    md_path = os.path.join(e.report_dir, "ai_analysis.md")
    if not os.path.isfile(md_path):
        raise HTTPException(status_code=404, detail="AI analysis markdown not found")

    return FileResponse(
        md_path,
        media_type="text/markdown",
        filename=f"ai_analysis_{execution_id}.md"
    )


# ===== RAG Knowledge Base Management =====

from app.services.ai.rag.store import rag_store, RAGDocument, ALLOWED_DOC_TYPES
from app.services.ai.rag.retriever import RAGRetriever
from app.services.ai.rag.archiver import rag_archiver

rag_retriever = RAGRetriever()


@router.get("/rag/stats")
async def get_rag_stats():
    """获取RAG知识库统计信息"""
    return await asyncio.to_thread(rag_store.get_stats)


@router.get("/rag/documents")
async def list_rag_documents(
    doc_type: Optional[str] = Query(None, description="文档类型"),
    module: Optional[str] = Query(None, description="模块名称"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    limit: int = Query(50, ge=1, le=200, description="返回数量上限"),
):
    """列出RAG知识库文档，支持按类型/模块/关键词过滤"""
    if keyword or doc_type or module:
        docs = await asyncio.to_thread(rag_store.hybrid_search,
            doc_type=doc_type,
            module=module,
            query_text=keyword,
            limit=limit,
        )
    else:
        docs = await asyncio.to_thread(rag_store.list_all, limit=limit)

    return {
        "total": len(docs),
        "documents": [d.to_dict() for d in docs],
    }


@router.get("/rag/documents/{doc_id}")
async def get_rag_document(doc_id: str):
    """获取单个RAG文档"""
    doc = await asyncio.to_thread(rag_store.get, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc.to_dict()


@router.delete("/rag/documents/{doc_id}")
async def delete_rag_document(doc_id: str):
    """删除RAG文档"""
    ok = await asyncio.to_thread(rag_store.delete, doc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"success": True, "message": "Document deleted"}


@router.get("/rag/search")
async def search_rag(
    q: str = Query(..., description="搜索关键词"),
    module: Optional[str] = Query(None),
    doc_type: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """全文搜索RAG知识库"""
    keywords = [w.strip() for w in q.split() if w.strip()]
    docs = await asyncio.to_thread(rag_store.hybrid_search,
        keywords=keywords,
        module=module,
        doc_type=doc_type,
        limit=limit,
    )

    return {
        "query": q,
        "total": len(docs),
        "documents": [
            {
                "id": d.id,
                "doc_type": d.doc_type,
                "module": d.module,
                "version": d.version,
                "title": d.title,
                "keywords": d.keywords,
                "created_at": d.created_at,
                "snippet": d.content[:300] + ("..." if len(d.content) > 300 else ""),
            }
            for d in docs
        ],
    }


@router.get("/rag/modules")
async def list_rag_modules():
    """列出所有已存储的模块及其版本"""
    def load_modules():
        stats = rag_store.get_stats()
        modules = []
        for module_name in stats.get("by_module", {}):
            versions = rag_store.get_module_versions(module_name)
            doc_count = stats["by_module"].get(module_name, 0)
            latest = rag_store.get_latest_analysis(module_name)
            modules.append({
                "module": module_name,
                "document_count": doc_count,
                "versions": versions,
                "latest_analysis": latest.title if latest else None,
                "latest_analysis_date": latest.created_at[:10] if latest else None,
            })
        return modules
    modules = await asyncio.to_thread(load_modules)
    return {"modules": modules}


@router.get("/rag/module/{module_name}/history")
async def get_module_history(module_name: str):
    """获取模块的完整历史分析记录"""
    docs = await asyncio.to_thread(rag_store.search_by_module, module_name, limit=50)

    by_version = {}
    for doc in docs:
        v = doc.version
        if v not in by_version:
            by_version[v] = []
        by_version[v].append({
            "id": doc.id,
            "doc_type": doc.doc_type,
            "title": doc.title,
            "created_at": doc.created_at,
            "snippet": doc.content[:200],
        })

    return {
        "module": module_name,
        "versions": {v: docs for v, docs in sorted(by_version.items(), reverse=True)},
        "total_documents": len(docs),
    }


@router.get("/rag/compare")
async def compare_versions(
    module: str = Query(..., description="模块名称"),
    version_a: str = Query(..., description="版本A"),
    version_b: str = Query(..., description="版本B"),
):
    """对比两个版本的测试数据"""
    def load_versions():
        return (
            rag_store.hybrid_search(module=module, version=version_a, limit=20),
            rag_store.hybrid_search(module=module, version=version_b, limit=20),
        )
    docs_a, docs_b = await asyncio.to_thread(load_versions)

    return {
        "module": module,
        "version_a": {
            "version": version_a,
            "documents": [
                {"id": d.id, "type": d.doc_type, "title": d.title, "content": d.content[:500]}
                for d in docs_a
            ],
        },
        "version_b": {
            "version": version_b,
            "documents": [
                {"id": d.id, "type": d.doc_type, "title": d.title, "content": d.content[:500]}
                for d in docs_b
            ],
        },
    }


@router.post("/rag/archive/manual")
async def manual_archive(request: dict):
    """手动归档文档到RAG知识库"""
    doc_type = request.get("doc_type")
    if doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid doc_type. Allowed: {list(ALLOWED_DOC_TYPES)}"
        )

    doc = RAGDocument(
        id="",
        doc_type=doc_type,
        module=request.get("module", "unknown"),
        version=request.get("version", "unknown"),
        title=request.get("title", ""),
        content=request.get("content", ""),
        keywords=request.get("keywords", []),
        tags=request.get("tags", []),
        source_execution_id=request.get("source_execution_id"),
        source_plan_id=request.get("source_plan_id"),
        metadata=request.get("metadata", {}),
    )

    try:
        doc_id = await asyncio.to_thread(rag_store.add, doc)
        return {"success": True, "doc_id": doc_id, "message": "Document archived successfully"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/rag/allowed-types")
async def get_allowed_doc_types():
    """获取允许存储的文档类型列表"""
    return {
        "allowed_types": list(ALLOWED_DOC_TYPES),
        "description": {
            "raw_report": "原始测试报告数据",
            "analysis_conclusion": "正式分析结论",
            "defect_retrospective": "缺陷复盘总结",
            "known_issue": "高频问题经验",
            "module_standard": "模块测试标准/性能基准",
        }
    }


# ===== Local access =====

@router.post("/session")
async def create_session(request: dict, response: Response):
    """Trade the access token from the start link for a cookie that scripts on the page cannot read."""
    token = request.get("token")
    if not isinstance(token, str) or not local_access.token_matches(token):
        raise HTTPException(401, "Invalid access token")
    response.set_cookie(local_access.COOKIE_NAME, token, max_age=local_access.COOKIE_MAX_AGE,
                        httponly=True, samesite="strict", path="/")
    return {"accepted": True}


@router.get("/session/token")
async def session_token():
    """For the MCP setup page: already signed-in callers only, as this path is not exempt."""
    return {"token": local_access.get_token()}


# ===== Agent tool confirmations =====

@router.get("/confirmations")
async def pending_confirmations():
    from app.services.confirmations import confirmations
    return confirmations.list_pending()


@router.post("/confirmations/{confirmation_id}")
async def decide_confirmation(confirmation_id: str, decision: dict):
    from app.services.confirmations import confirmations
    approved = decision.get("approved")
    if not isinstance(approved, bool):
        raise HTTPException(422, "approved must be a JSON boolean")
    try:
        confirmations.decide(confirmation_id, approved)
    except KeyError:
        raise HTTPException(409, "Confirmation expired or already consumed")
    return {"accepted": True}


def _local_jmeter():
    """Folder and path of the JMeter this app will really run, which may differ from JMETER_HOME."""
    try:
        path = get_executor().jmeter_path
    except FileNotFoundError:
        return None, None
    path = shutil.which(path) or path  # found on PATH: only the bare name is known
    return os.path.dirname(path), path


@router.get("/system/info")
async def system_info():
    """获取系统信息"""
    import platform
    in_container = settings.JMETER_EXECUTION_MODE == 'container'
    jmeter_home, jmeter_path = (None, None) if in_container else _local_jmeter()

    return {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "execution_mode": settings.JMETER_EXECUTION_MODE,
        "jmeter_image": settings.JMETER_CONTAINER_IMAGE if in_container else None,
        "jmeter_home": jmeter_home,
        "jmeter_executable": jmeter_path,
        "test_plans_dir": settings.TEST_PLANS_DIR,
        "reports_dir": settings.REPORTS_DIR,
        "test_plans_dir_default": settings.TEST_PLANS_DIR_DEFAULT,
        "reports_dir_default": settings.REPORTS_DIR_DEFAULT,
    }


@router.get("/system/resources")
async def get_system_resources():
    """获取当前系统资源使用情况(CPU/内存)"""
    try:
        from app.services.jmeter.resource_monitor import get_resource_monitor
        monitor = get_resource_monitor()
        snap = await asyncio.to_thread(monitor.snapshot)
        return snap.to_dict()
    except Exception:
        import psutil
        cpu = await asyncio.to_thread(psutil.cpu_percent, interval=0.3)
        mem = psutil.virtual_memory()
        return {
            "cpu_percent": round(cpu, 1),
            "memory_percent": round(mem.percent, 1),
            "memory_used_gb": round(mem.used / (1024**3), 2),
            "memory_total_gb": round(mem.total / (1024**3), 2),
            "is_healthy": (cpu <= settings.RESOURCE_CPU_THRESHOLD
                           and mem.percent <= settings.RESOURCE_MEMORY_THRESHOLD),
        }


@router.get("/system/paths")
async def get_paths_config():
    """获取路径配置"""
    config = load_paths_config()
    return {
        "test_plans_dir": config.get("test_plans_dir", settings.TEST_PLANS_DIR_DEFAULT),
        "reports_dir": config.get("reports_dir", settings.REPORTS_DIR_DEFAULT),
        "test_plans_dir_default": settings.TEST_PLANS_DIR_DEFAULT,
        "reports_dir_default": settings.REPORTS_DIR_DEFAULT,
    }


@router.put("/system/paths")
async def update_paths_config(request: dict):
    """更新路径配置"""
    test_plans_dir = request.get("test_plans_dir")
    reports_dir = request.get("reports_dir")
    
    if not test_plans_dir or not reports_dir:
        raise HTTPException(status_code=400, detail="test_plans_dir 和 reports_dir 都是必填项")
    
    if not os.path.isabs(test_plans_dir):
        raise HTTPException(status_code=400, detail="test_plans_dir 必须是绝对路径")
    
    if not os.path.isabs(reports_dir):
        raise HTTPException(status_code=400, detail="reports_dir 必须是绝对路径")
    
    try:
        os.makedirs(test_plans_dir, exist_ok=True)
        os.makedirs(reports_dir, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"无法创建目录: {str(e)}")
    
    save_paths_config({
        "test_plans_dir": test_plans_dir,
        "reports_dir": reports_dir
    })
    
    return {"success": True, "message": "路径配置已更新"}


# ===== JMeter Component & Function Registry =====
@router.get("/jmeter/components")
async def get_components():
    """获取支持的 JMeter 组件列表"""
    return get_all_components()


@router.get("/jmeter/functions")
async def get_functions():
    """获取支持的 JMeter 函数列表"""
    return get_all_functions()


@router.get("/jmeter/components/{parent_testclass}/children")
async def get_component_children(parent_testclass: str):
    """获取某组件下允许添加的子组件"""
    return get_allowed_children(parent_testclass)
