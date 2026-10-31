"""API boundary regressions using temporary files, without HTTP or external services."""
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile


@pytest.mark.asyncio
async def test_rag_document_filters_are_combined_and_list_limit_is_forwarded(monkeypatch):
    from app.api import routes
    calls = []
    def hybrid(**kwargs):
        calls.append(kwargs)
        return []
    monkeypatch.setattr(routes.rag_store, 'hybrid_search', hybrid)
    monkeypatch.setattr(routes.rag_store, 'full_text_search', lambda *args, **kwargs: pytest.fail('Filters bypassed'))
    await routes.list_rag_documents(doc_type='known_issue', module='orders', keyword='timeout', limit=100)
    assert calls[0]['module'] == 'orders' and calls[0]['doc_type'] == 'known_issue'
    assert calls[0]['query_text'] == 'timeout'
    limits = []
    monkeypatch.setattr(routes.rag_store, 'list_all', lambda limit=50: limits.append(limit) or [])
    await routes.list_rag_documents(doc_type=None, module=None, keyword=None, limit=100)
    assert limits == [100]


@pytest.mark.asyncio
async def test_empty_rag_search_does_not_broaden_scope(monkeypatch):
    from app.api import routes
    monkeypatch.setattr(routes.rag_store, 'hybrid_search', lambda **kwargs: [])
    monkeypatch.setattr(routes.rag_store, 'full_text_search', lambda *args, **kwargs: pytest.fail('Unscoped fallback'))
    assert (await routes.search_rag(q='timeout', module='orders', doc_type='known_issue', limit=20))['documents'] == []


@pytest.mark.asyncio
async def test_rag_query_does_not_run_on_event_loop_thread(monkeypatch):
    import threading
    from app.api import routes
    caller = threading.get_ident()
    def query(**kwargs):
        assert threading.get_ident() != caller, 'RAG query blocks event loop'
        return []
    monkeypatch.setattr(routes.rag_store, 'hybrid_search', query)
    await routes.list_rag_documents(doc_type=None, module=None, keyword='fixture', limit=10)


@pytest.mark.asyncio
async def test_system_resource_sampling_does_not_block_event_loop(monkeypatch):
    import threading
    from app.api import routes
    from app.services.jmeter.resource_monitor import get_resource_monitor
    caller = threading.get_ident()
    threads = []
    def sample():
        threads.append(threading.get_ident())
        return SimpleNamespace(to_dict=lambda: {'cpu_percent': 0})
    monkeypatch.setattr(get_resource_monitor(), 'snapshot', sample)
    assert await routes.get_system_resources() == {'cpu_percent': 0}
    assert threads and threads[0] != caller


@pytest.mark.asyncio
async def test_resource_fallback_uses_configured_readiness_thresholds(monkeypatch):
    import psutil
    from app.api import routes
    from app.services.jmeter.resource_monitor import get_resource_monitor
    def failed():
        raise OSError('fixture unavailable disk')
    monkeypatch.setattr(get_resource_monitor(), 'snapshot', failed)
    monkeypatch.setattr(routes.settings, 'RESOURCE_CPU_THRESHOLD', 50)
    monkeypatch.setattr(routes.settings, 'RESOURCE_MEMORY_THRESHOLD', 70)
    monkeypatch.setattr(psutil, 'cpu_percent', lambda **kwargs: 60)
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(percent=60, used=1024, total=2048))
    assert (await routes.get_system_resources())['is_healthy'] is False


