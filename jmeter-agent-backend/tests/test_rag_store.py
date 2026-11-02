"""Tests for the LanceRAGStore main flows (CRUD + zero-vector repair).

Embeddings are stubbed at the module boundary so tests never download or
load a sentence-transformers model. Each test gets an isolated LanceDB
directory via tmp_path.
"""

import pytest
from types import SimpleNamespace

import app.services.ai.rag.store as store_mod
from app.services.ai.rag.store import LanceRAGStore, RAGDocument

VECTOR_DIM = LanceRAGStore.VECTOR_DIM
FAKE_VECTOR = [0.01] * VECTOR_DIM

CONTENT = "登录接口在 200 并发下 p99 响应时间为 250ms，错误率 0.5%，吞吐稳定在 800 TPS。"


def test_lancedb_table_listing_uses_paginated_current_api():
    pages = {
        None: SimpleNamespace(tables=['first'], page_token='next'),
        'next': SimpleNamespace(tables=['rag_documents'], page_token=None),
    }
    calls = []
    fake_db = SimpleNamespace(
        list_tables=lambda page_token=None: calls.append(page_token) or pages[page_token],
        table_names=lambda: pytest.fail('deprecated table_names() called'),
    )
    rag = object.__new__(LanceRAGStore)
    rag._db = fake_db

    assert rag._list_table_names() == {'first', 'rag_documents'}
    assert calls == [None, 'next']


