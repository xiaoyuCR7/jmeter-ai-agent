"""Requirements: extraction keeps traceable quotes; verdicts are deterministic."""
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import db
from app.models.schemas import DetailedTestSummary
from app.services import requirements as req
from app.services.storage import store


def summary(**overrides):
    base = dict(total_requests=1000, avg_response_time=180, min_response_time=10, max_response_time=2400,
                p95_response_time=650, p99_response_time=1200, error_rate=0.4, throughput=210,
                received_bytes_per_sec=1, sent_bytes_per_sec=1,
                percentiles={'p50': 150, 'p90': 480, 'p95': 650, 'p99': 1200},
                per_label={
                    'POST /api/login': {'count': 300, 'avg_response_time': 90, 'p90_response_time': 150,
                                        'p95_response_time': 200, 'p99_response_time': 400, 'error_rate': 0.0,
                                        'throughput': 60, 'max_response_time': 900},
                    'POST /api/order/create': {'count': 400, 'avg_response_time': 320, 'p90_response_time': 700,
                                               'p95_response_time': 900, 'p99_response_time': 1800, 'error_rate': 1.2,
                                               'throughput': 80, 'max_response_time': 2400},
                    'GET /api/order/list': {'count': 300, 'avg_response_time': 120, 'p90_response_time': 300,
                                            'p95_response_time': 420, 'p99_response_time': 800, 'error_rate': 0.0,
                                            'throughput': 70, 'max_response_time': 1000},
                })
    base.update(overrides)
    return DetailedTestSummary(**base)


def item(**fields):
    return req.RequirementItem(**{'id': 'R1', 'metric': 'p95', 'operator': 'lte', 'value': 1000, **fields})


def test_global_requirements_pass_and_fail_with_gap():
    result = req.evaluate(req.PerformanceRequirements(items=[
        item(id='R1', metric='p95', value=1000),
        item(id='R2', metric='error_rate', operator='lt', value=0.1),
        item(id='R3', metric='throughput', operator='gte', value=200),
    ]), summary())
    by_id = {v.id: v for v in result.items}
    assert (by_id['R1'].status, by_id['R2'].status, by_id['R3'].status) == ('pass', 'fail', 'pass')
    assert by_id['R2'].actual == 0.4 and by_id['R2'].gap_pct == 300.0
    assert (result.verdict, result.passed, result.failed) == ('fail', 2, 1)


def test_transaction_requirement_judges_every_matching_label_by_the_worst():
    result = req.evaluate(req.PerformanceRequirements(items=[
        item(scope='transaction', transaction='order', metric='p95', value=500)]), summary())
    verdict = result.items[0]
    assert set(verdict.matched_labels) == {'POST /api/order/create', 'GET /api/order/list'}
    assert verdict.actual == 900 and verdict.status == 'fail'


def test_exact_label_match_wins_over_substring_and_p90_is_supported():
    result = req.evaluate(req.PerformanceRequirements(items=[
        item(scope='transaction', transaction='POST /api/login', metric='p90', value=200)]), summary())
    assert result.items[0].matched_labels == ['POST /api/login']
    assert result.items[0].actual == 150 and result.verdict == 'pass'


def test_unmatched_transaction_or_empty_run_is_missing_not_passing():
    requirements = req.PerformanceRequirements(items=[item(scope='transaction', transaction='checkout')])
    assert req.evaluate(requirements, summary()).verdict == 'incomplete'
    empty = req.evaluate(req.PerformanceRequirements(items=[item()]), summary(total_requests=0))
    assert empty.items[0].status == 'missing'


SAMPLER_JMX = """<jmeterTestPlan><hashTree><TestPlan testclass="TestPlan" testname="P"/><hashTree>
  <ThreadGroup testclass="ThreadGroup" testname="Users"/><hashTree>
    <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="GetQuotes">
      <stringProp name="HTTPSampler.path">/api/quotes?limit=5</stringProp>
      <stringProp name="HTTPSampler.method">GET</stringProp></HTTPSamplerProxy><hashTree/>
    <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="RecommendPizza">
      <stringProp name="HTTPSampler.path">/api/pizza/</stringProp>
      <stringProp name="HTTPSampler.method">POST</stringProp></HTTPSamplerProxy><hashTree/>
  </hashTree></hashTree></hashTree></jmeterTestPlan>"""


