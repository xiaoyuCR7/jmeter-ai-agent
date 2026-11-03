"""Baseline and previous-run comparison with a temporary database and in-memory store."""
from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import db
from app.models.schemas import (DetailedTestSummary, ProjectInfo, TestConfig, TestExecutionInfo,
                                TestExecutionStatus, TestPlanInfo)
from app.services import baseline_compare as bc
from app.services.storage import store

T0 = datetime(2026, 10, 1, 9, 0)


def summary(p95=500, err=0.5, tps=200, order_p95=600, order_err=0.0):
    return DetailedTestSummary(
        total_requests=1000, avg_response_time=p95 / 2, min_response_time=5, max_response_time=p95 * 3,
        p95_response_time=p95, p99_response_time=p95 * 2, error_rate=err, throughput=tps,
        received_bytes_per_sec=1, sent_bytes_per_sec=1,
        per_label={'POST /order': {'count': 500, 'avg_response_time': order_p95 / 2, 'p95_response_time': order_p95,
                                   'error_rate': order_err},
                   'GET /item': {'count': 500, 'avg_response_time': 100, 'p95_response_time': 200, 'error_rate': 0}})


def test_transaction_regressions_rank_first_and_error_rate_uses_points():
    rows = bc.compare_transactions(summary(), summary(order_p95=900, order_err=1.5))
    assert rows[0]['label'] == 'POST /order' and rows[0]['is_regression']
    assert {(r['metric'], r['is_regression']) for r in rows if r['label'] == 'POST /order'} >= {
        ('p95_response_time', True), ('error_rate', True)}
    assert not any(r['label'] == 'GET /item' for r in rows)  # unchanged label is not reported


def test_improvement_is_reported_but_not_a_regression():
    rows = bc.compare_transactions(summary(order_p95=900), summary(order_p95=400))
    assert rows and not any(r['is_regression'] for r in rows)


def execution(eid, start, s, plan='plan-a', status=TestExecutionStatus.COMPLETED, environment='staging'):
    return TestExecutionInfo(id=eid, project_id='shop', test_plan_id=plan, status=status, jmx_path=f'{eid}.jmx',
                             start_time=start, end_time=start + timedelta(minutes=5), summary=s,
                             context_snapshot={'environment_id': environment})


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'state.db'))
    monkeypatch.setattr(db, '_engine', engine)
    monkeypatch.setattr(db, '_async_session_factory', async_sessionmaker(engine, expire_on_commit=False))
    await db.init_db()
    async with db.get_session_factory()() as session:
        session.add(db.ProjectModel(id='shop', name='Shop'))
        await session.flush()
        for plan in ('plan-a', 'plan-b'):
            session.add(db.TestPlanModel(id=plan, project_id='shop', name=plan, jmx_path=f'{plan}.jmx'))
        await session.flush()
        for eid in ('old', 'base', 'other-plan', 'failed', 'now'):
            session.add(db.ExecutionModel(id=eid, project_id='shop', test_plan_id='plan-b' if eid == 'other-plan' else 'plan-a',
                                          status='completed'))
        await session.commit()
    runs = {
        'old': execution('old', T0, summary(p95=450)),
        'base': execution('base', T0 + timedelta(hours=1), summary(p95=500)),
        'other-plan': execution('other-plan', T0 + timedelta(hours=2), summary(p95=100), plan='plan-b'),
        'failed': execution('failed', T0 + timedelta(hours=3), summary(p95=50), status=TestExecutionStatus.FAILED),
        'production': execution('production', T0 + timedelta(hours=3, minutes=30), summary(p95=90), environment='prod'),
        'now': execution('now', T0 + timedelta(hours=4), summary(p95=700, order_p95=1200)),
    }
    monkeypatch.setattr(store, '_executions', runs)
    monkeypatch.setattr(store, '_projects', {'shop': ProjectInfo(id='shop', name='Shop')})
    monkeypatch.setattr(store, '_test_plans', {'plan-a': TestPlanInfo(id='plan-a', project_id='shop', name='Checkout',
                                                                     jmx_path='plan-a.jmx', created_at=T0,
                                                                     test_config=TestConfig(test_type='concurrency'), endpoints_count=1)})
    yield runs
    await engine.dispose()


@pytest.mark.asyncio
async def test_previous_run_skips_other_plans_environments_failures_and_later_runs(env):
    result = await bc.evidence_for(env['now'], env['now'].summary)
    assert result['baseline'] is None
    assert result['previous']['execution_id'] == 'base'  # not 'failed', 'other-plan' or the production run
    assert result['previous']['verdict'] == 'regression'
    assert any(t['label'] == 'POST /order' and t['is_regression'] for t in result['previous']['transactions'])


@pytest.mark.asyncio
async def test_promoted_baseline_is_compared_and_survives_its_execution(env):
    from fastapi import FastAPI
    from app.api.baseline_routes import router
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        created = (await client.post('/api/v1/executions/old/promote-baseline')).json()
    assert created['name'].startswith('Checkout @ ') and created['is_active']
    assert created['metrics_snapshot']['p95_response_time'] == 450

    env.pop('old')  # the baseline must not depend on the execution still being cached
    result = await bc.evidence_for(env['now'], env['now'].summary)
    assert result['baseline']['execution_id'] == 'old'
    p95 = next(d for d in result['baseline']['deltas'] if d['metric'] == 'p95_response_time')
    assert (p95['baseline'], p95['target'], p95['is_regression']) == (450, 700, True)
    assert '与基线' in bc.format_for_prompt(result) and 'POST /order' in bc.format_for_prompt(result)


@pytest.mark.asyncio
async def test_plan_baseline_wins_over_project_baseline_and_self_is_noted(env):
    from app.services.db_store import db_store
    await db_store.create_baseline({'project_id': 'shop', 'name': 'project-wide', 'execution_id': None,
                                    'metrics_snapshot': bc.snapshot_of(summary(p95=300))})
    await db_store.create_baseline({'project_id': 'shop', 'test_plan_id': 'plan-a', 'name': 'plan',
                                    'execution_id': 'now', 'metrics_snapshot': bc.snapshot_of(env['now'].summary)})
    result = await bc.evidence_for(env['now'], env['now'].summary)
    assert result.get('is_baseline') and result['baseline'] is None


def test_prompt_without_history_says_so():
    assert '没有可对比' in bc.format_for_prompt({'baseline': None, 'previous': None})
