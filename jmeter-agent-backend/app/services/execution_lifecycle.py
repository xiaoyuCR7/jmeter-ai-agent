"""One persisted execution lifecycle shared by HTTP, agents and MCP.

JMX is copied byte-for-byte; no parsing, rebuilding or script stripping occurs.
This is bookkeeping and task ownership, not a sandbox: in local mode a plan with
scripts runs only with the user's approval (``allow_scripts``); container mode
is the isolation boundary.
"""
import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from fastapi import HTTPException

from app.core.config import settings
from app.models.schemas import (
    ProjectInfo, TestConfig, TestPlanInfo, TestExecutionInfo, TestExecutionStatus,
)
from app.services.db_store import db_store
from app.services.storage import store
from app.services.jmeter.executor import get_executor
from app.services.jmeter.script_risk import ScriptApprovalRequired, scan as scan_scripts

logger = logging.getLogger(__name__)


def _directory_project(path):
    """Plans live in TEST_PLANS_DIR/<project_id>/; infer that project when it exists."""
    root = Path(settings.TEST_PLANS_DIR).resolve()
    try:
        relative = Path(path).resolve().relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) < 2:
        return None
    return relative.parts[0] if relative.parts[0] in store.projects else None


TERMINAL = {TestExecutionStatus.COMPLETED, TestExecutionStatus.FAILED, TestExecutionStatus.CANCELLED}

# started: the process is launching. finished: the run reached a terminal status (completed,
# failed or cancelled), with its summary when the report parsed; sent once per run.
# analysis_completed: emitted by whoever ran the post-run AI analysis.
EVENTS = ('started', 'finished', 'analysis_completed')