@pytest.mark.asyncio
async def test_invalid_analysis_is_recorded_on_the_analysis_not_the_report(monkeypatch, tmp_path):
    from app.api import routes
    from app.models.schemas import ReportAnalysis
    from app.services.ai.rag.archiver import rag_archiver
    analysis = ReportAnalysis(analysis_status='invalid', overall_assessment='Invalid model response', conclusion='Unknown')
    monkeypatch.setattr(routes, 'TestAnalyzer', lambda **kwargs: SimpleNamespace(analyze=AsyncMock(return_value=analysis)))
    monkeypatch.setattr(rag_archiver, 'archive_raw_report', lambda **kwargs: None)
    execution = SimpleNamespace(id='fixture', status=routes.TestExecutionStatus.COMPLETED,
        summary=SimpleNamespace(model_dump=lambda: {'total_requests': 10}), test_plan_id='fixture',
        report_dir=str(tmp_path), error_message=None, log_path=None, report_status='running', report_error=None)
    await routes._postprocess_web_execution(execution, {})
    assert execution.status == routes.TestExecutionStatus.COMPLETED
    # The parsed report is still good; the lifecycle restores its status after postprocessing.
    assert execution.report_status == 'running' and execution.report_error is None
    assert execution.ai_analysis.analysis_status == 'invalid'
    assert '尚未逐项事实核验' in (tmp_path / 'ai_analysis.md').read_text(encoding='utf-8')


@pytest.mark.asyncio
async def test_failed_execution_without_report_directory_is_readable(monkeypatch):
    from app.api import routes
    from app.models.schemas import TestExecutionInfo

    execution = TestExecutionInfo(id='failed-no-report', test_plan_id='unknown', status='failed',
                                 start_time=datetime.now(), jmx_path='missing.jmx', report_dir=None)
    monkeypatch.setitem(routes.store.executions, execution.id, execution)
    report = await routes.get_report(execution.id)
    assert report.status == 'failed'
    assert report.raw_data_path is None
    assert report.html_report_path is None
    assert report.summary.total_requests == 0
    with pytest.raises(HTTPException) as error:
        await routes.get_html_report(execution.id)
    assert error.value.status_code == 404


@pytest.fixture
def csv_root(tmp_path, monkeypatch):
    from app.api import data_routes
    root = tmp_path / 'data'
    root.mkdir()
    monkeypatch.setattr(data_routes, 'TEST_DATA_DIR', str(root))
    (tmp_path / 'outside.csv').write_text('dummy\nsentinel\n')
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['preview', 'delete'])
async def test_csv_traversal_is_rejected(csv_root, operation):
    from app.api import data_routes
    path = str(csv_root / '..' / 'outside.csv')
    with pytest.raises(HTTPException) as error:
        if operation == 'preview':
            await data_routes.preview_csv(path, rows=1)
        else:
            await data_routes.delete_data_file(path)
    assert error.value.status_code == 403
    assert (csv_root.parent / 'outside.csv').exists()


@pytest.mark.asyncio
async def test_csv_upload_cannot_escape_through_project_id(csv_root):
    from app.api import data_routes
    file = UploadFile(filename='test.csv', file=BytesIO(b'name\nvalue\n'))
    with pytest.raises(HTTPException) as error:
        await data_routes.upload_csv(file, project_id='..')
    assert error.value.status_code == 403
    assert not (csv_root.parent / 'test.csv').exists()


@pytest.mark.asyncio
async def test_csv_list_cannot_escape_through_project_id(csv_root):
    from app.api import data_routes
    with pytest.raises(HTTPException) as error:
        await data_routes.list_data_files('..')
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_csv_preview_accepts_file_inside_root(csv_root):
    from app.api import data_routes
    file = csv_root / 'valid.csv'
    file.write_text('name\nvalue\n')
    assert (await data_routes.preview_csv(str(file), rows=1))['rows'] == [['value']]
def test_utc_now_naive_preserves_legacy_database_contract():
    from datetime import datetime, timezone
    from app.core.time import utc_now_naive

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    value = utc_now_naive()
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    assert value.tzinfo is None
    assert before <= value <= after



