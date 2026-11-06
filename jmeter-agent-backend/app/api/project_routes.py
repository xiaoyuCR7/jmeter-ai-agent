"""Projects and their environments."""
import logging
import os
import shutil
import uuid
import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from app.core.config import settings
from app.services.db_store import db_store
from app.models.schemas import (
    TestExecutionStatus, ProjectInfo, ProjectEnvironment, ProjectEnvironments, EnvironmentConfig,
    SECRET_MASK, CreateProjectRequest, UpdateProjectRequest, CreateEnvironmentRequest,
    UpdateEnvironmentRequest,
)
from app.services.storage import store
from app.services.execution_lifecycle import default_environments, persist_project
from app.api.shared import DEFAULT_PROJECT_ID, _persist_project, ensure_default_project

logger = logging.getLogger(__name__)
router = APIRouter()


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
        environments = default_environments(environment)

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
    await _forget_project(project_id, executions)
    return {"success": True}


async def _forget_project(project_id: str, executions: list):
    """Reports and memory of a deleted project; the project is already gone, so a failure is only logged."""
    reports_root = os.path.realpath(settings.REPORTS_DIR)
    for execution in executions:
        if not execution.report_dir:
            continue
        report_dir = os.path.realpath(execution.report_dir)
        try:
            inside = report_dir != reports_root and os.path.commonpath([report_dir, reports_root]) == reports_root
        except ValueError:  # Another drive on Windows: certainly not ours.
            inside = False
        if inside:
            shutil.rmtree(report_dir, ignore_errors=True)
    from app.services.ai.memory import experience, fact_ledger, hypothesis_ledger
    execution_ids = [execution.id for execution in executions]
    for name, forget in (('facts', lambda: fact_ledger.forget_project(project_id, execution_ids)),
                         ('hypotheses', lambda: hypothesis_ledger.forget_project(project_id, execution_ids)),
                         ('lessons', lambda: asyncio.to_thread(experience.forget_project, project_id))):
        try:
            await forget()
        except Exception as error:
            logger.warning('Could not remove the %s of deleted project %s: %s', name, project_id, error)


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
    new_env = EnvironmentConfig(id=env_id, **request.model_dump(exclude={'copy_from'}))
    source = next((e for e in project.environments.environments if e.id == request.copy_from), None)
    if source is not None:
        # A copy made from the page only saw ******; its secrets come from the environment it copies.
        for field in ('variables', 'headers'):
            stored = getattr(source, field)
            setattr(new_env, field, {key: stored[key] if value == SECRET_MASK and key in stored else value
                                     for key, value in getattr(new_env, field).items()})
    new_env.secret_keys = _secret_keys(new_env)
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

    if request.secret_keys is not None:
        env.secret_keys = list(request.secret_keys)
    for field in ('variables', 'headers'):
        values = getattr(request, field)
        if values is not None:
            # The page only ever saw ****** for a secret: sending it back means "keep it".
            stored = getattr(env, field)
            setattr(env, field, {key: stored[key] if value == SECRET_MASK and key in stored else value
                                 for key, value in values.items()})
    for field in ('name', 'base_url', 'kind', 'max_threads'):
        if getattr(request, field) is not None:
            setattr(env, field, getattr(request, field))
    if request.clear_max_threads:
        env.max_threads = None
    if 'prometheus_url' in request.model_fields_set:
        env.prometheus_url = request.prometheus_url or None
    env.secret_keys = _secret_keys(env)
    project.updated_at = datetime.now()
    await _persist_project(project)
    return env


def _secret_keys(env: EnvironmentConfig) -> list[str]:
    """Only names the environment has; a secret whose value is still ****** was never given one."""
    names = set(env.variables) | set(env.headers)
    return sorted(key for key in set(env.secret_keys) if key in names)


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