def validate_id(value: str) -> str:
    """IDs are portable single path components, including on Windows."""
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,49}", value)
            or value.endswith('.') or value.split('.')[0].upper() in
            {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
        raise ValueError('Invalid execution/project/plan ID: expected a safe path component')
    return value


async def persist_project(project: ProjectInfo, *, create=None):
    data = project.model_dump()
    data['environment_data'] = data.pop('environment')
    data['environments_data'] = data.pop('environments')
    data['base_url'] = data['environment_data'].get('base_url', '')
    data['storage_version'] = project.storage_version
    if create is True:
        persisted = await db_store.create_project(data)
    elif create is False:
        persisted = await db_store.update_project(project.id, data)
        if persisted is None:
            raise LookupError(f'Project not registered: {project.id}')
    elif await db_store.get_project(project.id):
        persisted = await db_store.update_project(project.id, data)
    else:
        # Compatibility for internal import/fixture callers. Request handlers
        # pass an explicit create/update intent so stale cache cannot resurrect.
        persisted = await db_store.create_project(data)
    project.storage_version = persisted['storage_version']
    store.set_project(project.id, project.model_copy(deep=True))


async def persist_execution(execution: TestExecutionInfo, *, create=False):
    data = execution.model_dump()
    data['status'] = execution.status.value
    data['ai_analysis_data'] = data.pop('ai_analysis')
    data['summary_data'] = data.pop('summary')
    data['storage_version'] = execution.storage_version
    if create:
        persisted = await db_store.create_execution(data)
    else:
        persisted = await db_store.update_execution(execution.id, data)
        if not persisted:
            raise LookupError(f'Execution not registered: {execution.id}')
    execution.storage_version = persisted['storage_version']
    store.set_execution(execution.id, execution.model_copy(deep=True))


class ExecutionLifecycle:
    def __init__(self):
        self._tasks = {}
        self._registration_lock = asyncio.Lock()
        self._closed = False
        self._executors = {}
        self._prepared = set()
        self._listeners = {event: [] for event in EVENTS}
        self._finished = set()

    def on(self, event, handler):
        """Call ``await handler(execution)`` on ``event`` for every run, whatever started it.

        Unlike the per-call before_execute/postprocess, listeners see runs from the Web,
        agents, the scheduler and MCP alike. They get a copy, must return quickly and
        cannot fail the run.
        """
        if handler not in self._listeners[event]:
            self._listeners[event].append(handler)

    def off(self, event, handler):
        if handler in self._listeners[event]:
            self._listeners[event].remove(handler)

    async def emit(self, event, execution):
        if event == 'finished':
            if execution.status not in TERMINAL or execution.id in self._finished:
                return
            self._finished.add(execution.id)
        for handler in list(self._listeners[event]):
            try:
                await handler(execution.model_copy(deep=True))
            except Exception as error:
                logger.warning('%s listener failed for %s: %s', event, execution.id, error)

    def start(self):
        """Reopen admission only after the previous lifecycle fully drained."""
        if self._tasks or self._executors or self._prepared:
            raise RuntimeError('Cannot restart execution lifecycle while work remains')
        self._closed = False

    async def _persist_owned_state(self, execution):
        """An already owned process must record cleanup even after user revocation.

        This capability cannot create a run, change ownership or publish model
        output. Only this lifecycle's current task may use it for its own ID.
        """
        if self._tasks.get(execution.id) is not asyncio.current_task():
            raise RuntimeError('Execution cleanup is not owned by this task')
        fields = ('status', 'start_time', 'end_time', 'error_message', 'report_status', 'report_error')
        updates = {name: getattr(execution, name) for name in fields}
        updates['status'] = execution.status.value
        existing = await db_store.get_execution(execution.id)
        if existing is None or existing['project_id'] != execution.project_id:
            raise RuntimeError('Owned execution registration changed')
        updates['storage_version'] = execution.storage_version
        persisted = await db_store.update_execution(execution.id, updates)
        execution.storage_version = persisted['storage_version']
        cached = store.get_execution(execution.id)
        if cached is not None:
            cached = cached.model_copy(deep=True)
            for name in fields:
                setattr(cached, name, getattr(execution, name))
            cached.storage_version = execution.storage_version
            store.set_execution(execution.id, cached)

    async def register_plan(self, plan: TestPlanInfo):
        validate_id(plan.id)
        validate_id(plan.project_id)
        path = await self._validate_source(plan.jmx_path)
        owner = _directory_project(path)
        if owner is not None and owner != plan.project_id:
            raise HTTPException(409, 'Plan project does not match source file directory')
        plan = plan.model_copy(update={'jmx_path': str(path)}, deep=True)
        if not await db_store.get_project(plan.project_id):
            cached_project = store.get_project(plan.project_id)
            if cached_project is not None:
                raise LookupError(f'Project was deleted while registering plan: {plan.project_id}')
            await persist_project(ProjectInfo(
                id=plan.project_id, name='Default Project' if plan.project_id == 'default' else plan.project_id),
                create=True)
        data = plan.model_dump()
        data['test_type'] = plan.test_config.test_type.value
        data['storage_version'] = plan.storage_version
        if await db_store.get_test_plan(plan.id):
            persisted = await db_store.update_test_plan(plan.id, data)
        else:
            persisted = await db_store.create_test_plan(data)
        if persisted is None:
            raise LookupError(f'Test plan not registered: {plan.id}')
        plan.storage_version = persisted['storage_version']
        store.set_test_plan(plan.id, plan.model_copy(deep=True))
        return plan

    async def _validate_source(self, jmx_path):
        path = Path(jmx_path).expanduser().resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != '.jmx':
            raise ValueError('Execution source must be a .jmx file')
        if path.is_relative_to(Path(settings.TEST_PLANS_DIR).resolve()):
            return path
        # Old configured roots remain usable only for the exact DB-registered
        # file, never for arbitrary siblings or the entire application BASE_DIR.
        plans = await db_store.list_test_plans(limit=100000)
        if any(path == Path(p['jmx_path']).resolve() for p in plans['test_plans'] if p.get('jmx_path')):
            return path
        executions = await db_store.list_executions(limit=100000)
        if any(p.get('source_sha256') and path == Path(p['jmx_path']).resolve()
               for p in executions['executions'] if p.get('jmx_path')):
            return path
        raise ValueError('JMX source is outside the test-plan root and is not registered')

    async def register_path(self, jmx_path, *, project_id='default', name=None,
                            test_config=None, endpoints_count=0):
        path = await self._validate_source(jmx_path)
        owner = _directory_project(path)
        if owner is not None:
            if project_id not in (None, 'default', owner):
                raise HTTPException(409, 'Requested project differs from source file directory')
            project_id = owner
        async with self._registration_lock:
            plans = await db_store.list_test_plans(limit=100000)
            for data in plans['test_plans']:
                if data.get('jmx_path') and Path(data['jmx_path']).resolve() == path:
                    plan = TestPlanInfo(**data)
                    store.set_test_plan(plan.id, plan)
                    return plan
            return await self.register_plan(TestPlanInfo(
                id=uuid.uuid4().hex, project_id=project_id or 'default',
                name=name or path.stem, jmx_path=str(path), created_at=datetime.now(),
                test_config=test_config or TestConfig(test_type='custom'),
                endpoints_count=endpoints_count,
            ))

    async def prepare(self, jmx_path, execution_id='', *, test_plan_id=None,
                      project_id='default', test_config=None, allow_scripts=False):
        """``allow_scripts`` is the user's approval to run a plan's scripts on this machine."""
        if self._closed:
            raise RuntimeError('Execution lifecycle is shutting down')
        execution_id = validate_id(execution_id or uuid.uuid4().hex)
        path = await self._validate_source(jmx_path)
        # The bytes checked here are the bytes snapshotted and run.
        content = path.read_bytes()
        if settings.JMETER_EXECUTION_MODE == 'local' and not allow_scripts:
            findings = scan_scripts(content)
            if findings:
                raise ScriptApprovalRequired(findings)
        if store.get_execution(execution_id) or await db_store.get_execution(execution_id):
            raise ValueError(f'Execution ID already exists: {execution_id}')
        if test_plan_id:
            validate_id(test_plan_id)
            plan = store.get_test_plan(test_plan_id)
            if plan is None:
                data = await db_store.get_test_plan(test_plan_id)
                if data is None:
                    raise LookupError(f'Test plan not found: {test_plan_id}')
                plan = TestPlanInfo(**data)
            if Path(plan.jmx_path).resolve() != path:
                raise ValueError('Execution source does not match the registered plan')
            plan = await self.register_plan(plan)
        else:
            plan = await self.register_path(str(path), project_id=project_id, test_config=test_config)
        root = Path(settings.REPORTS_DIR).resolve()
        root.mkdir(parents=True, exist_ok=True)
        run_dir = root / execution_id
        # Never reuse an existing output directory, even when it has no DB row.
        run_dir.mkdir(exist_ok=False)
        snapshot = run_dir / 'snapshot.jmx'
        with snapshot.open('xb') as output:
            output.write(content)
        source_path = str(path)
        # Re-running a known snapshot preserves the original resource directory.
        for existing in store.executions.values():
            if Path(existing.jmx_path).resolve() == path and existing.source_jmx_path:
                source_path = existing.source_jmx_path
                break
        project = store.get_project(plan.project_id)
        environment = project.environment.model_dump() if project else {}
        environment_id = 'legacy'
        if project and project.environments and project.environments.active_id:
            environment_id = project.environments.active_id
            active = next((e for e in project.environments.environments if e.id == environment_id), None)
            if active is not None:
                environment = active.model_dump()
        context_snapshot = {
            'project_id': plan.project_id, 'test_plan_id': plan.id, 'environment_id': environment_id,
            'environment_fingerprint': hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest(),
            'test_config': plan.test_config.model_dump(mode='json'),
        }
        execution = TestExecutionInfo(
            id=execution_id, project_id=plan.project_id, test_plan_id=plan.id,
            status=TestExecutionStatus.PENDING, jmx_path=str(snapshot),
            source_jmx_path=source_path, source_sha256=hashlib.sha256(content).hexdigest(),
            context_snapshot=context_snapshot,
            report_dir=str(run_dir), log_path=str(run_dir / 'jmeter.log'),
        )
        await persist_execution(execution, create=True)
        self._prepared.add(execution.id)
        return execution

    async def run(self, execution_id, *, remote_engines=None, postprocess=None,
                  before_execute=None, progress_callback=None, executor=None):
        execution = store.get_execution(execution_id)
        if execution is None:
            raise LookupError(f'Execution not found: {execution_id}')
        if execution.status in TERMINAL:
            return execution
        current = asyncio.current_task()
        if execution_id in self._tasks and self._tasks[execution_id] is not current:
            raise ValueError(f'Execution already running: {execution_id}')
        self._tasks[execution_id] = current
        execution = execution.model_copy(deep=True)
        try:
            if before_execute:
                await before_execute(execution)
            if execution.status == TestExecutionStatus.CANCELLED:
                execution.end_time = datetime.now()
                await persist_execution(execution)
                await self.emit('finished', execution)
                return execution
            if hashlib.sha256(Path(execution.jmx_path).read_bytes()).hexdigest() != execution.source_sha256:
                raise ValueError('Execution snapshot changed after registration')
            execution.status = TestExecutionStatus.RUNNING
            execution.start_time = datetime.now()
            await persist_execution(execution)
            await self.emit('started', execution)
            def progress(pct):
                execution.progress = pct
                cached = store.get_execution(execution_id)
                if cached:
                    cached.progress = pct
                    store.set_execution(execution_id, cached)
                if progress_callback:
                    progress_callback(pct)
            executor = executor or get_executor()
            self._executors[execution_id] = executor
            result = await executor.execute(
                execution.jmx_path, execution_id, progress_callback=progress,
                remote_engines=remote_engines,
                working_directory=str(Path(execution.source_jmx_path).parent),
            )
            for field in ('status', 'end_time', 'error_message', 'resource_snapshot_pre', 'resource_snapshot_post'):
                value = getattr(result, field, None)
                if value is not None:
                    setattr(execution, field, value)
            for field in ('report_dir', 'log_path'):
                if getattr(result, field, None):
                    setattr(execution, field, getattr(result, field))
            if getattr(executor, '_running_processes', {}).get(execution_id, {}).get('cleanup_failed'):
                execution.status = TestExecutionStatus.RUNNING
                execution.end_time = None
                execution.error_message = 'Termination failed; process/container remains owned and requires cleanup'
                await persist_execution(execution)
                return execution
            if execution.status not in TERMINAL:
                execution.status = TestExecutionStatus.FAILED
                execution.error_message = 'Executor returned without a terminal process result'
            execution.end_time = execution.end_time or datetime.now()
            if execution.status == TestExecutionStatus.COMPLETED:
                execution.progress = 100
            # Commit the process result before parsing, analysis or notifications.
            await persist_execution(execution)
            if execution.status == TestExecutionStatus.COMPLETED:
                execution.report_status = 'running'
                await persist_execution(execution)
                try:
                    from app.services.jmeter.report_parser import ReportParser
                    jtl_path = Path(execution.report_dir) / 'result.jtl'
                    if not jtl_path.is_file() or not jtl_path.stat().st_size:
                        raise ValueError('JTL result file is empty or missing')
                    execution.summary = await asyncio.to_thread(ReportParser().parse_jtl, str(jtl_path))
                    execution.report_status = 'completed'
                except Exception as error:
                    execution.report_status = 'failed'
                    execution.report_error = str(error)
            else:
                execution.report_status = 'skipped'
            # Notify on the process result; postprocessing such as AI analysis can take minutes.
            await self.emit('finished', execution)
            if postprocess:
                previous_report_status = execution.report_status
                execution.report_status = 'running'
                await persist_execution(execution)
                try:
                    await postprocess(execution)
                    if execution.report_status == 'running':
                        execution.report_status = previous_report_status
                except Exception as error:
                    execution.report_status = 'failed'
                    execution.report_error = str(error)
                    logger.warning('Postprocessing failed for %s: %s', execution_id, error)
            await persist_execution(execution)
            return execution
        except asyncio.CancelledError:
            executor = self._executors.get(execution_id)
            still_owned = executor and execution_id in getattr(executor, '_running_processes', {})
            if still_owned:
                execution.status = TestExecutionStatus.RUNNING
                execution.error_message = 'Cancellation cleanup failed; process remains owned'
            elif execution.status not in TERMINAL:
                execution.status = TestExecutionStatus.CANCELLED
                execution.end_time = datetime.now()
            elif execution.report_status == 'running':
                execution.report_status = 'cancelled'
            await self._persist_owned_state(execution)
            await self.emit('finished', execution)
            raise
        except Exception as error:
            if execution.status not in TERMINAL:
                execution.status = TestExecutionStatus.FAILED
                execution.error_message = str(error)
                execution.end_time = datetime.now()
            await self._persist_owned_state(execution)
            await self.emit('finished', execution)
            raise
        finally:
            self._prepared.discard(execution_id)
            if self._tasks.get(execution_id) is current:
                self._tasks.pop(execution_id, None)
            executor = self._executors.get(execution_id)
            if executor and execution_id not in getattr(executor, '_running_processes', {}):
                self._executors.pop(execution_id, None)

    async def execute(self, jmx_path, execution_id='', *, test_plan_id=None,
                      project_id='default', test_config=None, allow_scripts=False, **kwargs):
        execution = await self.prepare(jmx_path, execution_id, test_plan_id=test_plan_id,
                                       project_id=project_id, test_config=test_config,
                                       allow_scripts=allow_scripts)
        return await self._run_prepared(execution, **kwargs)

    async def _run_prepared(self, execution, **kwargs):
        """Keep ownership across the first authorization await after admission."""
        current = asyncio.current_task()
        if self._tasks.get(execution.id) not in (None, current):
            raise RuntimeError('Execution is already owned')
        self._tasks[execution.id] = current
        try:
            return await self.run(execution.id, **kwargs)
        except BaseException as error:
            if execution.id in self._prepared and self._tasks.get(execution.id) is current:
                execution = execution.model_copy(deep=True)
                execution.status = (TestExecutionStatus.CANCELLED if isinstance(error, asyncio.CancelledError)
                                    else TestExecutionStatus.FAILED)
                execution.end_time = datetime.now()
                execution.error_message = 'Execution stopped before launch; authorization or admission failed'
                await self._persist_owned_state(execution)
                await self.emit('finished', execution)
                self._prepared.discard(execution.id)
                self._tasks.pop(execution.id, None)
            raise

    async def submit(self, jmx_path, execution_id='', *, test_plan_id=None,
                     project_id='default', test_config=None, allow_scripts=False, **kwargs):
        execution = await self.prepare(jmx_path, execution_id, test_plan_id=test_plan_id,
                                       project_id=project_id, test_config=test_config,
                                       allow_scripts=allow_scripts)
        task = asyncio.create_task(self._run_prepared(execution, **kwargs))
        self._tasks[execution.id] = task
        def finished(done):
            if self._tasks.get(execution.id) is done:
                self._tasks.pop(execution.id, None)
            if not done.cancelled() and done.exception():
                logger.error('Execution task %s failed: %s', execution.id, done.exception())
        task.add_done_callback(finished)
        return execution

    async def cancel(self, execution_id):
        validate_id(execution_id)
        execution = store.get_execution(execution_id)
        if execution is None or execution.status in TERMINAL:
            return False
        executor = self._executors.get(execution_id)
        if executor is None and execution.status == TestExecutionStatus.RUNNING:
            executor = get_executor()
        if executor and execution_id in getattr(executor, '_running_processes', {}):
            if not await executor.cancel(execution_id):
                return False
        task = self._tasks.get(execution_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._tasks.pop(execution_id, None)
        # A task cancelled before its first instruction cannot run its finally.
        execution = store.get_execution(execution_id).model_copy(deep=True)
        if execution.status not in TERMINAL:
            execution.status = TestExecutionStatus.CANCELLED
            execution.end_time = datetime.now()
            await persist_execution(execution)
            await self.emit('finished', execution)
        self._prepared.discard(execution_id)
        return True

    async def shutdown(self):
        self._closed = True
        await self._shutdown_owned()

    async def _shutdown_owned(self):
        failed = []
        for execution_id in set(self._tasks) | set(self._executors) | self._prepared:
            execution = store.get_execution(execution_id)
            if execution and execution.status not in TERMINAL:
                if not await self.cancel(execution_id):
                    failed.append(execution_id)
            elif execution_id in self._tasks:
                task = self._tasks[execution_id]
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if failed:
            raise RuntimeError(f'Unable to stop owned JMeter processes: {failed}')


execution_lifecycle = ExecutionLifecycle()