@pytest.mark.asyncio
async def test_saving_tree_with_new_name_persists_the_rename(monkeypatch, tmp_path):
    from app.api import routes
    from app.models.schemas import TestConfig, TestPlanInfo, UpdateJMXTreeRequest
    jmx = tmp_path / 'plan.jmx'
    jmx.write_text('<jmeterTestPlan><hashTree><TestPlan testclass="TestPlan" testname="P"/><hashTree/></hashTree></jmeterTestPlan>',
                   encoding='utf-8')
    plan = TestPlanInfo(id='abcd1234', project_id='default', name='Old', jmx_path=str(jmx),
                        created_at=datetime.now(), test_config=TestConfig(test_type='baseline'), endpoints_count=1)
    monkeypatch.setitem(routes.store.test_plans, plan.id, plan)
    persisted = []
    monkeypatch.setattr(routes, '_persist_test_plan', AsyncMock(side_effect=persisted.append))
    tree = routes.parse_jmx(str(jmx))
    await routes.update_jmx_tree(plan.id, UpdateJMXTreeRequest(tree=tree, name='New'))
    assert [p.name for p in persisted] == ['New']
    await routes.update_jmx_tree(plan.id, UpdateJMXTreeRequest(tree=tree))
    assert len(persisted) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ['plan.jmx']  # no .backup or .tmp left behind


@pytest.mark.asyncio
async def test_failed_tree_save_keeps_the_old_file(monkeypatch, tmp_path):
    from app.api import routes
    from app.models.schemas import TestConfig, TestPlanInfo, UpdateJMXTreeRequest
    jmx = tmp_path / 'plan.jmx'
    jmx.write_text('<jmeterTestPlan><hashTree><TestPlan testclass="TestPlan" testname="P"/><hashTree/></hashTree></jmeterTestPlan>',
                   encoding='utf-8')
    original = jmx.read_bytes()
    plan = TestPlanInfo(id='abcd1234', project_id='default', name='Old', jmx_path=str(jmx),
                        created_at=datetime.now(), test_config=TestConfig(test_type='baseline'), endpoints_count=1)
    monkeypatch.setitem(routes.store.test_plans, plan.id, plan)
    def broken_replace(src, dst):
        raise OSError('disk full')
    monkeypatch.setattr(routes.os, 'replace', broken_replace)
    with pytest.raises(HTTPException):
        await routes.update_jmx_tree(plan.id, UpdateJMXTreeRequest(tree=routes.parse_jmx(str(jmx))))
    assert jmx.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ['plan.jmx']


@pytest.mark.asyncio
async def test_post_run_analysis_sees_the_report_evidence_and_records_its_verdict(monkeypatch, tmp_path):
    from app.api import routes
    from app.models.schemas import DetailedTestSummary, ReportAnalysis
    from app.services import evidence as evidence_service
    from app.services import requirements as requirement_service
    from app.services.ai.rag.archiver import rag_archiver
    analyze = AsyncMock(return_value=ReportAnalysis(overall_assessment='ok', conclusion='ok'))
    monkeypatch.setattr(routes, 'TestAnalyzer', lambda **kwargs: SimpleNamespace(analyze=analyze))
    monkeypatch.setattr(rag_archiver, 'archive_raw_report', lambda **kwargs: None)
    summary = DetailedTestSummary(total_requests=10, avg_response_time=100, min_response_time=1, max_response_time=200,
                                  error_rate=0, throughput=5, received_bytes_per_sec=1, sent_bytes_per_sec=1)
    evaluation = requirement_service.RequirementEvaluation(verdict='fail', passed=1, failed=2, missing=0)
    async def build_evidence(execution, run_summary):
        assert run_summary is summary
        return evidence_service.ExecutionEvidence(execution_id='run', project_id='p', requirements=evaluation,
                                                  baseline=None, server_metrics={'status': 'not_configured'})
    monkeypatch.setattr(evidence_service, 'build_evidence', build_evidence)
    execution = SimpleNamespace(id='run', project_id='p', status=routes.TestExecutionStatus.COMPLETED, summary=summary,
        test_plan_id='plan', report_dir=str(tmp_path), error_message=None, log_path=None,
        report_status='running', report_error=None)
    await routes._postprocess_web_execution(execution, {})
    text = analyze.call_args.kwargs['evidence_text']
    assert text == evidence_service.format_for_prompt(await build_evidence(execution, summary))
    assert '服务端指标' in text and '历史对比' in text
    assert execution.ai_analysis.requirements_verdict == {'verdict': 'fail', 'passed': 1, 'failed': 2, 'missing': 0}


