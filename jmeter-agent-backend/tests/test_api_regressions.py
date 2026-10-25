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
async def test_invalid_analysis_does_not_mark_report_complete(monkeypatch, tmp_path):
    from app.api import routes
    from app.models.schemas import ReportAnalysis
    from app.services.ai.rag.archiver import rag_archiver
    import app.services.notifications as notifications
    analysis = ReportAnalysis(analysis_status='invalid', overall_assessment='Invalid model response', conclusion='Unknown')
    monkeypatch.setattr(routes, 'TestAnalyzer', lambda **kwargs: SimpleNamespace(analyze=AsyncMock(return_value=analysis)))
    monkeypatch.setattr(rag_archiver, 'archive_raw_report', lambda **kwargs: None)
    monkeypatch.setattr(notifications, 'notify_test_completed', AsyncMock())
    execution = SimpleNamespace(id='fixture', status=routes.TestExecutionStatus.COMPLETED,
        summary=SimpleNamespace(model_dump=lambda: {'total_requests': 10}), test_plan_id='fixture',
        report_dir=str(tmp_path), error_message=None, log_path=None, report_status='running', report_error=None)
    await routes._postprocess_web_execution(execution, {})
    assert execution.status == routes.TestExecutionStatus.COMPLETED
    assert execution.report_status == 'failed'
    assert execution.report_error == 'Invalid model response'
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
async def test_analysis_records_the_requirement_verdict_it_was_written_against(monkeypatch, tmp_path):
    from app.api import routes
    from app.models.schemas import DetailedTestSummary, ReportAnalysis
    from app.services import requirements as requirement_service
    from app.services.ai.rag.archiver import rag_archiver
    import app.services.notifications as notifications
    analysis = ReportAnalysis(overall_assessment='ok', conclusion='ok')
    monkeypatch.setattr(routes, 'TestAnalyzer', lambda **kwargs: SimpleNamespace(analyze=AsyncMock(return_value=analysis)))
    monkeypatch.setattr(rag_archiver, 'archive_raw_report', lambda **kwargs: None)
    monkeypatch.setattr(notifications, 'notify_test_completed', AsyncMock())
    monkeypatch.setattr(requirement_service, 'get_requirements', AsyncMock(return_value=None))
    summary = DetailedTestSummary(total_requests=10, avg_response_time=100, min_response_time=1, max_response_time=200,
                                  error_rate=0, throughput=5, received_bytes_per_sec=1, sent_bytes_per_sec=1)
    execution = SimpleNamespace(id='run', project_id='p', status=routes.TestExecutionStatus.COMPLETED, summary=summary,
        test_plan_id='plan', report_dir=str(tmp_path), error_message=None, log_path=None,
        report_status='running', report_error=None)
    await routes._postprocess_web_execution(execution, {})
    assert execution.ai_analysis.requirements_verdict == {'verdict': 'none', 'passed': 0, 'failed': 0, 'missing': 0}