def test_lancedb_table_listing_keeps_older_client_compatibility():
    rag = object.__new__(LanceRAGStore)
    rag._db = SimpleNamespace(table_names=lambda: ['legacy'])
    assert rag._list_table_names() == {'legacy'}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated LanceDB store."""
    s = LanceRAGStore(db_path=str(tmp_path / "lance"))
    assert s.available, "lancedb backend must be available for these tests"
    return s


def make_doc(doc_id: str = "", module: str = "login-api", **kwargs) -> RAGDocument:
    return RAGDocument(
        id=doc_id,
        doc_type=kwargs.pop("doc_type", "analysis_conclusion"),
        module=module,
        version=kwargs.pop("version", "v1"),
        title=kwargs.pop("title", "容量测试结论"),
        content=kwargs.pop("content", CONTENT),
        keywords=kwargs.pop("keywords", ["p99", "容量"]),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_rag_management_routes_offload_blocking_store_operations(monkeypatch):
    import threading
    from app.api import routes

    event_loop_thread = threading.get_ident()
    calls = []
    document = make_doc('doc-1')

    class BlockingStore:
        def record(self, name, value):
            calls.append((name, threading.get_ident()))
            return value

        def get_stats(self): return self.record('stats', {'by_module': {'login-api': 1}})
        def get(self, doc_id): return self.record('get', document)
        def delete(self, doc_id): return self.record('delete', True)
        def get_module_versions(self, module): return self.record('versions', ['v1'])
        def get_latest_analysis(self, module): return self.record('latest', document)
        def search_by_module(self, module, limit=50): return self.record('history', [document])
        def hybrid_search(self, **kwargs): return self.record('search', [document])
        def add(self, doc): return self.record('add', 'doc-2')

    monkeypatch.setattr(routes, 'rag_store', BlockingStore())
    await routes.get_rag_stats()
    await routes.get_rag_document('doc-1')
    await routes.delete_rag_document('doc-1')
    await routes.list_rag_modules()
    await routes.get_module_history('login-api')
    await routes.compare_versions(module='login-api', version_a='v1', version_b='v2')
    await routes.manual_archive({
        'doc_type': 'known_issue', 'module': 'login-api', 'version': 'v1',
        'title': 'fixture', 'content': CONTENT,
    })

    assert {name for name, _ in calls} == {
        'stats', 'get', 'delete', 'versions', 'latest', 'history', 'search', 'add',
    }
    assert all(thread_id != event_loop_thread for _, thread_id in calls)


class TestAdd:
    @pytest.mark.asyncio
    async def test_registered_analysis_archive_retains_scope_without_execution_cache(self, store, monkeypatch):
        from unittest.mock import AsyncMock
        from app.services.ai.rag import archiver as mod
        from app.services.ai.memory import provenance
        from app.models.schemas import FactSheet, StructuredAnalysis, DetailedTestSummary
        monkeypatch.setattr(provenance, 'execution_scope', lambda _: '')
        monkeypatch.setattr(mod.fact_ledger, 'save', AsyncMock(return_value=True))
        summary = DetailedTestSummary(total_requests=10, success_count=10, error_count=0, error_rate=0,
            avg_response_time=100, min_response_time=1, max_response_time=200, throughput=1,
            p50_response_time=100, p90_response_time=100, p95_response_time=100, p99_response_time=100,
            received_bytes_per_sec=0, sent_bytes_per_sec=0)
        fact = FactSheet(execution_id='run-A', module='plan-A', version='unknown', total_requests=10,
                         context_scope='persisted-scope', raw_summary=summary,
                         jtl_path='fixture.jtl', jtl_sha256='fixture-digest')
        analysis = StructuredAnalysis(execution_id='run-A', module='plan-A', version='unknown',
            overall_assessment='The available measurements require further investigation before drawing any root cause conclusion.')
        result = await mod.RAGArchiver(store=store).archive_analysis_result(analysis, fact)
        assert result['errors'] == [] and result['fact_archived']
        for key in ('conclusion_archived', 'raw_report_archived'):
            assert result[key]
            assert store.get(result[key]).context_scope == 'persisted-scope'

    def test_failed_write_never_enters_cache_or_reports_success(self, store, monkeypatch):
        def fail(*args, **kwargs):
            raise OSError('injected disk failure')
        monkeypatch.setattr(store._table, 'add', fail)
        monkeypatch.setattr(store._table, 'merge_insert', fail)
        with pytest.raises(OSError, match='disk failure'):
            store.add(make_doc(doc_id='failed'))
        assert store.get('failed') is None

    def test_reused_document_id_cannot_create_duplicate_rows(self, store):
        store.add(make_doc(doc_id='same'))
        with pytest.raises(ValueError, match='exists'):
            store.add(make_doc(doc_id='same', content='This different observation must not silently replace a saved document.'))
        assert store.count() == 1

    def test_add_and_get_roundtrip(self, store):
        doc_id = store.add(make_doc(doc_id="d1"))
        assert doc_id == "d1"
        doc = store.get("d1")
        assert doc is not None
        assert doc.module == "login-api"
        assert doc.content == CONTENT

    def test_add_generates_id_when_missing(self, store):
        doc_id = store.add(make_doc(doc_id=""))
        assert doc_id
        assert store.get(doc_id) is not None

    def test_add_rejects_disallowed_type(self, store):
        with pytest.raises(ValueError, match="not allowed"):
            store.add(make_doc(doc_type="chitchat"))

    def test_add_rejects_short_content(self, store):
        with pytest.raises(ValueError, match="quality filter"):
            store.add(make_doc(content="太短"))

class TestUpdate:
    def test_failed_update_preserves_original_row_and_cached_value(self, store, monkeypatch):
        store.add(make_doc(doc_id='original', title='original title'))
        def fail(*args, **kwargs):
            raise OSError('injected disk failure')
        monkeypatch.setattr(store._table, 'add', fail)
        monkeypatch.setattr(store._table, 'merge_insert', fail)
        with pytest.raises(OSError, match='disk failure'):
            store.update('original', title='unsaved title')
        assert store.get('original').title == 'original title'
        assert store.count() == 1

    def test_update_replaces_row(self, store):
        """Regression for B3: update must replace the row, not add a second one."""
        store.add(make_doc(doc_id="d1", title="旧结论"))
        updated = store.update("d1", title="新结论", content="新的测试结论内容，p99 降至 180ms，容量提升明显。")

        assert updated is not None
        assert updated.title == "新结论"
        assert store.count() == 1  # replaced, not duplicated
        assert store.get("d1").title == "新结论"
        assert len(store._table.search().to_list()) == 1

    def test_update_nonexistent_returns_none(self, store):
        assert store.update("ghost", title="x") is None

class TestDelete:
    def test_failed_delete_preserves_cache_and_reports_failure(self, store, monkeypatch):
        store.add(make_doc(doc_id='retained'))
        def fail(*args, **kwargs):
            raise OSError('injected disk failure')
        monkeypatch.setattr(store._table, 'delete', fail)
        with pytest.raises(OSError, match='disk failure'):
            store.delete('retained')
        assert store.get('retained') is not None

    def test_delete_removes_doc(self, store):
        store.add(make_doc(doc_id="d1"))
        assert store.delete("d1") is True
        assert store.get("d1") is None
        assert store.count() == 0


class TestStats:
    def test_count_list_and_stats(self, store):
        store.add(make_doc(doc_id="d1", module="login-api"))
        store.add(make_doc(doc_id="d2", module="order-api", doc_type="known_issue"))

        assert store.count() == 2
        docs = store.list_all(limit=10)
        assert len(docs) == 2

        stats = store.get_stats()
        assert stats["total_documents"] == 2
        assert stats["backend"] == "lancedb"
        assert stats["by_module"]["login-api"] == 1
        assert stats["by_type"]["known_issue"] == 1


class TestSafeWhere:
    def test_escapes_single_quotes(self):
        clause = LanceRAGStore._safe_where("id", "o'brien")
        assert clause == "id = 'o''brien'"


def test_json_fallback_survives_restart_and_persists_update_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(LanceRAGStore, '_init_db', lambda self: None)
    path = str(tmp_path / 'fallback')
    first = LanceRAGStore(path)
    first.add(make_doc(doc_id='durable'))
    second = LanceRAGStore(path)
    assert second.get('durable').content == CONTENT
    second.update('durable', title='updated')
    assert LanceRAGStore(path).get('durable').title == 'updated'
    assert second.delete('durable')
    assert LanceRAGStore(path).get('durable') is None


def test_reading_a_document_cannot_mutate_persisted_or_cached_memory(store):
    store.add(make_doc(doc_id='copy'))
    doc = store.get('copy')
    doc.metadata['unsaved'] = 'mutation'
    doc.title = 'unsaved'
    assert store.get('copy').title != 'unsaved'
    assert 'unsaved' not in store.get('copy').metadata


def test_cold_rag_pagination_is_globally_newest_first_and_stats_are_persistent(store):
    for i in range(5):
        store.add(make_doc(doc_id=f'page-{i}', created_at=f'2026-08-{i+1:02d}'))
    cold = LanceRAGStore(db_path=store.db_path)
    assert [d.id for d in cold.list_all(limit=2, offset=1)] == ['page-3', 'page-2']
    assert cold.get_stats()['by_module']['login-api'] == 5


def test_legacy_manual_archive_does_not_claim_numeric_validation(store):
    from app.services.ai.rag.archiver import RAGArchiver
    doc_id = RAGArchiver(store).archive_analysis_conclusion(CONTENT * 8, 'api', 'v1')
    assert store.get(doc_id).metadata['validated'] is False


def test_filtered_rag_queries_choose_newest_before_limit(store):
    store.add(make_doc(doc_id='old', created_at='2026-01-01'))
    store.add(make_doc(doc_id='new', created_at='2026-08-27'))
    cold = LanceRAGStore(store.db_path)
    assert [doc.id for doc in cold.search_by_module('login-api', limit=1)] == ['new']
    assert [doc.id for doc in cold.search_by_type('analysis_conclusion', limit=1)] == ['new']


@pytest.mark.asyncio
async def test_rag_api_filters_with_real_cold_store_in_worker(store, monkeypatch):
    from app.api import routes
    store.add(make_doc(doc_id='wanted', module='orders', doc_type='known_issue'))
    store.add(make_doc(doc_id='other-module', module='users', doc_type='known_issue'))
    store.add(make_doc(doc_id='other-type', module='orders', doc_type='raw_report'))
    cold = LanceRAGStore(store.db_path)
    monkeypatch.setattr(routes, 'rag_store', cold)
    result = await routes.list_rag_documents(doc_type='known_issue', module='orders', keyword='p99', limit=100)
    assert [doc['id'] for doc in result['documents']] == ['wanted']
    result = await routes.search_rag(q='p99', module='missing', doc_type='known_issue', limit=100)
    assert result['documents'] == []


def test_keyword_search_cold_fallback_does_not_depend_on_cache(store, monkeypatch):
    store.add(make_doc(doc_id='found', content='This needle is present in a persisted performance observation.'))
    cold = LanceRAGStore(store.db_path)
    assert [doc.id for doc in cold.search_by_keywords(['needle'])] == ['found']


def test_json_fallback_module_filter_is_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(LanceRAGStore, '_init_db', lambda self: None)
    store = LanceRAGStore(str(tmp_path / 'fallback'))
    store.add(make_doc(doc_id='wanted', module='api'))
    store.add(make_doc(doc_id='unrelated', module='private-api'))
    assert [doc.id for doc in store.search_by_module('api')] == ['wanted']


def test_fallback_filters_metadata_before_limiting_keyword_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(LanceRAGStore, '_init_db', lambda self: None)
    store = LanceRAGStore(str(tmp_path / 'fallback'))
    store._docs_cache = {str(i): make_doc(doc_id=str(i), module='other') for i in range(201)}
    store._docs_cache['wanted'] = make_doc(doc_id='wanted', module='api')
    assert [doc.id for doc in store.hybrid_search(keywords=['p99'], module='api', limit=1)] == ['wanted']


def test_latest_analysis_filters_type_before_module_limit(store):
    for i in range(21):
        store.add(make_doc(doc_id=f'other-{i}', doc_type='known_issue', created_at='2026-08-27'))
    store.add(make_doc(doc_id='analysis', created_at='2026-01-01'))
    cold = LanceRAGStore(store.db_path)
    assert cold.get_latest_analysis('login-api').id == 'analysis'


def test_module_versions_are_not_limited_to_recent_200_documents(store):
    documents = [make_doc(doc_id=str(i), version=f'v{i}', created_at='2026-08-27') for i in range(201)]
    store._table.add([store._doc_to_row(doc, FAKE_VECTOR) for doc in documents])
    cold = LanceRAGStore(store.db_path)
    assert set(cold.get_module_versions('login-api')) == {f'v{i}' for i in range(201)}


class TestSearchIsolation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize('metadata', [{}, {'execution_id': '', 'module': '', 'version': ''},
                                        {'execution_id': 'source', 'module': 'api', 'version': 'v1'}])
    async def test_archival_accepts_missing_or_matching_analysis_identity(self, store, monkeypatch, metadata):
        import importlib
        from unittest.mock import AsyncMock, Mock
        from app.models.schemas import FactSheet, StructuredAnalysis
        module = importlib.import_module('app.services.ai.rag.archiver')
        fact = FactSheet(execution_id='source', module='api', version='v1', context_scope='scope',
                         jtl_sha256='digest', jtl_path='evidence')
        analysis = StructuredAnalysis(**metadata, overall_assessment='No causal assertion is made here. ' * 5)
        monkeypatch.setattr(module.fact_ledger, 'save', AsyncMock(return_value=True))
        archiver = module.RAGArchiver(store)
        archiver.archive_analysis_conclusion = Mock(return_value='conclusion')
        result = await archiver.archive_analysis_result(analysis, fact)
        assert result['fact_archived'] and result['conclusion_archived'] == 'conclusion'
        assert result['errors'] == [] and result['rejected_reason'] is None

    @pytest.mark.asyncio
    async def test_concurrent_archival_deduplicates_hypotheses_without_false_failure(self, store, tmp_path, monkeypatch):
        import asyncio
        import importlib
        from unittest.mock import AsyncMock, Mock
        from app.models.schemas import FactSheet, StructuredAnalysis, Hypothesis
        from app.services.ai.memory.hypothesis_ledger import HypothesisLedger
        module = importlib.import_module('app.services.ai.rag.archiver')
        ledger = HypothesisLedger(str(tmp_path / 'archive-hypotheses.db'))
        monkeypatch.setattr(module, 'hypothesis_ledger', ledger)
        monkeypatch.setattr(module.fact_ledger, 'save', AsyncMock(return_value=True))
        original_get, both_read = ledger.get, asyncio.Event()
        reads = 0

        async def racing_get(hypothesis_id):
            nonlocal reads
            result = await original_get(hypothesis_id)
            reads += 1
            if reads == 2:
                both_read.set()
            await both_read.wait()
            return result

        get = AsyncMock(side_effect=racing_get)
        monkeypatch.setattr(ledger, 'get', get)
        fact = FactSheet(execution_id='source', module='api', context_scope='scope',
                         jtl_sha256='digest', jtl_path='evidence')
        analysis = StructuredAnalysis(hypotheses=[Hypothesis(hypothesis_id='h', source_execution_id='source',
                                                            module='api', statement='prediction')])
        archiver = module.RAGArchiver(store)
        archiver.archive_analysis_conclusion = Mock(return_value='conclusion')
        try:
            results = await asyncio.wait_for(asyncio.gather(
                archiver.archive_analysis_result(analysis, fact),
                archiver.archive_analysis_result(analysis, fact)), timeout=10)
            assert all(not result['errors'] for result in results)
            assert results[0]['hypotheses_archived'] == results[1]['hypotheses_archived']
            assert len(await ledger.list_for_module('api')) == 1
            get.assert_not_awaited()
        finally:
            await ledger.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('phase', ['fact_save', 'hypothesis_add'])
    async def test_archival_uses_consistent_nested_input_snapshots(self, store, monkeypatch, phase):
        import importlib
        from unittest.mock import AsyncMock, Mock
        from app.models.schemas import FactSheet, StructuredAnalysis, Hypothesis
        from app.services.jmeter.report_parser import ReportParser
        module = importlib.import_module('app.services.ai.rag.archiver')
        summary = ReportParser()._empty_summary()
        summary.total_requests = 100
        fact = FactSheet(execution_id='source', module='api', version='v1', context_scope='scope',
                         jtl_sha256='digest', jtl_path='evidence', percentiles={'p99': 100}, raw_summary=summary)
        analysis = StructuredAnalysis(execution_id='source', module='api', version='v1',
            overall_assessment='An initial report containing a tentative explanation. ' * 3,
            metric_statements=['p99 = 100ms'],
            hypotheses=[Hypothesis(hypothesis_id='h', source_execution_id='source', module='api',
                                   statement='initial interpretation', supporting_facts=['p99'])])
        saved_facts, saved_hypotheses = [], []

        def mutate():
            fact.execution_id = 'other'
            fact.module = 'other-api'
            fact.version = 'v2'
            fact.context_scope = 'other-scope'
            fact.percentiles['p99'] = 9000
            fact.raw_summary.total_requests = 99999
            analysis.metric_statements.append('TPS = 99999')
            analysis.hypotheses[0].statement = 'changed interpretation'
            analysis.hypotheses[0].supporting_facts.append('throughput')

        async def save(value):
            saved_facts.append(value.model_copy(deep=True))
            if phase == 'fact_save':
                mutate()
            return True

        async def add(value, **kwargs):
            saved_hypotheses.append(value.model_copy(deep=True))
            if phase == 'hypothesis_add':
                mutate()
            return value.hypothesis_id

        monkeypatch.setattr(module.fact_ledger, 'save', save)
        monkeypatch.setattr(module.hypothesis_ledger, 'get', AsyncMock(return_value=None))
        monkeypatch.setattr(module.hypothesis_ledger, 'add', add)
        archiver = module.RAGArchiver(store)
        archiver.archive_analysis_conclusion = Mock(return_value='conclusion')
        archiver.archive_raw_report = Mock(return_value='raw')
        result = await archiver.archive_analysis_result(analysis, fact)
        assert result['fact_archived'] and not result['errors'] and not result['rejected_reason']
        assert result['validation'].is_valid
        saved = saved_hypotheses[0]
        assert saved.source_execution_id == 'source' and saved.statement == 'initial interpretation'
        assert saved.supporting_facts == ['p99']
        conclusion = archiver.archive_analysis_conclusion.call_args.kwargs
        raw = archiver.archive_raw_report.call_args.kwargs
        assert 'changed interpretation' not in conclusion['analysis_text']
        assert '99999' not in conclusion['analysis_text']
        for projection in (conclusion, raw):
            assert projection['execution_id'] == 'source'
            assert projection['module'] == 'api' and projection['version'] == 'v1'
        assert raw['report_data']['total_requests'] == saved_facts[0].raw_summary.total_requests == 100
        assert fact.execution_id == 'other'

    @pytest.mark.asyncio
    @pytest.mark.parametrize('field', ['execution_id', 'module', 'version'])
    async def test_archival_rejects_conflicting_analysis_identity_but_keeps_fact(self, store, monkeypatch, field):
        import importlib
        from unittest.mock import AsyncMock, Mock
        from app.models.schemas import FactSheet, StructuredAnalysis, Hypothesis
        module = importlib.import_module('app.services.ai.rag.archiver')
        fact = FactSheet(execution_id='source', module='api', version='v1', context_scope='scope',
                         jtl_sha256='digest', jtl_path='evidence')
        analysis = StructuredAnalysis(execution_id='source', module='api', version='v1',
            overall_assessment='A substantial report that must not be attributed to another execution. ' * 3,
            hypotheses=[Hypothesis(hypothesis_id='h', source_execution_id='source', module='api', statement='prediction')])
        setattr(analysis, field, 'other')
        save, add = AsyncMock(return_value=True), AsyncMock(return_value='h')
        monkeypatch.setattr(module.fact_ledger, 'save', save)
        monkeypatch.setattr(module.hypothesis_ledger, 'add', add)
        monkeypatch.setattr(module.hypothesis_ledger, 'get', AsyncMock(return_value=None))
        archiver = module.RAGArchiver(store)
        archiver.archive_analysis_conclusion = Mock(return_value='conclusion')
        archiver.archive_raw_report = Mock(return_value='raw')
        result = await archiver.archive_analysis_result(analysis, fact)
        assert result['fact_archived'] and result['errors']
        assert field in result['rejected_reason']
        assert result['hypotheses_archived'] == []
        assert result['conclusion_archived'] is None and result['raw_report_archived'] is None
        save.assert_awaited_once()
        add.assert_not_awaited()
        archiver.archive_analysis_conclusion.assert_not_called()
        archiver.archive_raw_report.assert_not_called()


    @pytest.mark.asyncio
    @pytest.mark.parametrize('write_result', [None, OSError('fixture disk failure')])
    async def test_conclusion_failure_preserves_archived_fact_status(self, store, monkeypatch, write_result):
        from unittest.mock import AsyncMock, Mock
        import importlib
        from app.models.schemas import FactSheet, StructuredAnalysis
        module = importlib.import_module('app.services.ai.rag.archiver')
        fact = FactSheet(execution_id='source', module='api', context_scope='scope',
                         jtl_sha256='digest', jtl_path='evidence')
        monkeypatch.setattr(module.fact_ledger, 'save', AsyncMock(return_value=True))
        archiver = module.RAGArchiver(store)
        archiver.archive_analysis_conclusion = Mock(return_value=write_result,
            side_effect=write_result if isinstance(write_result, Exception) else None)
        result = await archiver.archive_analysis_result(StructuredAnalysis(
            overall_assessment='A possible bottleneck needs further controlled measurements. ' * 3), fact)
        assert result['fact_archived'] and result['conclusion_archived'] is None
        assert result.get('errors') and 'conclusion' in result['errors'][0].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('failed_write', ['fact', 'hypothesis'])
    async def test_archival_reports_caught_persistence_failures(self, store, monkeypatch, failed_write):
        from unittest.mock import AsyncMock
        import importlib
        from app.models.schemas import FactSheet, StructuredAnalysis, Hypothesis
        module = importlib.import_module('app.services.ai.rag.archiver')
        fact = FactSheet(execution_id='source', module='api', context_scope='scope',
                         jtl_sha256='digest', jtl_path='evidence')
        analysis = StructuredAnalysis(hypotheses=[Hypothesis(hypothesis_id='fixture', source_execution_id='source', module='api',
            statement='A possible bottleneck requires an independent experiment before confirmation.')])
        monkeypatch.setattr(module.fact_ledger, 'save', AsyncMock(return_value=failed_write != 'fact'))
        monkeypatch.setattr(module.hypothesis_ledger, 'get', AsyncMock(return_value=None))
        add = AsyncMock(side_effect=OSError('fixture disk failure'))
        monkeypatch.setattr(module.hypothesis_ledger, 'add', add)
        result = await module.RAGArchiver(store).archive_analysis_result(analysis, fact)
        assert result.get('errors') and failed_write in result['errors'][0].lower()
        assert result['fact_archived'] is (failed_write != 'fact')
        assert result['hypotheses_archived'] == []
        if failed_write == 'fact':
            add.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_archival_resets_model_verification_and_stops_after_fact_save_failure(self, store, monkeypatch):
        from unittest.mock import AsyncMock
        import importlib
        from app.models.schemas import FactSheet, StructuredAnalysis, Hypothesis, HypothesisStatus
        module = importlib.import_module('app.services.ai.rag.archiver')
        fact = FactSheet(execution_id='source', module='api', context_scope='scope', jtl_sha256='digest', jtl_path='evidence')
        model_hyp = Hypothesis(hypothesis_id='invented-id', source_execution_id='wrong', module='wrong',
                              statement='This is an untested causal interpretation', status=HypothesisStatus.CONFIRMED,
                              verified_by_execution_id='invented-proof')
        analysis = StructuredAnalysis(hypotheses=[model_hyp])
        save = AsyncMock(return_value=True)
        add = AsyncMock(return_value='new-hyp')
        monkeypatch.setattr(module.fact_ledger, 'save', save)
        monkeypatch.setattr(module.hypothesis_ledger, 'add', add)
        monkeypatch.setattr(module.hypothesis_ledger, 'get', AsyncMock(return_value=None))
        archiver = module.RAGArchiver(store)
        await archiver.archive_analysis_result(analysis, fact)
        saved = add.call_args.args[0]
        assert saved.status == HypothesisStatus.UNTESTED
        assert saved.verified_by_execution_id is None
        assert saved.source_execution_id == 'source'
        add.reset_mock(); save.return_value = False
        outcome = await archiver.archive_analysis_result(analysis, fact)
        add.assert_not_awaited()
        assert outcome['rejected_reason']

    def test_scope_filter_is_applied_before_the_limit(self, store):
        wanted = make_doc(doc_id='scoped-wanted'); wanted.context_scope = 'scope-one'
        other = make_doc(doc_id='scoped-other'); other.context_scope = 'scope-two'
        store.add(other); store.add(wanted)
        assert [doc.id for doc in store.hybrid_search(query_text='p99', limit=1,
                                                      context_scope='scope-one')] == ['scoped-wanted']

    def test_archiver_does_not_overwrite_another_execution(self, store):
        from app.services.ai.rag.archiver import RAGArchiver
        archiver = RAGArchiver(store)
        args = dict(doc_type='analysis_conclusion', module='api', version='v1', title='same title',
                    content=CONTENT * 3, keywords=['p99'])
        first = archiver._upsert(**args, source_execution_id='run-a')
        second = archiver._upsert(**args, source_execution_id='run-b')
        again = archiver._upsert(**args, source_execution_id='run-a')
        assert first != second and again == first
        assert store.count() == 2
        assert store.get(first).source_execution_id == 'run-a'

    def test_cold_metadata_search_reads_persisted_rows(self, store):
        store.add(make_doc(doc_id='persisted'))
        cold = LanceRAGStore(db_path=store.db_path)
        assert not cold._docs_cache
        assert [d.id for d in cold.hybrid_search(module='login-api', version='v1')] == ['persisted']

    def test_search_keeps_only_documents_with_the_query(self, store):
        store.add(make_doc(doc_id='relevant', content='The unique needle is supported by this saved observation.'))
        store.add(make_doc(doc_id='unrelated'))
        cold = LanceRAGStore(db_path=store.db_path)
        assert [d.id for d in cold.full_text_search('unique needle')] == ['relevant']
        assert store.full_text_search('NO_MATCH_EXPECTED') == []

    def test_search_applies_all_metadata_filters(self, store):
        store.add(make_doc(doc_id='wanted', version='v1'))
        store.add(make_doc(doc_id='other-version', version='v2'))
        store.add(make_doc(doc_id='other-module', module='different', version='v1'))
        result = store.hybrid_search(query_text='p99', module='login-api',
                                     doc_type='analysis_conclusion', version='v1')
        assert [d.id for d in result] == ['wanted']

    def test_keyword_filter_handles_quotes_as_data(self, store):
        store.add(make_doc(doc_id='quoted', content="The client's p99 stayed below the configured threshold."))
        cold = LanceRAGStore(db_path=store.db_path)
        assert [d.id for d in cold.full_text_search("client's")] == ['quoted']
        assert cold.full_text_search("' OR true --") == []


def test_chinese_terms_find_chinese_documents(store):
    store.add(make_doc(doc_id='timeout', content='订单接口在高峰期频繁超时，连接池耗尽后错误率升到 5%。'))
    store.add(make_doc(doc_id='other'))
    cold = LanceRAGStore(db_path=store.db_path)
    assert [d.id for d in cold.hybrid_search(query_text='超时')] == ['timeout']
    assert cold.hybrid_search(query_text='内存泄漏') == []
