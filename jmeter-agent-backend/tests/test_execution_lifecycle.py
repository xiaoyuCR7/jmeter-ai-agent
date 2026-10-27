"""Lifecycle regressions. All files/SQLite state live under pytest temporary paths."""
import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import db
from app.models.schemas import TestConfig as Config, TestExecutionInfo as Execution, TestExecutionStatus as Status
from app.services.db_store import DBStore
from app.services.storage import store


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.jmeter import executor as executor_module
    import app.services.execution_lifecycle as lifecycle_module
    def forbid_real_executor(*args, **kwargs):
        raise AssertionError('Tests must supply a fake executor; real JMeter is forbidden')
    monkeypatch.setattr(executor_module, 'get_executor', forbid_real_executor)
    monkeypatch.setattr(executor_module.JMeterExecutor, '_start_process', forbid_real_executor)
    monkeypatch.setattr(executor_module.subprocess, 'Popen', forbid_real_executor)
    monkeypatch.setattr(lifecycle_module, 'get_executor', forbid_real_executor)
    # The sample keeps a JSR223 script to prove byte-for-byte snapshots; the script gate has its own tests.
    monkeypatch.setattr(lifecycle_module, 'scan_scripts', lambda content: [])
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'state.db'))
    monkeypatch.setattr(db, '_engine', engine)
    monkeypatch.setattr(db, '_async_session_factory', async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, 'TEST_PLANS_DIR_DEFAULT', str(tmp_path / 'plans'))
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(tmp_path / 'reports'))
    monkeypatch.setattr('app.core.config.load_paths_config', lambda: {})
    for attr in ('_projects', '_test_plans', '_executions'):
        monkeypatch.setattr(store, attr, {})
    await db.init_db()
    path = tmp_path / 'plans' / 'sample.jmx'
    path.parent.mkdir()
    path.write_bytes(b'<?xml version="1.0"?><jmeterTestPlan><hashTree><JSR223Sampler><stringProp name="script"><![CDATA[println("keep me")]]></stringProp></JSR223Sampler><hashTree/></hashTree></jmeterTestPlan>')
    yield SimpleNamespace(path=path, root=tmp_path, db=DBStore())
    await engine.dispose()


def service():
    import app.services.execution_lifecycle as module
    return module.ExecutionLifecycle(), module


@pytest.mark.asyncio
async def test_execution_lifecycle_can_restart_after_clean_shutdown(env):
    lifecycle, _ = service()
    await lifecycle.shutdown()

    lifecycle.start()
    prepared = await lifecycle.prepare(str(env.path), 'after-restart')

    assert prepared.status == Status.PENDING
    await lifecycle.shutdown()


@pytest.mark.asyncio
async def test_revoked_execution_still_records_owned_process_termination(env, monkeypatch):
    from fastapi import HTTPException
    lifecycle, module = service()
    prepared = await lifecycle.prepare(str(env.path), 'revoked-finish')
    persist = module.persist_execution
    async def revoked_after_process(execution, **kwargs):
        if execution.status == Status.COMPLETED:
            raise HTTPException(403, 'User revoked after process termination')
        await persist(execution, **kwargs)
    monkeypatch.setattr(module, 'persist_execution', revoked_after_process)
    executor = SimpleNamespace(execute=AsyncMock(return_value=Execution(
        id=prepared.id, test_plan_id=prepared.test_plan_id, status=Status.COMPLETED,
        jmx_path=prepared.jmx_path, end_time=datetime.now())))
    with pytest.raises(HTTPException):
        await lifecycle.run(prepared.id, executor=executor)
    row = await env.db.get_execution(prepared.id)
    assert row['status'] == 'completed' and row['end_time']
    assert row['ai_analysis_data'] is None
    assert row['summary_data'] is None
    assert not lifecycle._tasks
    with pytest.raises(RuntimeError, match='not owned'):
        await lifecycle._persist_owned_state(prepared)


