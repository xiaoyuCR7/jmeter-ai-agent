"""Every finished run gets the same report and history, and a report can be questioned afterwards."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import settings
from app.models.schemas import (
    DetailedTestSummary, FactSheet, ReportAnalysis, TestExecutionInfo as Execution, TestExecutionStatus as Status,
)
from app.services import post_run, run_diagnostics
from app.services.ai import analysis_context
from app.services.ai.agent.tools import jmeter as jmeter_tools
from app.services.ai.agent.types import AgentResult
from app.services.evidence import _load_generator, _format_load_generator
from app.services.jmeter.test_scheduler import SchedulerDecision


def summary():
    return DetailedTestSummary(total_requests=10, avg_response_time=100, min_response_time=1, max_response_time=200,
                               error_rate=0, throughput=5, received_bytes_per_sec=1, sent_bytes_per_sec=1)


def completed(tmp_path, **overrides):
    values = dict(id='run', project_id='p', status=Status.COMPLETED, summary=summary(), test_plan_id='plan',
                  report_dir=str(tmp_path), context_snapshot={'test_config': {'test_type': 'baseline'}})
    return SimpleNamespace(**values | overrides)


@pytest.fixture
def steps(monkeypatch):
    """Records which post-run steps ran."""
    from app.services.ai.rag.archiver import rag_archiver
    ran = []
    async def analyze(execution, ai_model):
        ran.append(('analysis', ai_model))
    async def record(execution):
        ran.append('facts')
    monkeypatch.setattr(post_run, '_write_analysis', analyze)
    monkeypatch.setattr(post_run, 'record_facts', record)
    monkeypatch.setattr(rag_archiver, 'archive_raw_report', lambda **kwargs: ran.append('archive'))
    return ran


@pytest.mark.asyncio
async def test_a_finished_run_gets_its_analysis_archive_and_history(steps, tmp_path):
    await post_run.finish_run(completed(tmp_path), 'model-a')
    assert steps == [('analysis', 'model-a'), 'archive', 'facts']


@pytest.mark.asyncio
async def test_turning_analysis_off_keeps_the_measured_history(steps, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_ANALYSIS_ENABLED', False)
    await post_run.finish_run(completed(tmp_path))
    assert steps == ['archive', 'facts']


@pytest.mark.asyncio
@pytest.mark.parametrize('overrides', [{'status': Status.FAILED}, {'status': Status.CANCELLED}, {'summary': None}])
async def test_a_run_without_results_gets_no_post_run_work(steps, tmp_path, overrides):
    await post_run.finish_run(completed(tmp_path, **overrides))
    assert steps == []


@pytest.mark.asyncio
async def test_archiving_runs_off_the_event_loop(steps, tmp_path, monkeypatch):
    import threading
    from app.services.ai.rag.archiver import rag_archiver
    seen = []
    monkeypatch.setattr(rag_archiver, 'archive_raw_report', lambda **kwargs: seen.append(threading.current_thread()))
    await post_run.finish_run(completed(tmp_path))
    assert seen and seen[0] is not threading.current_thread()


@pytest.mark.parametrize('decision', [SchedulerDecision.OVERLOADED, SchedulerDecision.WAIT])
@pytest.mark.asyncio
async def test_a_run_the_user_started_is_recorded_not_held(monkeypatch, decision):
    snapshot = SimpleNamespace(cpu_percent=99, memory_percent=90)
    monkeypatch.setattr(jmeter_tools._scheduler, 'check_readiness', lambda: SimpleNamespace(
        snapshot=snapshot, decision=decision, message='busy'))
    monkeypatch.setattr('dataclasses.asdict', lambda value: vars(value))
    monkeypatch.setattr(jmeter_tools, 'get_resource_monitor', lambda: pytest.fail('must not wait'))
    before_execute, _ = jmeter_tools.resource_hooks(False, {}, wait=False)
    execution = SimpleNamespace(status=Status.PENDING, error_message=None, resource_snapshot_pre=None)
    await before_execute(execution)
    assert execution.status == Status.PENDING and execution.resource_snapshot_pre['cpu_percent'] == 99


def test_the_analysis_is_told_how_busy_the_load_generator_was():
    machine = _load_generator({'cpu_percent': 91, 'memory_percent': 70, 'disk_percent': 5}, None)
    assert machine == {'before': {'cpu_percent': 91, 'memory_percent': 70}, 'after': None}
    assert '压测机' in _format_load_generator(machine) and '91' in _format_load_generator(machine)
    assert _load_generator(None, None) is None and _format_load_generator(None) == ''


def fact(execution_id='old-run-1', **overrides):
    return FactSheet(**dict(execution_id=execution_id, module='plan', version='unknown', test_type='baseline',
                            total_requests=10, throughput=12.5, avg_response_time=321.0, error_rate=1.5,
                            percentiles={'p95': 654.0, 'p99': 987.0}) | overrides)


@pytest.mark.asyncio
async def test_history_stays_within_the_plans_configuration_and_leaves_this_run_out(monkeypatch):
    found = AsyncMock(return_value=[fact('run'), fact('older')])
    monkeypatch.setattr(analysis_context.fact_ledger, 'get_facts_for_module_version', found)
    facts = await analysis_context.recent_facts('plan', 'unknown', 'scope-1', 'run')
    assert [item.execution_id for item in facts] == ['older']
    assert found.call_args.kwargs['context_scope'] == 'scope-1'
    assert await analysis_context.recent_facts('plan', 'unknown', '', 'run') == []


def test_recent_runs_can_be_quoted_without_counting_as_wrong_about_this_one():
    text = analysis_context.format_recent_facts([fact()])
    assert '321.0' in text and '987.0' in text and '12.5' in text
    assert {321.0, 654.0, 987.0, 12.5, 1.5} <= set(analysis_context.fact_values([fact()]))
    assert analysis_context.format_recent_facts([]) == ''


@pytest.mark.asyncio
async def test_a_report_reads_history_and_knowledge(monkeypatch):
    monkeypatch.setattr(analysis_context.fact_ledger, 'get_facts_for_module_version', AsyncMock(return_value=[fact()]))
    monkeypatch.setattr('app.services.ai.memory.provenance.load_execution_scope', AsyncMock(return_value='scope'))
    monkeypatch.setattr('app.services.ai.rag.retriever.RAGRetriever', lambda: SimpleNamespace(
        retrieve=lambda *args: {'similar_defects': [], 'known_issues': ['x'], 'module_standards': [],
                                'formatted_context': '登录接口有已知慢查询'}))
    text, numbers = await post_run.history_context(completed(None))
    assert '最近的运行' in text and '登录接口有已知慢查询' in text and 987.0 in numbers


@pytest.mark.asyncio
async def test_a_report_is_still_written_when_history_is_unreadable(monkeypatch):
    monkeypatch.setattr('app.services.ai.memory.provenance.load_execution_scope', AsyncMock(side_effect=OSError('down')))
    monkeypatch.setattr('app.services.ai.rag.retriever.RAGRetriever', lambda: SimpleNamespace(
        retrieve=lambda *args: (_ for _ in ()).throw(OSError('down'))))
    assert await post_run.history_context(completed(None)) == ('', [])


@pytest.mark.parametrize('error, log, code', [
    ('Test timed out after 3600s', '', 'timed_out'),
    ('System overloaded: CPU critically high', '', 'load_generator_busy'),
    (None, 'ERROR o.a.j.JMeter: java.lang.OutOfMemoryError: Java heap space', 'out_of_memory'),
    (None, 'CannotResolveClassException: kg.apc.jmeter.threads.UltimateThreadGroup', 'missing_plugin'),
    (None, 'java.io.FileNotFoundException: users.csv (No such file or directory)', 'missing_file'),
    (None, 'java.net.BindException: Address already in use', 'port_in_use'),
    (None, 'Error in NonGUIDriver java.lang.IllegalArgumentException', 'plan_unreadable'),
    (None, 'INFO all fine\nERROR something odd happened', 'unknown'),
])
def test_a_failed_run_says_what_stopped_it(error, log, code):
    assert run_diagnostics.diagnose_failure(error, log)['code'] == code


def test_a_failure_diagnosis_shows_the_line_it_read():
    diagnosis = run_diagnostics.diagnose_failure(None, 'INFO ok\nERROR java.lang.OutOfMemoryError: heap\nINFO end')
    assert 'OutOfMemoryError' in diagnosis['line']
    assert run_diagnostics.diagnose_failure(None, '') == {'code': 'unknown', 'line': ''}


@pytest.fixture
def run_with_report(monkeypatch, tmp_path):
    from app.api import routes
    execution = Execution(id='run', test_plan_id='plan', status=Status.COMPLETED, jmx_path='a.jmx',
                          report_dir=str(tmp_path), start_time=datetime.now(), ai_analysis=ReportAnalysis(
                              overall_assessment='还行', conclusion='结束'))
    monkeypatch.setattr(routes.store, 'get_execution', lambda execution_id: execution)
    saved = []
    async def persist(updated):
        saved.append(updated)
    monkeypatch.setattr(routes, 'persist_execution', persist)
    monkeypatch.setattr(routes.AIAdapterFactory, 'create', lambda model: SimpleNamespace())
    ask = AsyncMock(return_value=AgentResult(True, 'q', output='慢在建连', analysis={
        'validation': {'mismatches': [{'metric': 'p95', 'claimed': 1, 'actual': 2}]}}))
    monkeypatch.setattr(routes, 'AnalysisAgent', type('Agent', (), {
        'resolve_execution': staticmethod(AsyncMock(return_value={'execution_id': 'run'})),
        '__init__': lambda self, adapter: None, 'run_analysis': lambda self, *a, **k: ask(*a, **k)}))
    app = FastAPI()
    app.include_router(routes.router)
    return SimpleNamespace(app=app, ask=ask, saved=saved, execution=execution)


async def post_question(app, question='为什么慢？'):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        return await client.post('/executions/run/questions', json={'question': question})


@pytest.mark.asyncio
async def test_a_question_is_answered_and_kept_with_the_analysis(run_with_report, tmp_path):
    response = await post_question(run_with_report.app)
    assert response.status_code == 200 and response.json()['answer'] == '慢在建连'
    assert response.json()['mismatches'][0]['metric'] == 'p95'
    assert '还行' in run_with_report.ask.call_args.kwargs['context']
    stored = run_with_report.saved[0].ai_analysis.follow_ups
    assert stored[0]['question'] == '为什么慢？'
    assert '慢在建连' in (tmp_path / 'ai_analysis.md').read_text(encoding='utf-8')


@pytest.mark.asyncio
async def test_a_failed_answer_keeps_nothing(run_with_report):
    run_with_report.ask.return_value = AgentResult(False, 'q', error='model down')
    response = await post_question(run_with_report.app)
    assert response.status_code == 502 and 'model down' in response.text and not run_with_report.saved


@pytest.mark.asyncio
async def test_a_run_without_an_analysis_cannot_be_questioned(run_with_report):
    run_with_report.execution.ai_analysis = None
    assert (await post_question(run_with_report.app)).status_code == 409
    run_with_report.ask.assert_not_awaited()


def test_questions_are_bounded():
    from app.models.schemas import RunQuestion
    with pytest.raises(ValueError):
        RunQuestion(question='')
    with pytest.raises(ValueError):
        RunQuestion(question='x' * 2001)


@pytest.mark.asyncio
async def test_a_run_deleted_while_answering_keeps_nothing(run_with_report, monkeypatch):
    from app.api import routes
    reads = iter([run_with_report.execution, None])
    monkeypatch.setattr(routes.store, 'get_execution', lambda execution_id: next(reads))
    assert (await post_question(run_with_report.app)).status_code == 409
    assert not run_with_report.saved


@pytest.mark.asyncio
async def test_a_run_cannot_be_questioned_while_its_analysis_is_written(run_with_report):
    run_with_report.execution.report_status = 'running'
    assert (await post_question(run_with_report.app)).status_code == 409
    run_with_report.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_questions_asked_together_are_both_kept(run_with_report, monkeypatch):
    import asyncio
    from app.api import routes
    latest = {'run': run_with_report.execution}
    monkeypatch.setattr(routes.store, 'get_execution', lambda execution_id: latest['run'])
    async def persist(updated):
        await asyncio.sleep(0)  # A database write gives the other request its turn.
        latest['run'] = updated
    monkeypatch.setattr(routes, 'persist_execution', persist)
    responses = await asyncio.gather(post_question(run_with_report.app, '一'), post_question(run_with_report.app, '二'))
    assert [r.status_code for r in responses] == [200, 200]
    assert sorted(item['question'] for item in latest['run'].ai_analysis.follow_ups) == ['一', '二']


@pytest.fixture
def stored_run(monkeypatch, tmp_path):
    """One run in the store; persisting replaces it, as the real store does."""
    from app.services import execution_lifecycle
    run = Execution(id='run', test_plan_id='plan', status=Status.COMPLETED, jmx_path='a.jmx',
                    report_dir=str(tmp_path), summary=summary(), report_status='completed')
    held = {'run': run}
    saved = []
    async def persist(updated):
        saved.append(updated.model_copy(deep=True))
        held['run'] = updated.model_copy(deep=True)
    monkeypatch.setattr(execution_lifecycle, 'persist_execution', persist)
    monkeypatch.setattr(post_run.store, 'get_execution', lambda execution_id: held.get(execution_id))
    monkeypatch.setattr(type(post_run.store), 'executions', property(lambda self: dict(held)))
    from app.services.ai.memory import fact_ledger
    monkeypatch.setattr(fact_ledger, 'load', AsyncMock(return_value=None))
    return SimpleNamespace(held=held, saved=saved)


@pytest.mark.asyncio
async def test_an_analysis_can_be_written_again(stored_run, monkeypatch):
    async def analyze(execution, ai_model):
        execution.ai_analysis = ReportAnalysis(overall_assessment=f'by {ai_model}', conclusion='done')
    monkeypatch.setattr(post_run, '_write_analysis', analyze)
    started = await post_run.start_reanalysis('run', 'model-b')
    assert started.report_status == 'running' and stored_run.saved[0].report_status == 'running'
    await post_run._reanalyses['run']
    final = stored_run.held['run']
    assert final.report_status == 'completed' and final.ai_analysis.overall_assessment == 'by model-b'
    assert 'run' not in post_run._reanalyses


@pytest.mark.asyncio
async def test_a_failed_reanalysis_says_why(stored_run, monkeypatch):
    monkeypatch.setattr(post_run, '_write_analysis', AsyncMock(side_effect=RuntimeError('model down')))
    await post_run.start_reanalysis('run')
    await post_run._reanalyses['run']
    assert stored_run.held['run'].report_status == 'failed' and stored_run.held['run'].report_error == 'model down'


@pytest.mark.asyncio
@pytest.mark.parametrize('change, error', [
    ({'status': Status.FAILED}, ValueError), ({'summary': None}, ValueError), ({'report_status': 'running'}, RuntimeError)])
async def test_only_a_finished_idle_run_can_be_analysed_again(stored_run, change, error):
    for key, value in change.items():
        setattr(stored_run.held['run'], key, value)
    with pytest.raises(error):
        await post_run.start_reanalysis('run')
    assert not stored_run.saved


@pytest.mark.asyncio
async def test_a_restart_ends_analyses_that_were_running(stored_run):
    stored_run.held['run'].report_status = 'running'
    await post_run.recover_interrupted()
    assert stored_run.held['run'].report_status == 'failed'
    assert stored_run.held['run'].report_error == post_run.INTERRUPTED


@pytest.mark.asyncio
@pytest.mark.parametrize('failure, status', [(LookupError('run'), 404), (ValueError('no'), 409), (RuntimeError('busy'), 409)])
async def test_the_analysis_endpoint_reports_why_it_cannot_start(monkeypatch, failure, status):
    from app.api import routes
    monkeypatch.setattr(routes.post_run, 'start_reanalysis', AsyncMock(side_effect=failure))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        assert (await client.post('/executions/run/analysis')).status_code == status
