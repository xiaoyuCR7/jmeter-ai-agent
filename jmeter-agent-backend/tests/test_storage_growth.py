"""What keeps the app quick and its folders small as runs pile up: lookups by path, cached settings, pruning."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import config
from app.core.config import settings
from app.models import db
from app.models.schemas import TestExecutionStatus as Status
from app.services import report_retention
from app.services.db_store import DBStore


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'state.db'))
    monkeypatch.setattr(db, '_engine', engine)
    monkeypatch.setattr(db, '_async_session_factory', async_sessionmaker(engine, expire_on_commit=False))
    await db.init_db()
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_database_from_before_the_indexes_gets_them(database):
    wanted = {'ix_executions_test_plan_id', 'ix_executions_created_at', 'ix_test_plans_jmx_path',
              'ix_performance_baselines_test_plan_id'}

    def names(connection):
        inspector = inspect(connection)
        return {index['name'] for table in ('executions', 'test_plans', 'performance_baselines')
                for index in inspector.get_indexes(table)}
    async with database.begin() as connection:
        assert wanted <= await connection.run_sync(names)
        for name in wanted:
            await connection.exec_driver_sql(f'DROP INDEX {name}')
        assert not wanted & await connection.run_sync(names)
    await db.init_db()
    await db.init_db()  # And again without complaint.
    async with database.begin() as connection:
        assert wanted <= await connection.run_sync(names)


@pytest.mark.asyncio
async def test_a_plan_is_found_by_its_path_without_listing_them_all(database):
    store = DBStore()
    await store.create_project({'id': 'p', 'name': 'P'})
    for number in range(3):
        await store.create_test_plan({'id': f'plan-{number}', 'project_id': 'p', 'name': 'n',
                                      'jmx_path': f'/plans/p/{number}.jmx'})
    assert (await store.find_test_plan_by_path('/plans/p/1.jmx'))['id'] == 'plan-1'
    assert await store.find_test_plan_by_path('/plans/p/9.jmx') is None
    await store.create_execution({'id': 'run', 'project_id': 'p', 'jmx_path': '/reports/run/snapshot.jmx',
                                  'source_sha256': 'a' * 64})
    await store.create_execution({'id': 'old', 'project_id': 'p', 'jmx_path': '/old/plan.jmx'})
    assert await store.execution_source_exists('/reports/run/snapshot.jmx')
    assert not await store.execution_source_exists('/old/plan.jmx')  # No recorded hash: not a known source.


def test_the_paths_file_is_read_again_only_when_it_changed(tmp_path, monkeypatch):
    path = tmp_path / 'paths_config.json'
    monkeypatch.setattr(config, 'PATHS_CONFIG_FILE', str(path))
    monkeypatch.setattr(config, '_paths_cache', None)
    assert config.load_paths_config() == {}
    config.save_paths_config({'reports_dir': 'D:/a'})
    assert config.load_paths_config() == {'reports_dir': 'D:/a'}

    reads = []
    real_load = json.load
    monkeypatch.setattr(config.json, 'load', lambda stream: reads.append(1) or real_load(stream))
    for _ in range(5):
        assert settings.REPORTS_DIR == 'D:/a'
    assert reads == []  # Served from the cache.
    config.load_paths_config()['reports_dir'] = 'changed by a caller'
    assert settings.REPORTS_DIR == 'D:/a'  # Callers get a copy.

    config.save_paths_config({'reports_dir': 'D:/b'})
    assert settings.REPORTS_DIR == 'D:/b' and len(reads) == 1
    path.write_text('{"reports_dir": "D:/edited-by-hand"}', encoding='utf-8')
    assert settings.REPORTS_DIR == 'D:/edited-by-hand'
    path.write_text('not json', encoding='utf-8')
    assert settings.REPORTS_DIR == settings.REPORTS_DIR_DEFAULT
    path.unlink()
    assert config.load_paths_config() == {}


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """Three finished runs of different ages and one still running, each with a dashboard and results."""
    monkeypatch.setattr(config, 'load_paths_config', lambda: {})
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(tmp_path))
    now = datetime(2026, 6, 30, 12, 0, 0)
    executions = {}
    for name, age, status in [('old', 40, Status.COMPLETED), ('baseline', 40, Status.COMPLETED),
                              ('recent', 3, Status.COMPLETED), ('running', 40, Status.RUNNING)]:
        run_dir = tmp_path / name
        (run_dir / 'html' / 'content').mkdir(parents=True)
        (run_dir / 'html' / 'index.html').write_text('x' * 1000)
        (run_dir / 'html' / 'content' / 'app.js').write_text('y' * 2000)
        (run_dir / 'result.jtl').write_text('z' * 500)
        (run_dir / 'ai_analysis.md').write_text('report')
        (run_dir / 'snapshot.jmx').write_text('<jmeterTestPlan/>')
        executions[name] = SimpleNamespace(id=name, status=status, report_dir=str(run_dir),
                                           start_time=now - timedelta(days=age, hours=1),
                                           end_time=None if status == Status.RUNNING else now - timedelta(days=age))
    monkeypatch.setattr(report_retention.store, '_executions', executions)

    async def protected():
        return {'baseline'}
    monkeypatch.setattr(report_retention, '_protected', protected)
    return SimpleNamespace(root=tmp_path, now=now)


@pytest.mark.asyncio
async def test_pruning_frees_old_dashboards_and_keeps_what_a_run_found(runs):
    before = report_retention.usage()
    assert before['runs'] == 4 and before['dashboard_bytes'] == 12000 and before['result_bytes'] == 2000
    result = await report_retention.prune(30, now=runs.now)
    assert result == {'pruned_runs': 1, 'freed_bytes': 3000, 'days': 30, 'include_results': False,
                      'kept_for_baselines': 1}
    old = runs.root / 'old'
    assert not (old / 'html').exists()
    # The raw results stay unless asked for, and so does everything a report page reads.
    assert (old / 'result.jtl').exists() and (old / 'ai_analysis.md').exists() and (old / 'snapshot.jmx').exists()
    for kept in ('baseline', 'recent', 'running'):
        assert (runs.root / kept / 'html' / 'index.html').exists()
    assert (await report_retention.prune(30, now=runs.now))['pruned_runs'] == 0  # Nothing left to free.


@pytest.mark.asyncio
async def test_pruning_can_take_the_raw_results_too(runs):
    result = await report_retention.prune(30, include_jtl=True, now=runs.now)
    assert result['freed_bytes'] == 3500 and not (runs.root / 'old' / 'result.jtl').exists()
    assert (runs.root / 'baseline' / 'result.jtl').exists()
    with pytest.raises(ValueError):
        await report_retention.prune(0)


@pytest.mark.asyncio
async def test_a_run_whose_folder_is_elsewhere_is_left_alone(runs, tmp_path_factory):
    outside = tmp_path_factory.mktemp('elsewhere') / 'old'
    (outside / 'html').mkdir(parents=True)
    (outside / 'html' / 'index.html').write_text('keep')
    report_retention.store.executions['old'].report_dir = str(outside)
    assert (await report_retention.prune(30, now=runs.now))['pruned_runs'] == 0
    assert (outside / 'html' / 'index.html').exists()


# ── The run cache: every run in outline, the runs in use whole ──

def _summary(requests=1000):
    from app.models.schemas import DetailedTestSummary
    return DetailedTestSummary(
        total_requests=requests, avg_response_time=120.0, min_response_time=5, max_response_time=900,
        p95_response_time=300.0, p99_response_time=450.0, error_rate=1.5, throughput=80.0,
        received_bytes_per_sec=10, sent_bytes_per_sec=5, percentiles={'p95': 300.0, 'p99': 450.0},
        per_label={'GET /items': {'count': requests, 'p95_response_time': 300.0}},
        timeline_segments=[], response_time_distribution={'0-100': 400})


def _run(execution_id, status=Status.COMPLETED, **overrides):
    from app.models.schemas import ReportAnalysis, TestExecutionInfo
    return TestExecutionInfo(**{
        'id': execution_id, 'project_id': 'p', 'test_plan_id': '', 'status': status, 'jmx_path': 'x.jmx',
        'summary': _summary(), 'context_snapshot': {'environment_id': 'env'},
        'ai_analysis': ReportAnalysis(overall_assessment='fine', conclusion='ok'), **overrides})


def test_the_cache_keeps_only_the_runs_in_use_whole():
    from app.services.storage import ExecutionCache
    saved = {}
    reads = []

    def load(execution_id):
        reads.append(execution_id)
        return saved[execution_id].model_copy(deep=True)
    cache = ExecutionCache(load, hot_limit=2)
    for number in range(5):
        saved[f'run-{number}'] = _run(f'run-{number}')
        cache.put(f'run-{number}', saved[f'run-{number}'], persisted=True)
    assert len(cache) == 5 and cache.whole_count() == 2 and 'run-0' in cache

    # A scan hands out what is in memory: outlines for the runs not in use, with the overall numbers.
    scanned = {run.id: run for run in cache.values()}
    assert scanned['run-0'].detail_loaded is False and scanned['run-4'].detail_loaded is True
    outline = scanned['run-0']
    assert outline.summary.total_requests == 1000 and outline.summary.percentiles == {'p95': 300.0, 'p99': 450.0}
    assert outline.summary.per_label is None and outline.ai_analysis is None
    assert outline.context_snapshot == {'environment_id': 'env'}
    assert reads == []

    # Asking for a run by ID gives it whole, read back once and then kept while it is in use.
    whole = cache['run-0']
    assert whole.detail_loaded and whole.summary.per_label and whole.ai_analysis.conclusion == 'ok'
    assert cache.get('run-0') is whole and reads == ['run-0']
    assert cache.whole_count() == 2 and cache.get('missing') is None

    del cache['run-0']
    assert 'run-0' not in cache and len(cache) == 4
    assert cache.pop('run-1').id == 'run-1' and len(cache) == 3


def test_a_run_in_flight_or_not_yet_saved_is_never_dropped():
    from app.services.storage import ExecutionCache
    cache = ExecutionCache(lambda execution_id: None, hot_limit=1)
    running = _run('running', Status.RUNNING)
    unsaved = _run('recovered-from-disk')
    cache.put('running', running, persisted=True)
    cache['recovered-from-disk'] = unsaved
    for number in range(4):
        cache.put(f'done-{number}', _run(f'done-{number}'), persisted=True)
    assert cache['running'] is running and cache['recovered-from-disk'] is unsaved
    # Once it has ended and been saved, it competes for memory like any other run.
    cache.put('running', _run('running'), persisted=True)
    cache.put('done-9', _run('done-9'), persisted=True)
    assert cache.whole_count() == 2  # The unsaved run and the most recent one.
    # A run whose row is gone still answers with what is known of it.
    assert cache['done-0'].detail_loaded is False and cache['done-0'].summary.total_requests == 1000


@pytest.mark.asyncio
async def test_runs_are_read_back_whole_from_the_database(database, monkeypatch):
    from app.services import execution_lifecycle as lifecycle
    from app.services.storage import ExecutionCache, store
    monkeypatch.setattr(settings, 'EXECUTION_CACHE_SIZE', 2)
    monkeypatch.setattr(store, '_projects', {})
    monkeypatch.setattr(store, '_test_plans', {})
    monkeypatch.setattr(store, '_executions', store._new_execution_cache())
    await DBStore().create_project({'id': 'p', 'name': 'P'})
    await DBStore().create_test_plan({'id': 'plan', 'project_id': 'p', 'name': 'Plan', 'jmx_path': 'x.jmx'})
    for number in range(6):
        await lifecycle.persist_execution(_run(f'run-{number}', test_plan_id='plan'), create=True)
    cache = store.executions
    assert isinstance(cache, ExecutionCache) and len(cache) == 6 and cache.whole_count() == 2

    whole = store.get_execution('run-0')
    assert whole.detail_loaded and whole.summary.per_label == {'GET /items': {'count': 1000, 'p95_response_time': 300.0}}
    assert whole.ai_analysis.conclusion == 'ok' and whole.status == Status.COMPLETED

    # An outline from a scan must never be written back: it would erase the run's detail.
    outline = next(run for run in store.executions.values() if not run.detail_loaded)
    with pytest.raises(RuntimeError, match='headline'):
        await lifecycle.persist_execution(outline)
    assert (await DBStore().get_execution(outline.id))['summary_data']['per_label']

    # A restart loads outlines only, and still serves every run whole on request.
    await store.sync_from_dbstore()
    assert len(store.executions) == 6 and store.executions.whole_count() == 0
    assert all(not run.detail_loaded and run.summary.throughput == 80.0 for run in store.executions.values())
    assert store.get_execution('run-3').ai_analysis.overall_assessment == 'fine'
    whole.error_message = 'edited and saved'
    await lifecycle.persist_execution(whole)
    assert store.get_execution('run-0').error_message == 'edited and saved'


# ── Schema upkeep ──

@pytest.mark.asyncio
async def test_one_time_migrations_run_once_and_are_recorded(database):
    async with database.begin() as connection:
        rows = (await connection.exec_driver_sql('SELECT version FROM schema_migrations ORDER BY version')).all()
        assert [row[0] for row in rows] == [number for number, _, _ in db.MIGRATIONS]
        # A table a later version of the app might create under an old name must survive the next start:
        # the drop that once removed it has been applied and is not run again.
        await connection.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
    await db.init_db()
    async with database.begin() as connection:
        assert (await connection.exec_driver_sql('SELECT COUNT(*) FROM users')).scalar() == 0
        assert (await connection.exec_driver_sql('SELECT COUNT(*) FROM schema_migrations')).scalar() == len(db.MIGRATIONS)


@pytest.mark.asyncio
async def test_a_database_of_the_multi_user_version_is_cleaned_up_once(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'old.db'))
    monkeypatch.setattr(db, '_engine', engine)
    monkeypatch.setattr(db, '_async_session_factory', async_sessionmaker(engine, expire_on_commit=False))
    try:
        async with engine.begin() as connection:
            for statement in (
                'CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)',
                "INSERT INTO users VALUES (1, 'someone')",
                'CREATE TABLE projects (id VARCHAR(50) PRIMARY KEY, name VARCHAR(200) NOT NULL, description TEXT,'
                ' base_url VARCHAR(500), environment_data JSON, created_at DATETIME, updated_at DATETIME)',
                "INSERT INTO projects (id, name) VALUES ('p', 'Kept')",
                'CREATE TABLE environments (id VARCHAR(50) PRIMARY KEY, project_id VARCHAR(50), name VARCHAR(100))',
                'CREATE TABLE test_plans (id VARCHAR(50) PRIMARY KEY, project_id VARCHAR(50) NOT NULL,'
                ' name VARCHAR(200) NOT NULL, description TEXT, test_type VARCHAR(30), jmx_path VARCHAR(500),'
                ' jmx_content TEXT, test_config JSON, endpoints_count INTEGER, created_at DATETIME, updated_at DATETIME)',
                "INSERT INTO test_plans (id, project_id, name, jmx_path) VALUES ('plan', 'p', 'Plan', 'a.jmx')",
            ):
                await connection.exec_driver_sql(statement)
        await db.init_db()

        def shape(connection):
            inspector = inspect(connection)
            return set(inspector.get_table_names()), {c['name'] for c in inspector.get_columns('test_plans')}
        async with engine.begin() as connection:
            tables, plan_columns = await connection.run_sync(shape)
        assert 'users' not in tables and 'environments' not in tables
        assert 'jmx_content' not in plan_columns and 'version' in plan_columns
        store = DBStore()
        assert (await store.get_project('p'))['name'] == 'Kept'
        assert (await store.get_test_plan('plan'))['storage_version'] == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unused_storage_that_holds_data_is_kept(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'data.db'))
    monkeypatch.setattr(db, '_engine', engine)
    monkeypatch.setattr(db, '_async_session_factory', async_sessionmaker(engine, expire_on_commit=False))
    try:
        async with engine.begin() as connection:
            for statement in (
                'CREATE TABLE projects (id VARCHAR(50) PRIMARY KEY, name VARCHAR(200) NOT NULL)',
                "INSERT INTO projects VALUES ('p', 'P')",
                'CREATE TABLE environments (id VARCHAR(50) PRIMARY KEY, project_id VARCHAR(50), name VARCHAR(100))',
                "INSERT INTO environments VALUES ('e', 'p', 'staging')",
                'CREATE TABLE test_plans (id VARCHAR(50) PRIMARY KEY, project_id VARCHAR(50) NOT NULL,'
                ' name VARCHAR(200) NOT NULL, jmx_path VARCHAR(500), jmx_content TEXT, created_at DATETIME)',
                "INSERT INTO test_plans (id, project_id, name, jmx_content) VALUES ('plan', 'p', 'Plan', '<xml/>')",
            ):
                await connection.exec_driver_sql(statement)
        await db.init_db()
        async with engine.begin() as connection:
            assert (await connection.exec_driver_sql('SELECT name FROM environments')).scalar() == 'staging'
            assert (await connection.exec_driver_sql('SELECT jmx_content FROM test_plans')).scalar() == '<xml/>'
    finally:
        await engine.dispose()
