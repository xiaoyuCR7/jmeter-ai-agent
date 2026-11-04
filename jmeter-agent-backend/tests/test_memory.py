"""What the agent remembers across runs: comparable history, checkable predictions and lessons people saved."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.models.schemas import FactSheet, Hypothesis, HypothesisCategory, HypothesisStatus
from app.services.ai import analysis_context
from app.services.ai.memory import experience, prediction
from app.services.ai.memory.fact_ledger import FactLedger
from app.services.ai.memory.hypothesis_ledger import HypothesisLedger
from app.services.ai.memory.provenance import snapshot_scope

LESSON = '订单查询在库存同步时会锁表，P95 会升到 2 秒以上，避开整点再压测。'
JMX = ('<jmeterTestPlan><hashTree><ThreadGroup testname="users" enabled="true">'
       '<stringProp name="ThreadGroup.num_threads">{threads}</stringProp>'
       '<stringProp name="ThreadGroup.duration">60</stringProp></ThreadGroup></hashTree></jmeterTestPlan>')


def test_a_prediction_is_kept_only_in_a_shape_a_run_can_answer():
    labels = {'GET /orders'}
    assert prediction.normalize({'metric': 'p95_response_time', 'label': 'GET /orders', 'change': 'increase',
                                 'value': 30, 'threads': 12}, labels) == {
        'metric': 'p95', 'label': 'GET /orders', 'change': 'increase', 'value': 30.0, 'threads': 12}
    assert prediction.normalize({'metric': 'p95', 'label': 'GET /missing', 'change': 'below', 'value': 1}, labels) is None
    for broken in ({'metric': 'latency', 'change': 'below', 'value': 1}, {'metric': 'p95', 'change': 'soon', 'value': 1},
                   {'metric': 'p95', 'change': 'below', 'value': -1}, {'metric': 'p95', 'change': 'increase', 'value': 5000},
                   {'metric': 'p95', 'change': 'below', 'value': 1, 'threads': 2.5}, 'p95 below 1'):
        assert prediction.normalize(broken, labels) is None
    assert prediction.from_text('error_rate below 1%') == {'metric': 'error_rate', 'change': 'below', 'value': 1.0}
    assert prediction.from_text('把连接池翻倍后会好') is None
    assert prediction.describe({'metric': 'p95', 'label': 'GET /orders', 'change': 'increase', 'value': 30,
                                'threads': 12}) == 'GET /orders 的P95 上升至少 30%（线程数 12）'
    assert prediction.describe({'metric': 'throughput', 'change': 'above', 'value': 3.5}) == '整体吞吐量高于 3.5/s（负载不变）'


@pytest_asyncio.fixture
async def memory(tmp_path, monkeypatch):
    """Throwaway ledgers and knowledge base, wired in wherever memory is read."""
    from app.api import memory_routes
    facts = FactLedger(str(tmp_path / 'facts.db'))
    hypotheses = HypothesisLedger(str(tmp_path / 'hypotheses.db'))
    knowledge = experience.LessonStore(str(tmp_path / 'lessons.db'))
    for module in (analysis_context, memory_routes):
        monkeypatch.setattr(module, 'fact_ledger', facts)
        monkeypatch.setattr(module, 'hypothesis_ledger', hypotheses)
    monkeypatch.setattr(experience, 'lesson_store', knowledge)
    try:
        yield SimpleNamespace(facts=facts, hypotheses=hypotheses, knowledge=knowledge, tmp_path=tmp_path)
    finally:
        await facts.close()
        await hypotheses.close()


def run(tmp_path, execution_id='now', plan='plan-a', threads=5, labels=('GET /orders',)):
    jmx = tmp_path / f'{execution_id}.jmx'
    jmx.write_text(JMX.format(threads=threads), encoding='utf-8')
    snapshot = {'project_id': 'p', 'test_plan_id': plan, 'environment_id': 'e', 'environment_fingerprint': 'env',
                'test_config': {'think_time_ms': 1000, 'concurrent_users': threads}}
    return SimpleNamespace(id=execution_id, project_id='p', test_plan_id=plan, jmx_path=str(jmx),
                           context_snapshot=snapshot, summary=SimpleNamespace(per_label={label: {} for label in labels}))


def fact(execution_id, plan, threads, p95, scope=''):
    return FactSheet(execution_id=execution_id, module=plan, version='unknown', project_id='p',
                     environment_fingerprint='env', load={'threads': threads, 'think_time_ms': 1000},
                     context_scope=scope, total_requests=100, throughput=7.5, percentiles={'p95': p95},
                     per_label={'GET /orders': {'count': 50, 'p95_response_time': p95 + 11, 'avg_response_time': 90,
                                                'error_rate': 0.4, 'throughput': 3.25}})


@pytest.mark.asyncio
async def test_a_report_recalls_each_kind_of_memory_in_order_of_comparability(memory):
    current = run(memory.tmp_path)
    for item in (fact('same-config', 'plan-a', 5, 301, scope=snapshot_scope(current.context_snapshot)),
                 fact('more-users', 'plan-a', 20, 802),
                 fact('other-plan', 'plan-b', 8, 455)):
        assert await memory.facts.save(item)
    await memory.hypotheses.add(Hypothesis(
        hypothesis_id='h', source_execution_id='other-plan', module='plan-b', project_id='p',
        category=HypothesisCategory.BOTTLENECK, statement='订单接口在排队',
        prediction={'metric': 'p95', 'label': 'GET /orders', 'change': 'increase', 'value': 30, 'threads': 12}))
    saved = experience.save('p', 'known_issue', '订单锁表', LESSON, ['GET /orders'], 'other-plan', 'plan-b')
    experience.save('p', 'module_standard', '只管支付', '支付接口 P95 不超过 800 毫秒，错误率不超过 0.1%。', ['POST /pay'])

    remembered = await analysis_context.recall(current)
    text = remembered.text
    order = [text.index(heading) for heading in ('同样配置', '其它负载下', '其它计划里的相同接口', '历史假设', '团队确认过的经验')]
    assert order == sorted(order)
    assert '[same-con] 各接口\n- GET /orders：P95=312' in text
    assert '线程数 20' in text and 'GET /orders：计划' in text and '（线程数 12）' in text and '订单锁表' in text
    assert '只管支付' not in text  # A lesson about a request this run does not have stays out.
    assert {301.0, 802.0, 466.0, 3.25} <= set(remembered.numbers)
    assert remembered.counts == {'same_config': 1, 'other_loads': 1, 'other_plans': 1, 'hypotheses': 1, 'experiences': 1}
    assert memory.knowledge.get(saved.id).metadata['cited_count'] == 1


@pytest.mark.asyncio
async def test_nothing_remembered_is_not_an_error(memory, monkeypatch):
    assert (await analysis_context.recall(run(memory.tmp_path))).text == ''
    monkeypatch.setattr(memory.facts, 'get_project_facts', AsyncMock(side_effect=OSError('disk')))
    assert (await analysis_context.recall(run(memory.tmp_path))).counts['same_config'] == 0


def test_a_lesson_saved_twice_is_one_lesson_and_a_retired_one_stays_out(memory):
    first = experience.save('p', 'known_issue', 'Orders  lock', LESSON, ['GET /orders'])
    again = experience.save('p', 'known_issue', 'orders lock', LESSON + ' 已复现两次。', ['GET /orders'])
    assert again.id == first.id and len(experience.for_project('p')) == 1 and '复现两次' in again.content
    project_wide = experience.save('p', 'module_standard', '全局标准', '所有接口错误率不超过 1%，P95 不超过 1 秒。', [])
    assert [doc.id for doc in experience.relevant('p', {'GET /orders'})] == [first.id, project_wide.id]
    experience.set_status(first.id, 'retired', '已修复')
    assert [doc.id for doc in experience.relevant('p', {'GET /orders'})] == [project_wide.id]
    assert experience.view(memory.knowledge.get(first.id))['retired_reason'] == '已修复'
    experience.set_status(first.id, 'active')
    assert len(experience.relevant('p', {'GET /orders'})) == 2


def test_an_old_lesson_is_marked_for_review_but_being_read_is_not_a_review(memory):
    doc = experience.save('p', 'known_issue', '旧经验', LESSON, [])
    old = (datetime.now() - timedelta(days=120)).isoformat()
    memory.knowledge.update(doc.id, metadata={**doc.metadata, 'reviewed_at': old})
    experience.cite([memory.knowledge.get(doc.id)])
    stored = memory.knowledge.get(doc.id)
    assert experience.needs_review(stored) and '可能已过时' in experience.format_for_prompt([stored])


@pytest.mark.asyncio
async def test_a_recorded_run_carries_its_project_and_load_and_checks_the_projects_hypotheses(memory, monkeypatch, tmp_path):
    from app.services import post_run
    from app.services.ai import memory as ledgers
    from app.services.ai.memory import provenance
    from tests.test_analysis_quality import HEADER, row, summary_of
    current = run(tmp_path, execution_id='run-1', threads=7)
    current.report_dir, current.summary, current.ai_analysis = str(tmp_path), summary_of(), None
    (tmp_path / 'result.jtl').write_text(HEADER + row(1000, 50), encoding='utf-8')
    monkeypatch.setattr(ledgers, 'fact_ledger', memory.facts)
    monkeypatch.setattr(ledgers, 'hypothesis_ledger', SimpleNamespace(
        evaluate_project_hypotheses=AsyncMock(), expire=AsyncMock(), add=AsyncMock(),
        list_for_project=AsyncMock(return_value=[])))
    monkeypatch.setattr(provenance, 'load_execution_scope', AsyncMock(return_value='scope-1'))
    await post_run.record_facts(current)
    saved = await memory.facts.load('run-1')
    assert (saved.project_id, saved.environment_fingerprint, saved.load['threads']) == ('p', 'env', 7)
    ledgers.hypothesis_ledger.evaluate_project_hypotheses.assert_awaited_once_with('p', 'run-1')
    ledgers.hypothesis_ledger.expire.assert_awaited_once_with('p', post_run.HYPOTHESIS_MAX_AGE_DAYS)


async def record_report(execution_id, hypotheses, plan='plan'):
    from app.models.schemas import ReportAnalysis
    from app.services import post_run
    analysis = ReportAnalysis(overall_assessment='a', conclusion='b', hypotheses=hypotheses)
    await post_run.record_hypotheses(SimpleNamespace(id=execution_id, ai_analysis=analysis), SimpleNamespace(
        module=plan, version='unknown', test_type='baseline', per_label={'GET /orders': {}}, project_id='p'))


@pytest.mark.asyncio
async def test_report_predictions_are_stored_when_checkable(memory, monkeypatch):
    from app.services.ai import memory as ledgers
    monkeypatch.setattr(ledgers, 'hypothesis_ledger', memory.hypotheses)
    await record_report('run', [
        {'statement': '订单排队', 'category': 'bottleneck', 'testable_prediction': '加到 12 线程 P95 涨 30%',
         'prediction': {'metric': 'p95', 'label': 'GET /orders', 'change': 'increase', 'value': 30, 'threads': 12}},
        {'statement': '名字写错', 'category': 'risk',
         'prediction': {'metric': 'p95', 'label': 'GET /order', 'change': 'increase', 'value': 30}}])
    stored = {hyp.statement: hyp for hyp in await memory.hypotheses.list_for_project('p')}
    assert stored['订单排队'].prediction['threads'] == 12
    assert stored['名字写错'].prediction is None  # Waits for a person to decide it.


@pytest.mark.asyncio
async def test_a_restated_prediction_replaces_the_open_one_of_the_same_plan(memory, monkeypatch):
    from app.services.ai import memory as ledgers
    monkeypatch.setattr(ledgers, 'hypothesis_ledger', memory.hypotheses)
    claim = {'metric': 'throughput', 'change': 'above', 'value': 3.5, 'threads': 6}
    await record_report('first', [{'statement': '吞吐受 think time 限制', 'category': 'risk', 'prediction': claim}])
    await record_report('other-plan', [{'statement': '另一个计划', 'category': 'risk', 'prediction': claim}], plan='b')
    await record_report('second', [{'statement': '吞吐由 3 线程和 1 秒思考时间决定', 'category': 'risk',
                                    'prediction': {**claim, 'value': 4}}])
    status = {hyp.statement: hyp for hyp in await memory.hypotheses.list_for_project('p')}
    assert status['吞吐受 think time 限制'].status == HypothesisStatus.DEPRECATED
    assert '运行 second 的报告提出了同样的预测' in status['吞吐受 think time 限制'].verification_notes
    assert status['吞吐由 3 线程和 1 秒思考时间决定'].status == HypothesisStatus.UNTESTED
    assert status['另一个计划'].status == HypothesisStatus.UNTESTED


@pytest.mark.asyncio
async def test_a_report_retires_the_open_hypothesis_it_says_it_restates(memory, monkeypatch):
    from app.services.ai import memory as ledgers
    monkeypatch.setattr(ledgers, 'hypothesis_ledger', memory.hypotheses)
    await record_report('first', [{'statement': '开头尖峰来自建连', 'category': 'root_cause'},
                                  {'statement': '吞吐受 think time 限制', 'category': 'risk'}])
    old = {hyp.statement: hyp for hyp in await memory.hypotheses.list_for_project('p')}
    prompt = HypothesisLedger.format_for_prompt(list(old.values()))
    ref = old['开头尖峰来自建连'].hypothesis_id[:8]
    assert f'编号 {ref}：开头尖峰来自建连' in prompt
    await record_report('second', [{'statement': '开头尖峰来自 login 的 3 次建连', 'category': 'root_cause',
                                    'replaces': ref}, {'statement': '编号写错', 'category': 'risk', 'replaces': 'nope'}])
    status = {hyp.statement: hyp.status for hyp in await memory.hypotheses.list_for_project('p')}
    assert status['开头尖峰来自建连'] == HypothesisStatus.DEPRECATED
    assert status['吞吐受 think time 限制'] == status['开头尖峰来自 login 的 3 次建连'] == HypothesisStatus.UNTESTED


def test_a_prediction_that_needs_a_change_first_is_left_to_a_person():
    conditional = prediction.normalize({'metric': 'p99', 'change': 'below', 'value': 450, 'after': '加连接预热'}, set())
    assert conditional['after'] == '加连接预热' and not prediction.checkable(conditional)
    assert prediction.describe(conditional).endswith('，前提：加连接预热')
    source = fact('before', 'plan-a', 5, 300)
    rerun = fact('plain-rerun', 'plan-a', 5, 1400)
    assert prediction.check({**conditional, 'metric': 'p95'}, source, rerun) is None
    # The same claim without a change to make first is settled by the plain rerun.
    assert prediction.check({'metric': 'p95', 'change': 'below', 'value': 450}, source, rerun)[0] is False


@pytest.mark.asyncio
async def test_earlier_runs_join_project_history_once(memory, monkeypatch):
    from app.models.schemas import TestExecutionStatus
    from app.services import post_run
    from app.services.ai import memory as ledgers
    current = run(memory.tmp_path, execution_id='old-run', threads=9)
    current.status = TestExecutionStatus.COMPLETED
    assert await memory.facts.save(FactSheet(execution_id='old-run', module='plan-a', total_requests=1))
    await memory.hypotheses.add(Hypothesis(hypothesis_id='h', source_execution_id='old-run', module='plan-a',
                                           statement='legacy'))
    monkeypatch.setattr(ledgers, 'fact_ledger', memory.facts)
    monkeypatch.setattr(ledgers, 'hypothesis_ledger', memory.hypotheses)
    monkeypatch.setattr(type(post_run.store), 'executions', property(lambda self: {'old-run': current}))
    await post_run.backfill_memory()
    backfilled = await memory.facts.load('old-run')
    assert (backfilled.project_id, backfilled.load['threads']) == ('p', 9)
    assert (await memory.hypotheses.get('h')).project_id == 'p'
    assert not await memory.facts.annotate('old-run', only_missing=True, project_id='other')


@pytest_asyncio.fixture
async def api(memory, monkeypatch):
    from app.api import memory_routes
    current = run(memory.tmp_path)
    monkeypatch.setattr(memory_routes.store, 'get_execution', lambda execution_id: current if execution_id == 'now' else None)
    app = FastAPI()
    app.include_router(memory_routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        yield client


@pytest.mark.asyncio
async def test_a_lesson_is_saved_from_a_report_only_about_the_runs_requests(api, memory):
    body = {'kind': 'known_issue', 'title': '订单锁表', 'content': LESSON, 'labels': ['GET /orders']}
    saved = await api.post('/executions/now/experiences', json=body)
    assert saved.status_code == 200 and saved.json()['labels'] == ['GET /orders']
    assert (await api.post('/executions/now/experiences', json=body | {'labels': ['GET /nope']})).status_code == 400
    assert (await api.post('/executions/now/experiences', json=body | {'content': '太短'})).status_code == 422
    assert (await api.post('/executions/gone/experiences', json=body)).status_code == 404
    lesson = saved.json()['id']
    retired = await api.patch(f'/experiences/{lesson}', json={'status': 'retired', 'reason': '已修复'})
    assert retired.json()['status'] == 'retired'
    listed = (await api.get('/projects/p/experiences')).json()
    assert [item['status'] for item in listed] == ['retired']


@pytest.mark.asyncio
async def test_the_report_shows_and_settles_the_hypotheses_about_its_run(api, memory):
    await memory.hypotheses.add(Hypothesis(hypothesis_id='about-plan', source_execution_id='x', module='plan-a',
                                           project_id='p', statement='连接池太小', testable_prediction='加大后会好'))
    await memory.hypotheses.add(Hypothesis(hypothesis_id='elsewhere', source_execution_id='y', module='plan-z',
                                           project_id='p', statement='无关接口'))
    shown = (await api.get('/executions/now/memory')).json()
    assert [item['id'] for item in shown['hypotheses']] == ['about-plan'] and not shown['hypotheses'][0]['checkable']
    decided = await api.patch('/hypotheses/about-plan', json={'status': 'refuted', 'note': '加大后没变化'})
    assert decided.json()['status'] == 'refuted' and decided.json()['notes'] == '人工判定：加大后没变化'
    assert (await api.patch('/hypotheses/missing', json={'status': 'confirmed'})).status_code == 404
    overview = (await api.get('/memory/overview', params={'project_id': 'p'})).json()
    assert overview['hypotheses'] == {'refuted': 1, 'untested': 1}
    assert overview['recently_refuted'][0]['id'] == 'about-plan'


@pytest.mark.asyncio
async def test_a_follow_up_question_sees_the_same_recall_as_the_report(monkeypatch):
    import importlib
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    agent = object.__new__(module.AnalysisAgent)
    agent._build_fact_sheet = AsyncMock(return_value=FactSheet(execution_id='now', module='plan-a', total_requests=5))
    agent._collect_evidence = AsyncMock(return_value=None)
    agent.converse = AsyncMock(return_value=SimpleNamespace(success=True, output='上次 P95 是 777ms', error=None,
                                                       plan=[], iterations=1))
    monkeypatch.setattr('app.services.ai.memory.provenance.load_execution_scope', AsyncMock(return_value=''))
    monkeypatch.setattr('app.services.storage.store.get_execution', lambda execution_id: SimpleNamespace(id=execution_id))
    monkeypatch.setattr(module.analysis_context, 'recall', AsyncMock(return_value=analysis_context.Recall(
        text='\n\n## 这个计划最近的运行 X', numbers=[777.0], counts={'same_config': 1})))
    result = await agent.analyze_with_rag(execution_id='now', module='plan-a', user_task='和上次比呢？')
    assert '## 这个计划最近的运行 X' in agent.converse.call_args.args[0]
    assert result['validation']['mismatches'] == [] and result['recall'] == {'same_config': 1}


def test_a_report_does_not_see_its_own_hypotheses_as_history():
    own = Hypothesis(hypothesis_id='own', source_execution_id='now', module='plan-a', statement='mine')
    earlier = Hypothesis(hypothesis_id='earlier', source_execution_id='before', module='plan-a', statement='theirs')
    about_request = Hypothesis(hypothesis_id='request', source_execution_id='x', module='plan-b', statement='orders',
                               prediction={'metric': 'p95', 'label': 'GET /orders', 'change': 'below', 'value': 1})
    unrelated = Hypothesis(hypothesis_id='other', source_execution_id='y', module='plan-b', statement='pay')
    chosen = analysis_context.relevant_hypotheses([own, earlier, about_request, unrelated], 'plan-a', {'GET /orders'}, 'now')
    assert [hyp.hypothesis_id for hyp in chosen] == ['earlier', 'request']


def test_other_plans_are_spread_across_requests():
    facts = [fact(f'run-{i}', 'plan-b', 5, 100 + i) for i in range(5)]
    for item in facts:
        item.per_label['GET /pay'] = {'count': 3, 'p95_response_time': 50}
    text, _ = analysis_context.same_requests(facts, {'GET /orders', 'GET /pay'}, {})
    assert text.count('GET /orders：') == 2 and text.count('GET /pay：') == 2


@pytest.mark.asyncio
async def test_deleting_a_project_removes_its_reports_and_memory(memory, monkeypatch, tmp_path):
    from app.api import project_routes
    from app.services.ai import memory as ledgers
    monkeypatch.setattr(ledgers, 'fact_ledger', memory.facts)
    monkeypatch.setattr(ledgers, 'hypothesis_ledger', memory.hypotheses)
    reports = tmp_path / 'reports'
    (reports / 'run-1').mkdir(parents=True)
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    monkeypatch.setattr(type(project_routes.settings), 'REPORTS_DIR', property(lambda self: str(reports)))
    assert await memory.facts.save(fact('run-1', 'plan-a', 5, 300))
    # Recorded before facts carried their project: found by the run instead.
    assert await memory.facts.save(FactSheet(execution_id='legacy', module='plan-a', total_requests=1))
    assert await memory.facts.save(FactSheet(execution_id='keep', module='plan-z', project_id='other', total_requests=1))
    await memory.hypotheses.add(Hypothesis(hypothesis_id='h', source_execution_id='legacy', module='plan-a', statement='s'))
    await memory.hypotheses.add(Hypothesis(hypothesis_id='k', source_execution_id='keep', module='plan-z',
                                           project_id='other', statement='s'))
    experience.save('p', 'known_issue', '订单锁表', LESSON, [])
    kept = experience.save('other', 'known_issue', '别的项目', LESSON, [])

    await project_routes._forget_project('p', [SimpleNamespace(id='run-1', report_dir=str(reports / 'run-1')),
                                       SimpleNamespace(id='legacy', report_dir=str(outside)),
                                       SimpleNamespace(id='none', report_dir=None)])
    assert not (reports / 'run-1').exists() and outside.exists() and reports.exists()
    assert [await memory.facts.load(run_id) is None for run_id in ('run-1', 'legacy', 'keep')] == [True, True, False]
    assert await memory.hypotheses.get('h') is None and await memory.hypotheses.get('k') is not None
    assert experience.for_project('p', include_retired=True) == [] and experience.for_project('other')[0].id == kept.id


@pytest.mark.asyncio
async def test_memory_from_older_versions_moves_into_the_app_database_once(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'BASE_DIR', str(tmp_path))
    old = FactLedger(str(tmp_path / 'facts.db'))
    assert await old.save(fact('earlier', 'plan-a', 5, 300))
    await old.close()
    ledger = FactLedger(str(tmp_path / 'app.db'))
    ledger._adopted = False  # As the shared ledger, which opens the app's database by default.
    try:
        assert (await ledger.load('earlier')).percentiles['p95'] == 300
        assert not (tmp_path / 'facts.db').exists() and (tmp_path / 'facts.db.migrated').exists()
    finally:
        await ledger.close()


def test_memory_shares_the_app_database_file(monkeypatch):
    import os
    from app.core.config import settings
    from app.services.ai.memory.storage import memory_db_path
    monkeypatch.setattr(settings, 'DATABASE_URL', 'sqlite+aiosqlite:///./data/app.db')
    assert memory_db_path() == os.path.abspath('./data/app.db')
    monkeypatch.setattr(settings, 'DATABASE_URL', 'postgresql+asyncpg://db/app')
    assert memory_db_path() == os.path.join(settings.BASE_DIR, 'memory.db')