def test_analysis_prompt_includes_evidence_only_when_given():
    from app.services.ai.prompts.test_analyzer import build_analysis_prompt
    plain = build_analysis_prompt({'total_requests': 1}, {}, 'baseline')
    assert '不得改判' not in plain
    with_evidence = build_analysis_prompt({'total_requests': 1}, {}, 'baseline', '## 性能需求\nR1 不达标')
    assert '不得改判需求是否达标' in with_evidence and 'R1 不达标' in with_evidence


async def _workflow_events(monkeypatch, items):
    import json
    from app.api import routes
    from app.models.schemas import AIWorkflowRequest
    from app.services.ai.agents import workflow_agent

    class FakeAgent:
        def __init__(self, adapter):
            pass

        async def stream_from_doc(self, **kwargs):
            for item in items:
                yield item
    monkeypatch.setattr(workflow_agent, 'WorkflowAgent', FakeAgent)
    monkeypatch.setattr(routes.AIAdapterFactory, 'get_adapter', lambda model: object())
    response = await routes.ai_workflow_stream(AIWorkflowRequest(api_documentation='doc', testing_requirements='req'))
    body = ''.join([chunk if isinstance(chunk, str) else chunk.decode() async for chunk in response.body_iterator])
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]


@pytest.mark.asyncio
async def test_workflow_stream_reports_each_step_and_counts_appended_steps(monkeypatch):
    from app.services.ai.agent.types import AgentEvent
    result = {'success': True, 'plan_steps': [{'step_id': 1, 'success': True}], 'project_id': 'p1',
              'test_plan_ids': ['plan1'], 'diagnostic_summary': 'ok', 'error': None}
    events = await _workflow_events(monkeypatch, [
        ('event', AgentEvent('plan_ready', 'Plan has 2 steps', {'steps': [{}, {}]})),
        ('event', AgentEvent('step_start', 'Step 1/2: create project', {'step_id': 1})),
        ('event', AgentEvent('step_start', 'Step 3/2: summarize', {'step_id': 3})),
        ('result', result),
    ])
    messages = [e['message'] for e in events]
    assert '第 1/2 步：create project' in messages and '第 3/3 步：summarize' in messages
    assert events[-1]['step'] == 'complete' and events[-1]['project_id'] == 'p1'


@pytest.mark.asyncio
async def test_workflow_whose_steps_all_finished_is_complete_even_if_the_summary_failed(monkeypatch):
    result = {'success': False, 'plan_steps': [{'step_id': 1, 'success': True}], 'project_id': 'p1',
              'test_plan_ids': ['plan1'], 'diagnostic_summary': '', 'error': 'Summary generation failed: budget'}
    events = await _workflow_events(monkeypatch, [('result', result)])
    assert events[-1]['step'] == 'complete' and events[-1]['project_id'] == 'p1'
    assert '最后的总结没能生成' in events[-1]['diagnostic_summary']
    failed = dict(result, plan_steps=[{'step_id': 1, 'success': False}])
    events = await _workflow_events(monkeypatch, [('result', failed)])
    assert events[-1]['step'] == 'error'