def sampler_summary():
    stats = lambda p95: {'count': 100, 'avg_response_time': p95 / 2, 'p90_response_time': p95, 'p95_response_time': p95,
                         'p99_response_time': p95, 'error_rate': 0.0, 'throughput': 10, 'max_response_time': p95}
    return summary(per_label={'GetQuotes': stats(916), 'RecommendPizza': stats(657)})


def test_endpoint_requirements_find_samplers_through_the_jmx_that_ran(tmp_path):
    # Documents name endpoints, JMeter reports sampler names; the run's JMX links the two.
    jmx = tmp_path / 'snapshot.jmx'
    jmx.write_text(SAMPLER_JMX, encoding='utf-8')
    requirements = req.PerformanceRequirements(items=[
        item(id='R1', scope='transaction', transaction='POST /api/pizza', value=800),
        item(id='R2', scope='transaction', transaction='get /api/quotes', value=500),
        item(id='R3', scope='transaction', transaction='/api/quotes', value=1000),
    ])
    result = req.evaluate(requirements, sampler_summary(), req.sampler_routes(str(jmx)))
    by_id = {v.id: v for v in result.items}
    assert (by_id['R1'].matched_labels, by_id['R1'].status) == (['RecommendPizza'], 'pass')
    assert (by_id['R2'].matched_labels, by_id['R2'].actual, by_id['R2'].status) == (['GetQuotes'], 916, 'fail')
    assert by_id['R3'].matched_labels == ['GetQuotes']
    # Without the JMX the same requirements cannot be judged, rather than guessed.
    assert req.evaluate(requirements, sampler_summary()).missing == 3


def test_route_matching_respects_the_method_and_tolerates_a_missing_jmx(tmp_path):
    jmx = tmp_path / 'snapshot.jmx'
    jmx.write_text(SAMPLER_JMX, encoding='utf-8')
    wrong_method = req.PerformanceRequirements(items=[item(scope='transaction', transaction='GET /api/pizza')])
    assert req.evaluate(wrong_method, sampler_summary(), req.sampler_routes(str(jmx))).items[0].status == 'missing'
    assert req.sampler_routes(str(tmp_path / 'gone.jmx')) == {} and req.sampler_routes(None) == {}


def test_no_requirements_prompt_forbids_claiming_compliance():
    evaluation = req.evaluate(None, summary())
    assert evaluation.verdict == 'none'
    assert '不要声称达标' in req.format_for_prompt(evaluation)


def test_prompt_cites_ids_values_and_quotes():
    evaluation = req.evaluate(req.PerformanceRequirements(items=[
        item(id='R7', source='下单接口 95% 响应时间不超过 1 秒')]), summary())
    text = req.format_for_prompt(evaluation)
    assert 'R7' in text and '650' in text and '下单接口 95% 响应时间不超过 1 秒' in text


@pytest.mark.asyncio
async def test_extraction_verifies_quotes_and_drops_malformed_items():
    document = '性能需求：\n1. 登录接口平均响应时间不超过 200ms。\n2. 系统错误率低于 0.1%。\n并发用户 500，持续 30 分钟。'
    adapter = AsyncMock()
    adapter.chat.return_value = '''```json
{"items": [
  {"id": "R1", "scope": "transaction", "transaction": "login", "metric": "avg_response_time",
   "operator": "lte", "value": 200, "source": "登录接口平均响应时间不超过 200ms。"},
  {"id": "R2", "scope": "global", "metric": "error_rate", "operator": "lt", "value": 0.1,
   "source": "系统错误率必须低于千分之一"},
  {"id": "R3", "metric": "latency", "operator": "lte", "value": 5}
 ],
 "load_profile": {"concurrency": 500, "duration_seconds": 1800, "source": "并发用户 500，持续 30 分钟。"},
 "assumptions": ["未说明错误率统计口径"]}
```'''
    result = await req.extract_requirements(document, adapter, source_name='spec.md')
    assert [i.id for i in result.items] == ['R1', 'R2']
    assert result.items[0].source_verified is True
    assert result.items[1].source_verified is False  # paraphrased, not quoted
    assert result.load_profile.concurrency == 500 and result.source_name == 'spec.md'
    assert result.assumptions == ['未说明错误率统计口径']


