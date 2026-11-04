"""Test plans: templates, generation, the tree editor, downloads and trial runs."""
import os
import uuid
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from app.models.schemas import (
    GenerateTestPlanRequest, TestPlanInfo, TestTemplate, TestType, UpdateJMXTreeRequest,
)
from app.services.ai import usage as ai_usage
from app.services.ai.adapters.factory import AIAdapterFactory
from app.services.jmeter import jmx_lint
from app.services.jmeter.jmx_generator import JMXGenerator
from app.services.jmeter.jmx_parser import parse_jmx
from app.services.jmeter.jmx_builder import build_jmx
from app.services.jmeter.script_risk import ScriptApprovalRequired
from app.services.jmeter.environment import ProductionRunNeedsConfirmation
from app.services.storage import store
from app.services.execution_lifecycle import active_environment
from app.services import trial_run
from app.api.shared import DEFAULT_PROJECT_ID, _approval_needed, _delete_test_plan_from_db, _persist_test_plan, ensure_default_project

router = APIRouter()


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

        # Determine base_url: the request's, else the active environment's, else the project's old single one.
        base_url = request.base_url
        if project_id in store.projects:
            project = store.projects[project_id]
            _, active = active_environment(project)
            base_url = base_url or (active.base_url if active else '') or project.environment.base_url

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
    """解析测试计划 JMX 为树结构（编辑器读取）"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")
    plan = store.test_plans[plan_id]
    if not os.path.exists(plan.jmx_path):
        raise HTTPException(status_code=404, detail="JMX file not found")
    try:
        return parse_jmx(plan.jmx_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse JMX: {str(e)}")


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


def _plan(plan_id: str) -> TestPlanInfo:
    plan = store.test_plans.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Test plan not found")
    return plan


@router.post("/test-plans/{plan_id}/trial")
async def trial_run_plan(plan_id: str, approved_sha256: Optional[str] = Query(None),
                         confirm_production: bool = Query(False)):
    """试跑：一个用户、每个请求一次，返回每个请求的结果和响应开头；不记入运行历史"""
    plan = _plan(plan_id)
    try:
        return await trial_run.run(plan, approved_sha256, confirm_production)
    except (ProductionRunNeedsConfirmation, ScriptApprovalRequired) as error:
        raise _approval_needed(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/test-plans/{plan_id}/trial/diagnosis")
async def diagnose_trial(plan_id: str, lang: str = 'zh-CN', ai_model: Optional[str] = Query(None)):
    """让模型根据最近一次试跑的响应说明失败原因；提取路径建议会先用响应核对"""
    plan = _plan(plan_id)
    try:
        with ai_usage.metered() as used:
            diagnosis = await trial_run.diagnose(plan, AIAdapterFactory.get_adapter(ai_model), lang)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"诊断失败：{error}") from error
    return {**diagnosis, 'usage': used}


@router.post("/test-plans/{plan_id}/trial/fixes/{fix_id}")
async def apply_trial_fix(plan_id: str, fix_id: int):
    """把一条已被试跑响应证实的提取器修改写进计划"""
    plan = _plan(plan_id)
    try:
        return await asyncio.to_thread(trial_run.apply_fix, plan, fix_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
