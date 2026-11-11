"""Shared in-memory cache of projects, test plans and executions.

Writes go to DBStore first, then here; reads come from here. It is loaded from the database at
startup. Used by both REST API routes and agent tools.

Projects and plans are held whole. Runs are held as headlines, with the detail of the runs in use
(see ``ExecutionCache``).
"""

import threading
import logging
from collections import OrderedDict
from collections.abc import MutableMapping
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


HEADLINE_FIELDS = ('total_requests', 'avg_response_time', 'min_response_time', 'max_response_time',
                   'p95_response_time', 'p99_response_time', 'error_rate', 'throughput',
                   'received_bytes_per_sec', 'sent_bytes_per_sec', 'percentiles')
ACTIVE = ('pending', 'running')


def headline(execution):
    """A light copy of a run: its overall numbers, without per-request detail or the AI report.

    ``detail_loaded`` is False on it, and such a copy is never saved.
    """
    from app.models.schemas import DetailedTestSummary
    summary = execution.summary
    if summary is not None:
        summary = DetailedTestSummary(**{name: getattr(summary, name) for name in HEADLINE_FIELDS})
    return execution.model_copy(update={'summary': summary, 'ai_analysis': None, 'detail_loaded': False})


class ExecutionCache(MutableMapping):
    """Every run by ID, without holding every run's detail in memory.

    All runs used to be kept whole: thousands of summaries and AI reports loaded at startup and never
    released. Now each run has a headline copy (see ``headline``), and only the runs in use are whole:

    - looking a run up by ID (``cache[id]``, ``get``, ``store.get_execution``) returns it whole, read
      from the database when it is not among the ``hot_limit`` most recently used;
    - iterating (``values``, ``items``) returns whatever is at hand, so a run may be a headline copy.
      Code that picks a run from a scan and needs its detail looks it up by ID.

    A run that is still going, or that was put here without being saved, is always kept whole.
    """

    def __init__(self, loader: Optional[Callable[[str], Any]] = None, hot_limit: int = 256):
        self._loader = loader
        self._hot_limit = max(1, hot_limit)
        self._light: Dict[str, Any] = {}
        self._hot: 'OrderedDict[str, Any]' = OrderedDict()
        self._pinned: set = set()

    def __getitem__(self, execution_id):
        if execution_id in self._hot:
            self._hot.move_to_end(execution_id)
            return self._hot[execution_id]
        light = self._light[execution_id]
        whole = None
        if self._loader is not None:
            try:
                whole = self._loader(execution_id)
            except Exception as error:
                logger.warning('Run %s could not be read back from the database: %s', execution_id, error)
        if whole is None:
            return light  # Its row is gone or unreadable; what is known is still better than nothing.
        self._hot[execution_id] = whole
        self._evict()
        return whole

    def __setitem__(self, execution_id, execution):
        self.put(execution_id, execution)

    def put(self, execution_id, execution, persisted: bool = False):
        """``persisted``: the database holds exactly this, so it may be dropped from memory and read back."""
        if getattr(execution, 'detail_loaded', True) is False:
            self._light[execution_id] = execution
            return
        self._light[execution_id] = headline(execution) if hasattr(execution, 'model_copy') else execution
        self._hot[execution_id] = execution
        self._hot.move_to_end(execution_id)
        status = getattr(getattr(execution, 'status', None), 'value', getattr(execution, 'status', None))
        if persisted and status not in ACTIVE and self._loader is not None:
            self._pinned.discard(execution_id)
        else:
            self._pinned.add(execution_id)
        self._evict()

    def add_headline(self, execution_id, light):
        """A run known to be in the database, loaded without its detail."""
        self._light[execution_id] = light

    def _evict(self):
        spare = len(self._hot) - len(self._pinned) - self._hot_limit
        if spare <= 0:
            return
        for execution_id in [key for key in self._hot if key not in self._pinned][:spare]:
            del self._hot[execution_id]

    def __delitem__(self, execution_id):
        del self._light[execution_id]
        self._hot.pop(execution_id, None)
        self._pinned.discard(execution_id)

    def __contains__(self, execution_id):
        return execution_id in self._light

    def __iter__(self):
        return iter(list(self._light))

    def __len__(self):
        return len(self._light)

    def values(self):
        return [self._hot.get(key, light) for key, light in list(self._light.items())]

    def items(self):
        return [(key, self._hot.get(key, light)) for key, light in list(self._light.items())]

    def whole_count(self) -> int:
        return len(self._hot)


def _load_whole_execution(execution_id: str):
    from app.services.db_store import load_execution_sync
    row = load_execution_sync(execution_id)
    return SharedStore._execution_value(row) if row else None


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
        self._executions: MutableMapping = self._new_execution_cache()
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

    @staticmethod
    def _new_execution_cache() -> 'ExecutionCache':
        from app.core.config import settings
        return ExecutionCache(_load_whole_execution, settings.EXECUTION_CACHE_SIZE)

    def get_execution(self, execution_id: str):
        """The whole run, read back from the database if it is not in memory."""
        return self.executions.get(execution_id)

    def set_execution(self, execution_id: str, execution, persisted: bool = False):
        """``persisted``: the caller has just saved exactly this to the database."""
        cache = self.executions
        if isinstance(cache, ExecutionCache):
            cache.put(execution_id, execution, persisted)
        else:
            cache[execution_id] = execution

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
    def _execution_value(value, light: bool = False):
        """``light`` builds the headline copy straight from the row, without validating its detail."""
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
        if light:
            result.detail_loaded = False
            summary = value.get('summary_data')
            if summary:
                try:
                    result.summary = DetailedTestSummary(**{name: summary[name] for name in HEADLINE_FIELDS
                                                            if name in summary})
                except Exception:
                    # An old row missing a number: the whole run still loads, and says so, when asked for.
                    result.summary = None
            return result
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
        # A page at a time, each row reduced to its headline before the next page is read: memory at
        # startup follows the page size, not the number of runs ever made.
        executions, offset, total = self._new_execution_cache(), 0, None
        active = []
        while total is None or offset < total:
            page = await db_store.list_executions(offset=offset, limit=200)
            batch, total = page.get('executions', []), page.get('total')
            if not isinstance(total, int) or total < 0 or not isinstance(batch, list):
                raise RuntimeError('Business cache snapshot is malformed')
            if not batch and offset < total:
                raise RuntimeError('Business cache snapshot ended before its declared total')
            for item in batch:
                executions.add_headline(item['id'], self._execution_value(item, light=True))
                if item.get('status') in ACTIVE:
                    active.append(self._execution_value(item))
            offset += len(batch)
        for execution in active:
            executions.put(execution.id, execution, persisted=True)  # Still going: kept whole.
        with self._cache_lock:
            self._projects = {item['id']: self._project_value(item) for item in projects}
            self._test_plans = {item['id']: self._plan_value(item) for item in plans}
            self._executions = executions
            self._db_synced = True
        logger.info('Loaded %s projects, %s plans and %s executions', len(projects), len(plans), len(executions))
        return True

# Module-level singleton — import this everywhere
store = SharedStore()