@pytest.mark.asyncio
async def test_extraction_rejects_empty_documents():
    with pytest.raises(ValueError):
        await req.extract_requirements('   ', AsyncMock())


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from app.api.requirement_routes import router
    from app.models.schemas import ProjectInfo
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'state.db'))
    monkeypatch.setattr(db, '_engine', engine)
    monkeypatch.setattr(db, '_async_session_factory', async_sessionmaker(engine, expire_on_commit=False))
    await db.init_db()
    async with db.get_session_factory()() as session:
        session.add(db.ProjectModel(id='shop', name='Shop'))
        await session.commit()
    monkeypatch.setattr(store, '_projects', {'shop': ProjectInfo(id='shop', name='Shop')})
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client
    await engine.dispose()


@pytest.mark.asyncio
async def test_requirements_round_trip_and_project_scoping(api):
    assert (await api.get('/api/v1/projects/shop/requirements')).json() is None
    body = {'items': [{'id': 'R1', 'metric': 'p95', 'operator': 'lte', 'value': 800, 'source': 'P95 ≤ 800ms'}],
            'load_profile': {'concurrency': 200}}
    saved = (await api.put('/api/v1/projects/shop/requirements', json=body)).json()
    assert saved['updated_at'] and saved['items'][0]['value'] == 800
    assert (await api.get('/api/v1/projects/shop/requirements')).json()['load_profile']['concurrency'] == 200
    assert (await api.get('/api/v1/projects/missing/requirements')).status_code == 404
    duplicate = {'items': [body['items'][0], body['items'][0]]}
    assert (await api.put('/api/v1/projects/shop/requirements', json=duplicate)).status_code == 422
    assert (await api.delete('/api/v1/projects/shop/requirements')).json() == {'deleted': True}
    assert (await api.get('/api/v1/projects/shop/requirements')).json() is None


@pytest.mark.asyncio
async def test_execution_list_rows_carry_plan_name_and_requirement_verdict(api, monkeypatch):
    from datetime import datetime
    from app.api.routes import list_executions
    from app.models.schemas import TestExecutionInfo, TestExecutionStatus
    body = {'items': [item(id='R1', value=500).model_dump()]}
    assert (await api.put('/api/v1/projects/shop/requirements', json=body)).status_code == 200
    plan = type('Plan', (), {'name': 'Checkout load'})()
    monkeypatch.setattr(store, '_test_plans', {'plan-1': plan})

    def run(run_id, start, run_summary):
        return TestExecutionInfo(id=run_id, project_id='shop', test_plan_id='plan-1', jmx_path='x.jmx',
                                 status=TestExecutionStatus.COMPLETED, start_time=start, summary=run_summary)
    monkeypatch.setattr(store, '_executions', {
        'old': run('old', datetime(2026, 1, 1), summary(p95_response_time=400)),
        'new': run('new', datetime(2026, 2, 1), summary()),
        'pending': run('pending', None, None),
    })

    rows = (await list_executions(project_id='shop', offset=0, limit=50))['executions']
    assert [r['id'] for r in rows] == ['new', 'old', 'pending']
    assert rows[0]['test_plan_name'] == 'Checkout load' and rows[0]['project_name'] == 'Shop'
    assert rows[0]['requirements']['verdict'] == 'fail' and rows[1]['requirements']['verdict'] == 'pass'
    assert rows[0]['metrics']['p95_response_time'] == 650
    assert rows[2]['metrics'] is None and rows[2]['requirements'] is None
    assert rows[0]['has_ai_analysis'] is False


def test_requirements_accept_the_names_and_symbols_models_send():
    # A save with load_profile.concurrent_users once kept every field except the concurrency.
    saved = req.PerformanceRequirements.model_validate({
        'items': [{'id': 'R1', 'metric': 'p95', 'operator': '<=', 'value': 800},
                  {'id': 'R2', 'metric': 'throughput', 'operator': '≥', 'value': 5}],
        'load_profile': {'concurrent_users': 10, 'ramp_up_seconds': 10, 'duration_seconds': 60}})
    assert [i.operator for i in saved.items] == ['lte', 'gte']
    assert saved.load_profile.concurrency == 10
    assert saved.model_dump()['load_profile']['concurrency'] == 10
    assert req.LoadProfile.model_validate({'concurrency': 5}).concurrency == 5