@pytest.mark.asyncio
async def test_submitted_task_failing_before_launch_does_not_remain_pending(env, monkeypatch):
    lifecycle, module = service()
    def unavailable():
        raise RuntimeError('JMeter unavailable')
    monkeypatch.setattr(module, 'get_executor', unavailable)
    prepared = await lifecycle.submit(str(env.path), 'failed-before-run')
    task = lifecycle._tasks[prepared.id]
    await asyncio.gather(task, return_exceptions=True)
    assert (await env.db.get_execution(prepared.id))['status'] == 'failed'
    assert not lifecycle._prepared and not lifecycle._tasks


@pytest.mark.asyncio
async def test_snapshot_registered_before_launch_and_round_trips(env, monkeypatch):
    lifecycle, module = service()
    original = env.path.read_bytes()
    calls = []
    async def execute(path, execution_id, progress_callback=None, **kwargs):
        record = await env.db.get_execution(execution_id)
        assert record['status'] == 'running'
        assert store.get_execution(execution_id).status == Status.RUNNING
        assert record['source_sha256'] == hashlib.sha256(original).hexdigest()
        assert Path(path).read_bytes() == original
        assert kwargs['working_directory'] == str(env.path.parent.resolve())
        assert await env.db.get_test_plan(record['test_plan_id'])
        env.path.write_text('changed later')
        calls.append(path)
        return Execution(id=execution_id, test_plan_id='', status=Status.FAILED,
                         jmx_path=path, end_time=datetime.now(), error_message='process failed',
                         resource_snapshot_pre={'cpu': 2}, resource_snapshot_post={'cpu': 4})
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    result = await lifecycle.execute(str(env.path), 'run-one')
    row = await env.db.get_execution('run-one')
    assert row['status'] == 'failed' and row['error_message'] == 'process failed'
    assert row['end_time'] and row['resource_snapshot_post'] == {'cpu': 4}
    assert Path(calls[0]).read_bytes() == original
    assert result.test_plan_id


@pytest.mark.asyncio
async def test_execution_context_is_captured_once_and_persisted(env):
    lifecycle, _ = service()
    prepared = await lifecycle.prepare(str(env.path), 'context-run')
    row = await env.db.get_execution(prepared.id)
    assert row['context_snapshot']['project_id'] == prepared.project_id
    assert len(row['context_snapshot']['environment_fingerprint']) == 64
    assert row['context_snapshot']['test_config']['concurrent_users'] >= 1
    before = row['context_snapshot']
    store.get_project(prepared.project_id).environment.base_url = 'http://different.invalid'
    assert (await env.db.get_execution(prepared.id))['context_snapshot'] == before
    store.executions.clear()
    await store.sync_from_dbstore()
    assert store.get_execution(prepared.id).context_snapshot == before


@pytest.mark.asyncio
async def test_failed_termination_is_not_persisted_as_terminal(env, monkeypatch):
    lifecycle, module = service()
    fake = SimpleNamespace(_running_processes={})
    async def execute(path, execution_id, **kwargs):
        fake._running_processes[execution_id] = {'cleanup_failed': True}
        return Execution(id=execution_id, test_plan_id='', jmx_path=path, status=Status.FAILED,
                         error_message='termination failed')
    fake.execute = execute
    result = await lifecycle.execute(str(env.path), 'owned-after-timeout', executor=fake)
    assert result.status == Status.RUNNING and result.end_time is None
    row = await env.db.get_execution(result.id)
    assert row['status'] == 'running' and row['error_message']


@pytest.mark.asyncio
async def test_prepare_refuses_existing_run_and_unsafe_identifiers(env):
    lifecycle, _ = service()
    for identifier in ('../escape', 'CON', 'nul.txt', 'x:stream', 'bad/name', 'tail.', 'bad\\name'):
        with pytest.raises(ValueError):
            await lifecycle.prepare(str(env.path), identifier)
    first = await lifecycle.prepare(str(env.path), 'unique')
    with pytest.raises((ValueError, FileExistsError)):
        await lifecycle.prepare(str(env.path), 'unique')
    store.executions.clear()
    with pytest.raises((ValueError, FileExistsError)):
        await lifecycle.prepare(str(env.path), 'unique')
    assert Path(first.jmx_path).read_bytes() == env.path.read_bytes()