@pytest.mark.asyncio
async def test_workflow_always_returns_a_requirements_draft_for_review(monkeypatch):
    from app.services import requirements as requirement_service
    calls = []

    async def extract(document, adapter, source_name=''):
        calls.append(document)
        return requirement_service.PerformanceRequirements(items=[requirement_service.RequirementItem(
            id='R1', scope='transaction', transaction='POST /api/pizza', metric='p95', operator='lte', value=800)])
    saved = []
    monkeypatch.setattr(requirement_service, 'extract_requirements', extract)
    monkeypatch.setattr(requirement_service, 'get_requirements', AsyncMock(return_value=None))
    monkeypatch.setattr(requirement_service, 'save_requirements', AsyncMock(side_effect=saved.append))
    result = {'success': True, 'plan_steps': [{'step_id': 1, 'success': True}], 'project_id': 'p1',
              'test_plan_ids': [], 'diagnostic_summary': '', 'error': None}
    events = await _workflow_events(monkeypatch, [('result', result)])
    assert calls == ['req\n\ndoc']  # the user's goal and document, as typed
    assert events[-1]['requirements_draft']['items'][0]['transaction'] == 'POST /api/pizza'
    assert not saved  # a draft is for review; nothing applies until the user saves


@pytest.mark.asyncio
async def test_workflow_skips_the_draft_when_requirements_already_exist_or_drafting_fails(monkeypatch):
    from app.services import requirements as requirement_service
    existing = requirement_service.PerformanceRequirements(items=[requirement_service.RequirementItem(
        id='R1', metric='error_rate', operator='lt', value=1)])
    monkeypatch.setattr(requirement_service, 'get_requirements', AsyncMock(return_value=existing))
    extract = AsyncMock()
    monkeypatch.setattr(requirement_service, 'extract_requirements', extract)
    result = {'success': True, 'plan_steps': [{'step_id': 1, 'success': True}], 'project_id': 'p1',
              'test_plan_ids': [], 'diagnostic_summary': '', 'error': None}
    events = await _workflow_events(monkeypatch, [('result', result)])
    assert events[-1]['requirements_draft'] is None
    extract.assert_not_awaited()
    monkeypatch.setattr(requirement_service, 'get_requirements', AsyncMock(return_value=None))
    monkeypatch.setattr(requirement_service, 'extract_requirements', AsyncMock(side_effect=ValueError('model down')))
    events = await _workflow_events(monkeypatch, [('result', result)])
    assert events[-1]['step'] == 'complete' and events[-1]['requirements_draft'] is None


def test_workflow_agent_leaves_requirements_to_the_app():
    from app.services.ai.agents.workflow_agent import REQUIREMENT_TOOLS, WorkflowAgent
    agent = WorkflowAgent(SimpleNamespace())
    assert not REQUIREMENT_TOOLS & set(agent.tools._tools)
    assert 'create_project' in agent.tools._tools


@pytest.mark.asyncio
async def test_analysis_crash_is_recorded_on_the_analysis_and_announced(monkeypatch, tmp_path):
    from app.api import routes
    from app.services.ai.rag.archiver import rag_archiver
    from app.services.execution_lifecycle import execution_lifecycle
    monkeypatch.setattr(routes, 'TestAnalyzer', lambda **kwargs: SimpleNamespace(
        analyze=AsyncMock(side_effect=TimeoutError('model timed out'))))
    monkeypatch.setattr(rag_archiver, 'archive_raw_report', lambda **kwargs: None)
    monkeypatch.setattr(routes, '_analysis_evidence', AsyncMock(return_value=('', None, [])))
    emit = AsyncMock()
    monkeypatch.setattr(execution_lifecycle, 'emit', emit)
    execution = SimpleNamespace(id='crash', project_id='p', status=routes.TestExecutionStatus.COMPLETED,
        summary=SimpleNamespace(model_dump=lambda: {'total_requests': 10}), test_plan_id='plan',
        report_dir=str(tmp_path), error_message=None, log_path=None, report_status='running', report_error=None)
    await routes._postprocess_web_execution(execution, {})
    assert execution.report_status == 'running' and execution.report_error is None
    assert execution.ai_analysis.analysis_status == 'failed'
    assert 'model timed out' in execution.ai_analysis.overall_assessment
    emit.assert_awaited_once_with('analysis_completed', execution)
