"""Helpers the API modules share: persistence, startup recovery and the answer to a run that needs approval."""
import os
from datetime import datetime

from fastapi import HTTPException
from app.core.config import settings
from app.services.db_store import db_store
from app.models.schemas import (
    TestPlanInfo, TestExecutionInfo, TestExecutionStatus, ProjectInfo, ProjectEnvironment,
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
        if exec_id in store.executions or exec_id.startswith('trial-'):
            continue  # Already tracked, or a trial run's leftovers, which are never history.

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


def _approval_needed(error) -> HTTPException:
    """409 the UI answers by asking the user, then retrying with their approval."""
    if isinstance(error, ProductionRunNeedsConfirmation):
        return HTTPException(status_code=409, detail={
            "code": "production_needs_confirmation", "message": str(error), "environment": error.environment,
            "base_url": error.base_url, "threads": error.threads})
    # The UI lists these and asks before retrying with this exact content's hash.
    return HTTPException(status_code=409, detail={
        "code": "scripts_need_approval", "message": str(error), "findings": error.findings, "sha256": error.sha256})