@pytest.mark.asyncio
async def test_path_outside_allowed_roots_is_rejected(env):
    lifecycle, _ = service()
    outside = env.root / 'secret.jmx'
    outside.write_text('<jmeterTestPlan/>')
    with pytest.raises(ValueError):
        await lifecycle.prepare(str(outside), 'outside')


@pytest.mark.asyncio
async def test_database_failure_does_not_launch_or_publish_execution(env, monkeypatch):
    lifecycle, module = service()
    fake = SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(module, 'get_executor', lambda: fake)
    monkeypatch.setattr(module.db_store, 'create_execution', AsyncMock(side_effect=RuntimeError('DB unavailable')))
    with pytest.raises(RuntimeError, match='DB unavailable'):
        await lifecycle.execute(str(env.path), 'db-down')
    fake.execute.assert_not_awaited()
    assert store.get_execution('db-down') is None


@pytest.mark.asyncio
async def test_reporting_failure_does_not_rewrite_process_outcome(env, monkeypatch):
    lifecycle, module = service()
    async def execute(path, execution_id, **kwargs):
        return Execution(id=execution_id, test_plan_id='', status=Status.COMPLETED,
                         jmx_path=path, end_time=datetime.now())
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    hook = AsyncMock(side_effect=ValueError('analysis unavailable'))
    result = await lifecycle.execute(str(env.path), 'report-failed', postprocess=hook)
    assert result.status == Status.COMPLETED
    assert result.report_status == 'failed' and 'analysis unavailable' in result.report_error
    assert (await env.db.get_execution(result.id))['report_status'] == 'failed'
    hook.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_queued_and_running_awaits_cleanup(env, monkeypatch):
    lifecycle, module = service()
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def execute(path, execution_id, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    queued = await lifecycle.prepare(str(env.path), 'queued')
    assert await lifecycle.cancel(queued.id)
    assert (await env.db.get_execution(queued.id))['status'] == 'cancelled'
    running = await lifecycle.submit(str(env.path), 'running')
    await started.wait()
    assert await lifecycle.cancel(running.id)
    assert cleaned.is_set()
    assert (await env.db.get_execution(running.id))['status'] == 'cancelled'
    assert not lifecycle._tasks


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_tasks(env, monkeypatch):
    lifecycle, module = service()
    started = asyncio.Event()
    async def execute(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    result = await lifecycle.submit(str(env.path), 'shutdown')
    await started.wait()
    await lifecycle.shutdown()
    assert (await env.db.get_execution(result.id))['status'] == 'cancelled'
    assert not lifecycle._tasks


@pytest.mark.asyncio
async def test_restart_restores_full_plan_environment_and_execution_fields(env):
    from app.models.schemas import ProjectInfo, ProjectEnvironments, EnvironmentConfig, TestPlanInfo
    from app.services.execution_lifecycle import persist_project, persist_execution
    lifecycle, _ = service()
    project = ProjectInfo(id='multi-env', name='Environments', environments=ProjectEnvironments(
        environments=[EnvironmentConfig(id='staging', name='Staging', base_url='https://invalid.test')], active_id='staging'))
    await persist_project(project)
    await lifecycle.register_plan(TestPlanInfo(id='iterations', project_id=project.id, name='Loop',
        jmx_path=str(env.path), created_at=datetime.now(), endpoints_count=1,
        test_config=Config(test_type='stress', iterations=9, step_users=7, stability_hours=2.5)))
    execution = await lifecycle.prepare(str(env.path), 'restore', test_plan_id='iterations')
    execution.status, execution.report_status = Status.FAILED, 'failed'
    execution.error_message, execution.report_error = 'process', 'report'
    execution.end_time = datetime.now()
    execution.resource_snapshot_post = {'cpu': 1}
    await persist_execution(execution)
    store.projects.clear(); store.test_plans.clear(); store.executions.clear()
    await store.sync_from_dbstore()
    assert store.get_project(project.id).environments.active_id == 'staging'
    assert store.get_test_plan('iterations').test_config.iterations == 9
    assert store.get_test_plan('iterations').test_config.step_users == 7
    restored = store.get_execution('restore')
    assert restored.source_sha256 == execution.source_sha256
    assert restored.report_error == 'report' and restored.error_message == 'process'
    assert restored.resource_snapshot_post == {'cpu': 1}


@pytest.mark.asyncio
async def test_cancel_does_not_claim_success_when_process_termination_fails(env, monkeypatch):
    lifecycle, module = service()
    execution = await lifecycle.prepare(str(env.path), 'kill-fails')
    from app.services.execution_lifecycle import persist_execution
    execution.status = Status.RUNNING
    await persist_execution(execution)
    fake = SimpleNamespace(_running_processes={execution.id: {}}, cancel=AsyncMock(return_value=False))
    monkeypatch.setattr(module, 'get_executor', lambda: fake)
    assert await lifecycle.cancel(execution.id) is False
    assert (await env.db.get_execution(execution.id))['status'] == 'running'


@pytest.mark.asyncio
async def test_agent_created_plan_is_immediately_registered(env):
    from app.services.ai.agent.tools.jmx import create_jmx
    result = await create_jmx('Agent-created', [{'name': 'Ping', 'method': 'GET', 'path': '/'}],
                              'https://invalid.test')
    assert result.get('test_plan_id')
    assert store.get_test_plan(result['test_plan_id'])
    assert await env.db.get_test_plan(result['test_plan_id'])


@pytest.mark.asyncio
async def test_http_and_agent_use_same_lifecycle(env, monkeypatch):
    from app.api import routes
    from app.services.ai.agent.tools import jmeter
    from fastapi import BackgroundTasks
    lifecycle, module = service()
    plan = await lifecycle.register_path(str(env.path))
    marker = Execution(id='shared', test_plan_id=plan.id, status=Status.COMPLETED,
                       jmx_path=str(env.path))
    fake = SimpleNamespace(submit=AsyncMock(return_value=marker), execute=AsyncMock(return_value=marker))
    monkeypatch.setattr(module, 'execution_lifecycle', fake)
    # Module imports may hold the singleton too.
    monkeypatch.setattr(routes, 'execution_lifecycle', fake, raising=False)
    monkeypatch.setattr(jmeter, 'execution_lifecycle', fake, raising=False)
    response = await routes.execute_test_plan(plan.id, BackgroundTasks(), ai_model=None)
    assert response.id == 'shared'
    fake.submit.assert_awaited_once()
    response = await jmeter.execute_test(str(env.path), skip_resource_check=True)
    assert response['execution_id'] == 'shared'
    fake.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_common_report_parse_failure_is_persisted_without_fake_process_failure(env, monkeypatch):
    lifecycle, module = service()
    async def execute(path, execution_id, **kwargs):
        return Execution(id=execution_id, test_plan_id='', status=Status.COMPLETED, jmx_path=path)
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    result = await lifecycle.execute(str(env.path), 'no-jtl')
    assert result.status == Status.COMPLETED
    assert result.report_status == 'failed' and result.report_error
    assert (await env.db.get_execution(result.id))['report_status'] == 'failed'


@pytest.mark.asyncio
async def test_cloned_and_imported_plans_register_without_overwriting(env):
    from app.services.ai.agent.tools.jmx import clone_jmx, import_jmx
    await service()[0].register_path(str(env.path))
    cloned = await clone_jmx(str(env.path), 'Clone')
    assert cloned.get('test_plan_id')
    assert await env.db.get_test_plan(cloned['test_plan_id'])
    imported = await import_jmx(str(env.path), target_name='Imported')
    assert imported.get('test_plan_id')
    assert await env.db.get_test_plan(imported['test_plan_id'])
    target = Path(imported['imported_path'])
    original = target.read_bytes()
    again = await import_jmx(str(env.path), target_name='Imported')
    assert again.get('error')
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_scheduler_wait_is_registered_and_cancellable(env, monkeypatch):
    from app.services.jmeter.test_scheduler import TestScheduler, ReadinessCheck, SchedulerDecision
    from app.services.jmeter.resource_monitor import ResourceSnapshot
    lifecycle, module = service()
    monkeypatch.setattr(module, 'execution_lifecycle', lifecycle)
    waiting = asyncio.Event()
    snap = ResourceSnapshot(cpu_percent=80, memory_percent=20, memory_used_gb=1, memory_total_gb=8, disk_percent=10)
    async def wait(**kwargs):
        waiting.set()
        await asyncio.Event().wait()
    scheduler = TestScheduler(monitor=SimpleNamespace(wait_until_safe=wait, snapshot=lambda: snap))
    monkeypatch.setattr(scheduler, 'check_readiness', lambda: ReadinessCheck(SchedulerDecision.WAIT, snap, 'busy'))
    task = asyncio.create_task(scheduler.run_one(str(env.path), 'resource-wait'))
    try:
        await waiting.wait()
        assert await env.db.get_execution('resource-wait')
        assert await lifecycle.cancel('resource-wait')
        assert (await env.db.get_execution('resource-wait'))['status'] == 'cancelled'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_legacy_database_migration_is_idempotent_and_preserves_rows(env):
    lifecycle, _ = service()
    execution = await lifecycle.prepare(str(env.path), 'legacy')
    async with db.get_engine().begin() as conn:
        for name in ('summary_data', 'source_jmx_path', 'source_sha256', 'report_status', 'report_error'):
            await conn.exec_driver_sql(f'ALTER TABLE executions DROP COLUMN {name}')
        await conn.exec_driver_sql('ALTER TABLE projects DROP COLUMN environments_data')
    await db.init_db()
    await db.init_db()
    row = await env.db.get_execution(execution.id)
    assert row['id'] == execution.id and row['report_status'] == 'pending'
    assert await env.db.get_test_plan(execution.test_plan_id)


@pytest.mark.asyncio
async def test_cancelled_before_start_report_is_readable(env):
    from app.api import routes
    lifecycle, _ = service()
    execution = await lifecycle.prepare(str(env.path), 'early-cancel')
    await lifecycle.cancel(execution.id)
    report = await routes.get_report(execution.id)
    assert report.status == 'cancelled' and report.duration_seconds == 0


@pytest.mark.asyncio
async def test_recovery_never_infers_success_from_jtl_presence(env):
    from app.api import routes
    for name, flag in [('legacy-unknown', None), ('legacy-failed', 'FAILED')]:
        directory = env.root / 'reports' / name
        directory.mkdir(parents=True)
        (directory / 'result.jtl').write_text('partial')
        if flag:
            (directory / '_done.flag').write_text(flag)
    routes.scan_reports()
    assert store.get_execution('legacy-unknown').status == Status.FAILED
    assert store.get_execution('legacy-failed').status == Status.FAILED


@pytest.mark.asyncio
async def test_shutdown_marks_prepared_unstarted_execution_cancelled(env):
    lifecycle, _ = service()
    execution = await lifecycle.prepare(str(env.path), 'prepared-stop')
    await lifecycle.shutdown()
    assert (await env.db.get_execution(execution.id))['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_scheduler_preserves_injected_executor(env, monkeypatch):
    from app.services.jmeter.test_scheduler import TestScheduler, ReadinessCheck, SchedulerDecision
    from app.services.jmeter.resource_monitor import ResourceSnapshot
    lifecycle, module = service()
    monkeypatch.setattr(module, 'execution_lifecycle', lifecycle)
    snap = ResourceSnapshot(cpu_percent=10, memory_percent=20, memory_used_gb=1, memory_total_gb=8, disk_percent=10)
    fake = SimpleNamespace(execute=AsyncMock(return_value=Execution(
        id='injected', test_plan_id='', status=Status.FAILED, jmx_path=str(env.path))))
    scheduler = TestScheduler(executor=fake, monitor=SimpleNamespace(snapshot=lambda: snap))
    monkeypatch.setattr(scheduler, 'check_readiness', lambda: ReadinessCheck(SchedulerDecision.PROCEED, snap, 'ready'))
    await scheduler.run_one(str(env.path), 'injected')
    fake.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_postprocess_cancel_keeps_process_status_and_marks_reporting_cancelled(env, monkeypatch):
    lifecycle, module = service()
    entered = asyncio.Event()
    async def execute(path, execution_id, **kwargs):
        return Execution(id=execution_id, test_plan_id='', status=Status.COMPLETED, jmx_path=path)
    async def hook(execution):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    execution = await lifecycle.submit(str(env.path), 'post-cancel', postprocess=hook)
    await entered.wait()
    await lifecycle.shutdown()
    row = await env.db.get_execution(execution.id)
    assert row['status'] == 'completed' and row['report_status'] == 'cancelled'


def _record_events(lifecycle):
    seen = []
    for event in ('started', 'finished'):
        async def listener(execution, event=event):
            seen.append((event, execution.status.value, execution.summary is not None))
        lifecycle.on(event, listener)
    return seen


@pytest.mark.asyncio
async def test_listeners_hear_start_and_finish_once_before_postprocessing(env, monkeypatch):
    lifecycle, module = service()
    seen = _record_events(lifecycle)
    async def broken(execution):
        raise RuntimeError('listener bug')
    lifecycle.on('finished', broken)
    async def execute(path, execution_id, **kwargs):
        return Execution(id=execution_id, test_plan_id='', status=Status.COMPLETED,
                         jmx_path=path, end_time=datetime.now())
    async def postprocess(execution):
        seen.append(('postprocess',))
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    result = await lifecycle.execute(str(env.path), 'events-ok', postprocess=postprocess)
    assert result.status == Status.COMPLETED  # a failing listener cannot fail the run
    assert seen == [('started', 'running', False), ('finished', 'completed', False), ('postprocess',)]


@pytest.mark.asyncio
async def test_runs_cancelled_before_launch_or_failing_still_finish(env, monkeypatch):
    lifecycle, module = service()
    seen = _record_events(lifecycle)
    async def not_ready(execution):
        execution.status = Status.CANCELLED
    await lifecycle.execute(str(env.path), 'events-cancelled', before_execute=not_ready)
    async def crash(path, execution_id, **kwargs):
        raise RuntimeError('JMeter crashed')
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=crash))
    with pytest.raises(RuntimeError):
        await lifecycle.execute(str(env.path), 'events-failed')
    assert seen == [('finished', 'cancelled', False), ('started', 'running', False), ('finished', 'failed', False)]


@pytest.mark.asyncio
async def test_cancelling_during_postprocessing_does_not_finish_twice(env, monkeypatch):
    lifecycle, module = service()
    seen = _record_events(lifecycle)
    entered = asyncio.Event()
    async def execute(path, execution_id, **kwargs):
        return Execution(id=execution_id, test_plan_id='', status=Status.COMPLETED, jmx_path=path)
    async def hook(execution):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    await lifecycle.submit(str(env.path), 'events-post-cancel', postprocess=hook)
    await entered.wait()
    await lifecycle.shutdown()
    assert [e for e in seen if e[0] == 'finished'] == [('finished', 'completed', False)]


@pytest.mark.asyncio
async def test_cancel_endpoint_path_emits_cancelled(env, monkeypatch):
    lifecycle, module = service()
    seen = _record_events(lifecycle)
    started = asyncio.Event()
    async def execute(path, execution_id, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    execution = await lifecycle.submit(str(env.path), 'events-user-cancel')
    await started.wait()
    assert await lifecycle.cancel(execution.id)
    assert seen == [('started', 'running', False), ('finished', 'cancelled', False)]


@pytest.mark.asyncio
async def test_local_runs_with_scripts_need_explicit_approval(env, monkeypatch):
    from app.core.config import settings
    from app.services.jmeter import script_risk
    lifecycle, module = service()
    monkeypatch.setattr(module, 'scan_scripts', script_risk.scan)
    monkeypatch.setattr(settings, 'JMETER_EXECUTION_MODE', 'local')
    with pytest.raises(script_risk.ScriptApprovalRequired) as refused:
        await lifecycle.prepare(str(env.path), 'scripts-refused')
    assert refused.value.findings == [{'name': 'JSR223Sampler', 'kind': 'JSR223Sampler'}]
    assert store.get_execution('scripts-refused') is None
    assert not (env.root / 'reports' / 'scripts-refused').exists()  # nothing created before approval
    assert (await lifecycle.prepare(str(env.path), 'scripts-approved', allow_scripts=True)).status == Status.PENDING
    monkeypatch.setattr(settings, 'JMETER_EXECUTION_MODE', 'container')
    assert (await lifecycle.prepare(str(env.path), 'scripts-in-container')).status == Status.PENDING
    await lifecycle.shutdown()
