"""Shared in-memory cache of projects, test plans and executions.

Writes go to DBStore first, then here; reads come from here. It is loaded from the database at
startup. Used by both REST API routes and agent tools.
"""

import threading
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class SharedStore:
    """Thread-safe singleton cache synchronized with DBStore."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._projects: Dict[str, Any] = {}
        self._test_plans: Dict[str, Any] = {}
        self._executions: Dict[str, Any] = {}
        self._db_synced: bool = False
        self._cache_lock = threading.RLock()
        self._initialized = True

    @property
    def db_synced(self) -> bool:
        return self._db_synced

    # ── Projects ──────────────────────────────────────────

    @property
    def projects(self) -> Dict[str, Any]:
        return self._projects

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def set_project(self, project_id: str, project):
        self.projects[project_id] = project

    def delete_project(self, project_id: str) -> bool:
        if project_id in self.projects:
            del self.projects[project_id]
            return True
        return False

    def has_project(self, project_id: str) -> bool:
        return project_id in self.projects

    # ── Test Plans ────────────────────────────────────────

    @property
    def test_plans(self) -> Dict[str, Any]:
        return self._test_plans

    def get_test_plan(self, plan_id: str):
        return self.test_plans.get(plan_id)

    def set_test_plan(self, plan_id: str, plan):
        self.test_plans[plan_id] = plan

    def delete_test_plan(self, plan_id: str) -> bool:
        if plan_id in self.test_plans:
            del self.test_plans[plan_id]
            return True
        return False

    # ── Executions ────────────────────────────────────────

    @property
    def executions(self) -> Dict[str, Any]:
        return self._executions

    def get_execution(self, execution_id: str):
        return self.executions.get(execution_id)

    def set_execution(self, execution_id: str, execution):
        self.executions[execution_id] = execution

    # ── DB sync ───────────────────────────────────────────

    @staticmethod
    async def _all_pages(method, key):
        rows, offset, total = [], 0, None
        while total is None or offset < total:
            page = await method(offset=offset, limit=1000)
            batch = page.get(key, [])
            total = page.get('total')
            if not isinstance(total, int) or total < 0 or not isinstance(batch, list):
                raise RuntimeError('Business cache snapshot is malformed')
            if not batch and offset < total:
                raise RuntimeError('Business cache snapshot ended before its declared total')
            rows.extend(batch)
            offset += len(batch)
        return rows

    @staticmethod
    def _project_value(value):
        from app.models.schemas import ProjectInfo, ProjectEnvironment
        environment = value.get('environment_data') or {}
        return ProjectInfo(
            id=value['id'], name=value['name'], description=value.get('description', ''),
            environment=ProjectEnvironment(
                base_url=value.get('base_url', environment.get('base_url', '')),
                variables=environment.get('variables', {}), headers=environment.get('headers', {})),
            environments=value.get('environments_data'), created_at=value.get('created_at'),
            updated_at=value.get('updated_at'), storage_version=value.get('storage_version', 1))

    @staticmethod
    def _plan_value(value):
        from app.models.schemas import TestPlanInfo, TestConfig, TestType
        config = value.get('test_config') or {}
        return TestPlanInfo(
            id=value['id'], project_id=value.get('project_id', 'default'), name=value['name'],
            description=value.get('description', ''), jmx_path=value.get('jmx_path', ''),
            created_at=value.get('created_at'),
            test_config=TestConfig(**{'test_type': TestType(config.get('test_type', 'baseline')), **config}),
            endpoints_count=value.get('endpoints_count', 0),
            storage_version=value.get('storage_version', 1))

    @staticmethod
    def _execution_value(value):
        from app.models.schemas import (
            DetailedTestSummary, ReportAnalysis, TestExecutionInfo, TestExecutionStatus,
        )
        result = TestExecutionInfo(
            id=value['id'], project_id=value.get('project_id', 'default'),
            test_plan_id=value.get('test_plan_id'),
            status=TestExecutionStatus(value.get('status', 'pending')),
            jmx_path=value.get('jmx_path', ''), start_time=value.get('start_time'),
            end_time=value.get('end_time'), report_dir=value.get('report_dir', ''),
            log_path=value.get('log_path', ''), error_message=value.get('error_message'),
            source_jmx_path=value.get('source_jmx_path'), source_sha256=value.get('source_sha256'),
            context_snapshot=value.get('context_snapshot'),
            report_status=value.get('report_status') or 'pending',
            report_error=value.get('report_error'),
            resource_snapshot_pre=value.get('resource_snapshot_pre'),
            resource_snapshot_post=value.get('resource_snapshot_post'))
        result.storage_version = value.get('storage_version', 1)
        if value.get('ai_analysis_data'):
            result.ai_analysis = ReportAnalysis(**value['ai_analysis_data'])
        if value.get('summary_data'):
            result.summary = DetailedTestSummary(**value['summary_data'])
        return result

    async def sync_from_dbstore(self):
        """Replace the cache with what the database holds."""
        from app.services.db_store import db_store
        projects = await self._all_pages(db_store.list_projects, 'projects')
        plans = await self._all_pages(db_store.list_test_plans, 'test_plans')
        executions = await self._all_pages(db_store.list_executions, 'executions')
        with self._cache_lock:
            self._projects = {item['id']: self._project_value(item) for item in projects}
            self._test_plans = {item['id']: self._plan_value(item) for item in plans}
            self._executions = {item['id']: self._execution_value(item) for item in executions}
            self._db_synced = True
        logger.info('Loaded %s projects, %s plans and %s executions', len(projects), len(plans), len(executions))
        return True

# Module-level singleton — import this everywhere
store = SharedStore()
