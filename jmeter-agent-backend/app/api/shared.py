"""Helpers the API modules share: persistence, the default project and the answer to a run that needs approval."""
from datetime import datetime

from fastapi import HTTPException
from app.core.config import settings  # noqa: F401  (tests reach the settings through this module)
from app.services.db_store import db_store
from app.models.schemas import (
    TestPlanInfo, TestExecutionInfo, ProjectInfo, ProjectEnvironment,
)
from app.services.jmeter.report_parser import ReportParser
from app.services.jmeter.environment import ProductionRunNeedsConfirmation
from app.services.storage import store
from app.services.execution_lifecycle import execution_lifecycle, persist_project, persist_execution


# JMeter executor — obtained lazily via get_executor() at each call site so
# importing this module never probes the filesystem for the JMeter binary.
_report_parser = ReportParser()


DEFAULT_PROJECT_ID = "default"


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


def _approval_needed(error) -> HTTPException:
    """409 the UI answers by asking the user, then retrying with their approval."""
    if isinstance(error, ProductionRunNeedsConfirmation):
        return HTTPException(status_code=409, detail={
            "code": "production_needs_confirmation", "message": str(error), "environment": error.environment,
            "base_url": error.base_url, "threads": error.threads})
    # The UI lists these and asks before retrying with this exact content's hash.
    return HTTPException(status_code=409, detail={
        "code": "scripts_need_approval", "message": str(error), "findings": error.findings, "sha256": error.sha256})
