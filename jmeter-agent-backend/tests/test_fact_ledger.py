"""Tests for FactLedger — deterministic ground-truth computation."""

import os
import asyncio
import tempfile
import pytest
import pytest_asyncio

from app.services.ai.memory.fact_ledger import FactLedger
from app.services.jmeter.report_parser import ReportParser


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['strict', 'legacy', 'cancel'])
@pytest.mark.parametrize('kind, phase', [
    (kind, phase) for kind in ('fact_load', 'fact_list', 'hyp_get', 'hyp_list')
    for phase in ('initialize', 'connection', 'execute', 'read')
] + [(kind, phase) for kind in ('fact_list', 'hyp_list') for phase in ('enter', 'exit')])
async def test_ledger_read_contract_fault_boundaries(tmp_path, monkeypatch, kind, phase, mode):
    from unittest.mock import AsyncMock, Mock
    from app.services.ai.memory.hypothesis_ledger import HypothesisLedger
    from app.models.schemas import FactSheet, Hypothesis
    ledger = (FactLedger if kind.startswith('fact') else HypothesisLedger)(str(tmp_path / 'unused.db'))
    item = (FactSheet(execution_id='good', module='api') if kind.startswith('fact') else
            Hypothesis(hypothesis_id='good', source_execution_id='old', module='api', statement='prediction'))
    # Decode one valid row before a failing second fetch; row decoding is covered separately on real DBs.
    monkeypatch.setattr(ledger, '_verified_row' if kind.startswith('fact') else '_row_to_hypothesis',
                        AsyncMock(return_value=item) if kind.startswith('fact') else Mock(return_value=item))
    error = asyncio.CancelledError() if mode == 'cancel' else OSError('PRIVATE_SENTINEL database')
    class Query:
        seen = False
        def __await__(self):
            async def value():
                return self
            return value().__await__()
        async def __aenter__(self):
            if phase == 'enter':
                raise error
            return self
        async def __aexit__(self, *args):
            if phase == 'exit':
                raise error
        async def fetchone(self):
            if phase == 'read':
                raise error
            return ('valid-row',)
        def __aiter__(self):
            return self
        async def __anext__(self):
            if not self.seen:
                self.seen = True
                return ('valid-row',)
            if phase == 'read':
                raise error
            raise StopAsyncIteration
    db = Mock()
    db.execute.side_effect = error if phase == 'execute' else lambda *args: Query()
    monkeypatch.setattr(ledger, '_ensure_table', AsyncMock(side_effect=error if phase == 'initialize' else None))
    monkeypatch.setattr(ledger, '_get_db', AsyncMock(return_value=db, side_effect=error if phase == 'connection' else None))
    method = {'fact_load': 'load', 'fact_list': 'get_facts_for_module_version',
              'hyp_get': 'get', 'hyp_list': 'list_for_module'}[kind]
    async def read():
        return await getattr(ledger, method)('api', **({'strict': True} if mode != 'legacy' else {}))
    if mode == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await read()
    elif mode == 'strict':
        with pytest.raises(Exception) as failure:
            await read()
        assert type(failure.value).__name__ == 'LedgerReadError'
        assert 'PRIVATE_SENTINEL' not in str(failure.value)
    elif phase in ('initialize', 'connection'):
        with pytest.raises(OSError, match='PRIVATE_SENTINEL'):
            await read()
    else:
        assert await read() == ([] if kind.endswith('list') else None)


@pytest.fixture
def registered_evidence(tmp_path, monkeypatch, sample_jtl):
    import shutil
    from unittest.mock import AsyncMock
    from app.core.config import settings
    from app.services.db_store import db_store
    root = tmp_path / 'reports'
    folder = root / 'run-A'
    folder.mkdir(parents=True)
    jtl = folder / 'result.jtl'
    shutil.copyfile(sample_jtl, jtl)
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(root))
    monkeypatch.setattr('app.core.config.load_paths_config', lambda: {})
    record = {'id': 'run-A', 'project_id': 'project-A', 'test_plan_id': 'plan-A',
              'status': 'completed', 'report_dir': str(folder), 'context_snapshot': {
                  'project_id': 'project-A', 'test_plan_id': 'plan-A', 'environment_fingerprint': 'fingerprint',
                  'test_config': {'test_type': 'baseline', 'concurrent_users': 10}}}
    plan = {'id': 'plan-A', 'project_id': 'project-A', 'name': 'Edited name',
            'test_type': 'stress', 'test_config': {'test_type': 'stress'}}
    project = {'id': 'project-A', 'name': 'Project'}
    monkeypatch.setattr(db_store, 'get_execution', AsyncMock(return_value=record))
    monkeypatch.setattr(db_store, 'get_test_plan', AsyncMock(return_value=plan))
    monkeypatch.setattr(db_store, 'get_project', AsyncMock(return_value=project))
    return record, plan, project, jtl


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_registered_analysis_end_to_end_jtl_to_scoped_persistence(registered_evidence, temp_ledger, tmp_path, monkeypatch, stream):
    import importlib
    import json
    import httpx
    from unittest.mock import Mock, AsyncMock
    from fastapi import FastAPI
    from app.api import routes
    from app.services.ai.agent.types import PlanStep
    from app.services.ai.agent.planner import TaskPlanner
    from app.services.ai.memory.provenance import snapshot_scope
    from app.services.storage import store as execution_cache
    analysis_mod = importlib.import_module('app.services.ai.agents.analysis_agent')
    archiver_mod = importlib.import_module('app.services.ai.rag.archiver')
    rag_mod = importlib.import_module('app.services.ai.rag.store')
    monkeypatch.setattr(rag_mod, '_compute_embedding', lambda _: [0.01] * rag_mod.LanceRAGStore.VECTOR_DIM)
    rag = rag_mod.LanceRAGStore(db_path=str(tmp_path / 'rag'))
    monkeypatch.setattr(archiver_mod, 'rag_store', rag)
    monkeypatch.setattr(analysis_mod, 'fact_ledger', temp_ledger)
    monkeypatch.setattr(archiver_mod, 'fact_ledger', temp_ledger)
    monkeypatch.setattr(analysis_mod.hypothesis_ledger, 'list_for_module', AsyncMock(return_value=[]))
    monkeypatch.setattr(analysis_mod.hypothesis_ledger, 'evaluate_module_hypotheses', AsyncMock(return_value=[]))
    monkeypatch.setattr(execution_cache, 'get_execution', lambda _: None)
    adapter = Mock()
    adapter.prepare_chat_messages.side_effect = lambda messages, **kwargs: messages
    adapter.chat = AsyncMock(side_effect=['Measured observation', json.dumps({'overall_assessment':
        'The measured results provide a baseline; additional experiments are required to establish any root cause.'})])
    monkeypatch.setattr(routes.AIAdapterFactory, 'create', lambda _: adapter)
    monkeypatch.setattr(TaskPlanner, 'plan', AsyncMock(return_value=[PlanStep(1, 'explain evidence')]))
    app = FastAPI()
    app.include_router(routes.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            response = await client.post('/agent/run' + ('/stream' if stream else ''), json={
                'agent_type': 'analysis', 'execution_id': 'run-A', 'task': 'Explain this run',
                'context': 'module=untrusted, version=untrusted, jtl_path=untrusted'})
        assert response.status_code == 200
        result = (json.loads(response.text.strip().split('\n\n')[-1].removeprefix('data: '))['data']
                  if stream else response.json())
        assert result['success'], result
        fact = await temp_ledger.load('run-A')
        assert fact and fact.total_requests == ReportParser().parse_jtl(str(registered_evidence[3])).total_requests
        assert (fact.module, fact.version, fact.test_type) == ('plan-A', 'unknown', 'baseline')
        assert fact.context_scope == snapshot_scope(registered_evidence[0]['context_snapshot'])
        for key in ('conclusion_archived', 'raw_report_archived'):
            doc = rag.get(result['analysis']['archive'][key])
            assert doc and doc.context_scope == fact.context_scope and doc.source_execution_id == 'run-A'
        assert adapter.chat.await_count == 2
    finally:
        rag_mod._ZERO_VECTOR_IDS.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
async def test_registered_analysis_resolves_only_server_evidence(registered_evidence, monkeypatch, status):
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    from app.services.ai.memory.provenance import snapshot_scope
    from app.services.storage import store
    record, plan, project, jtl = registered_evidence
    record['status'] = status
    monkeypatch.setattr(store, 'get_execution', lambda key: None)
    result = await AnalysisAgent.resolve_execution('run-A')
    assert result['execution_id'] == 'run-A' and result['module'] == 'plan-A'
    assert result['version'] == 'unknown' and result['test_type'] == 'baseline'
    assert result['jtl_path'] == os.path.realpath(jtl)
    assert result['context_scope'] == snapshot_scope(record['context_snapshot'])


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['pending', 'running', 'missing', 'no_scope', 'snapshot_project',
    'snapshot_plan', 'plan_project', 'no_plan', 'no_project', 'foreign_path', 'missing_jtl', 'empty_jtl',
    'invalid_snapshot', 'invalid_config'])
async def test_registered_analysis_rejects_unusable_or_mismatched_evidence(registered_evidence, case):
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    from app.services.db_store import db_store
    record, plan, project, jtl = registered_evidence
    if case in ('pending', 'running'): record['status'] = case
    elif case == 'missing': db_store.get_execution.return_value = None
    elif case == 'no_scope': record['context_snapshot'] = None
    elif case == 'snapshot_project': record['context_snapshot']['project_id'] = 'other'
    elif case == 'snapshot_plan': record['context_snapshot']['test_plan_id'] = 'other'
    elif case == 'plan_project': plan['project_id'] = 'other'
    elif case == 'no_plan': db_store.get_test_plan.return_value = None
    elif case == 'no_project': db_store.get_project.return_value = None
    elif case == 'foreign_path': record['report_dir'] = str(jtl.parent.parent.parent)
    elif case == 'missing_jtl': jtl.unlink()
    elif case == 'empty_jtl': jtl.write_text('')
    elif case == 'invalid_snapshot': record['context_snapshot'] = ['invalid']
    elif case == 'invalid_config': record['context_snapshot']['test_config'] = ['invalid']
    with pytest.raises(ValueError) as error:
        await AnalysisAgent.resolve_execution('run-A')
    assert error.value.status_code == (404 if case == 'missing' else 409)


@pytest.mark.asyncio
@pytest.mark.parametrize('redirect_directory', [False, True])
async def test_registered_analysis_rejects_redirected_evidence_paths(registered_evidence, monkeypatch, redirect_directory):
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    _, _, _, jtl = registered_evidence
    target = jtl.parent.parent / 'run-B' / 'result.jtl' if redirect_directory else jtl.parent / 'other.jtl'
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(jtl.read_bytes())
    realpath = os.path.realpath
    original = os.path.normcase(str(jtl.parent if redirect_directory else jtl))
    destination = str(target.parent if redirect_directory else target)
    def redirected(path, *args, **kwargs):
        resolved = realpath(path, *args, **kwargs)
        if os.path.normcase(resolved) == original or os.path.normcase(resolved).startswith(original + os.sep):
            return destination + resolved[len(original):]
        return resolved
    monkeypatch.setattr(os.path, 'realpath', redirected)
    with pytest.raises(ValueError) as error:
        await AnalysisAgent.resolve_execution('run-A')
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize('execution_id', ['../run', 'CON', 'bad/name', ''])
async def test_registered_analysis_invalid_id_does_not_read_database(registered_evidence, execution_id):
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    from app.services.db_store import db_store
    with pytest.raises(ValueError) as error:
        await AnalysisAgent.resolve_execution(execution_id)
    assert error.value.status_code == 400
    db_store.get_execution.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['initialize', 'hash', 'connection', 'commit'])
async def test_fact_save_uses_one_input_snapshot(temp_ledger, sample_jtl, monkeypatch, phase):
    fact = temp_ledger.compute_from_jtl(sample_jtl, execution_id='original', module='api', version='v1')
    fact.context_scope = 'scope-a'
    expected = fact.model_dump()
    await temp_ledger._ensure_table()
    db = await temp_ledger._get_db()

    def mutate():
        fact.execution_id = 'other'
        fact.module = 'other-api'
        fact.version = 'v2'
        fact.context_scope = 'scope-b'
        fact.jtl_path = 'missingmissing.jtl'
        fact.percentiles['p99'] = 99999
        fact.raw_summary.total_requests = 99999

    target, name = {
        'initialize': (temp_ledger, '_ensure_table'),
        'hash': (asyncio, 'to_thread'),
        'connection': (temp_ledger, '_get_db'),
        'commit': (db, 'commit'),
    }[phase]
    original = getattr(target, name)
    calls = 0

    async def suspend_and_mutate(*args, **kwargs):
        nonlocal calls
        value = await original(*args, **kwargs)
        calls += 1
        if phase not in ('connection', 'commit') or calls == 2:
            mutate()  # Skip the initialization call when testing the write phase.
        return value

    with monkeypatch.context() as patch:
        patch.setattr(target, name, suspend_and_mutate)
        assert await temp_ledger.save(fact)
    loaded = await temp_ledger.load('original')
    assert loaded is not None and loaded.model_dump() == expected
    assert await temp_ledger.load('other') is None
    assert [f.execution_id for f in await temp_ledger.get_facts_for_module_version(
        'api', 'v1', context_scope='scope-a')] == ['original']


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('rollback_fails', [False, True])
async def test_failed_fact_save_does_not_replace_history_on_next_read(temp_ledger, monkeypatch, cancelled, rollback_fails):
    from unittest.mock import AsyncMock
    from app.models.schemas import FactSheet
    original = FactSheet(execution_id='existing', module='api', total_requests=10)
    assert await temp_ledger.save(original)
    db = await temp_ledger._get_db()
    original_commit = db.commit
    commits = 0
    async def fail_write_commit():
        nonlocal commits
        commits += 1
        if commits == 2:
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError('simulated commit failure')
        await original_commit()
    monkeypatch.setattr(db, 'commit', fail_write_commit)
    if rollback_fails:
        monkeypatch.setattr(db, 'rollback', AsyncMock(side_effect=RuntimeError('rollback failed')))
    replacement = original.model_copy(update={'total_requests': 999})
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await temp_ledger.save(replacement)
    else:
        assert not await temp_ledger.save(replacement)
    loaded = await temp_ledger.load('existing')
    assert loaded.total_requests == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['fact', 'hypothesis'])
@pytest.mark.parametrize('cancelled', [False, True])
async def test_ledger_initialization_failure_closes_connection_and_can_retry(monkeypatch, tmp_path, kind, cancelled):
    from unittest.mock import AsyncMock
    import aiosqlite
    from app.services.ai.memory.hypothesis_ledger import HypothesisLedger
    ledger = (FactLedger if kind == 'fact' else HypothesisLedger)(str(tmp_path / 'unused.db'))
    first, second = AsyncMock(), AsyncMock()
    first.execute.side_effect = asyncio.CancelledError() if cancelled else RuntimeError('PRAGMA failed')
    connect = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(aiosqlite, 'connect', connect)
    try:
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
            await ledger._get_db()
        assert ledger._db_conn is None
        first.close.assert_awaited_once()
        assert await ledger._get_db() is second
        assert await ledger._get_db() is second
        assert connect.await_count == 2 and second.execute.await_count == 2
    finally:
        await ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_count', [1, 70])
@pytest.mark.parametrize('strict', [False, True])
async def test_fact_history_skips_corrupt_rows_and_fills_valid_limit(temp_ledger, invalid_count, strict):
    from app.models.schemas import FactSheet
    fact = FactSheet(execution_id='valid', module='api', version='v1', context_scope='scope-a')
    assert await temp_ledger.save(fact)
    db = await temp_ledger._get_db()
    await db.executemany('''INSERT INTO fact_sheets
        (execution_id, module, version, schema_version, computed_at, facts_json)
        VALUES (?, 'api', 'v1', '1.0', '2099-01-01', ?)''',
        [(f'bad-{i:03}', 'broken-json') for i in range(invalid_count)])
    await db.commit()
    for scope in (None, 'scope-a'):
        result = await temp_ledger.get_facts_for_module_version('api', 'v1', limit=1, context_scope=scope,
                                                             **({'strict': True} if strict else {}))
        assert [f.execution_id for f in result] == ['valid']
    assert (await temp_ledger.get_latest_fact('api', 'v1')).execution_id == 'valid'


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [False, True])
async def test_latest_fact_ignores_stale_source_and_unrelated_scope(temp_ledger, tmp_path, sample_jtl, strict):
    from pathlib import Path
    from datetime import datetime
    from app.models.schemas import FactSheet
    good = FactSheet(execution_id='good', module='api', version='v1', context_scope='scope-a')
    assert await temp_ledger.save(good)
    copied = tmp_path / 'copied.jtl'
    copied.write_bytes(Path(sample_jtl).read_bytes())
    stale = temp_ledger.compute_from_jtl(str(copied), 'stale', module='api', version='v1')
    stale.context_scope = 'scope-a'
    stale.computed_at = datetime(2098, 1, 1)
    assert await temp_ledger.save(stale)
    copied.write_text('source changed', encoding='utf-8')
    other = good.model_copy(update={'execution_id': 'other', 'context_scope': 'scope-b',
                                    'computed_at': datetime(2099, 1, 1)})
    assert await temp_ledger.save(other)
    result = await temp_ledger.get_facts_for_module_version('api', 'v1', limit=1, context_scope='scope-a',
                                                         **({'strict': True} if strict else {}))
    assert [f.execution_id for f in result] == ['good']


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [False, True])
async def test_fact_row_identity_mismatch_cannot_return_another_execution(temp_ledger, strict):
    from app.models.schemas import FactSheet
    fact = FactSheet(execution_id='requested', module='api')
    assert await temp_ledger.save(fact)
    db = await temp_ledger._get_db()
    wrong = fact.model_copy(update={'execution_id': 'not-requested'})
    await db.execute('UPDATE fact_sheets SET facts_json = ?', (wrong.model_dump_json(),))
    await db.commit()
    options = {'strict': True} if strict else {}
    assert await temp_ledger.load('requested', **options) is None
    assert await temp_ledger.get_facts_for_module_version('api', **options) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [-1, 1.5, True])
@pytest.mark.parametrize('strict', [False, True])
async def test_fact_history_invalid_limit_is_rejected_before_database(temp_ledger, monkeypatch, limit, strict):
    from unittest.mock import AsyncMock
    ensure = AsyncMock(side_effect=AssertionError('should not open database'))
    monkeypatch.setattr(temp_ledger, '_ensure_table', ensure)
    with pytest.raises(ValueError, match='limit'):
        await temp_ledger.get_facts_for_module_version('api', limit=limit, **({'strict': True} if strict else {}))
    ensure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [False, True])
async def test_fact_history_zero_limit_does_not_open_database(temp_ledger, monkeypatch, strict):
    from unittest.mock import AsyncMock
    ensure = AsyncMock(side_effect=AssertionError('should not open database'))
    monkeypatch.setattr(temp_ledger, '_ensure_table', ensure)
    assert await temp_ledger.get_facts_for_module_version('api', limit=0, **({'strict': True} if strict else {})) == []
    ensure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_phase', ['extraction', 'archive', 'evaluation', 'reported_write', 'policy_skip'])
async def test_post_analysis_failure_is_visible_without_losing_partial_results(monkeypatch, failure_phase):
    import importlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.models.schemas import FactSheet, StructuredAnalysis
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    agent = object.__new__(module.AnalysisAgent)
    agent._build_fact_sheet = AsyncMock(return_value=FactSheet(execution_id='fixture', module='api'))
    agent.run = AsyncMock(return_value=SimpleNamespace(success=True, output='analysis text', error=None,
                                                     plan=[], iterations=1))
    agent._extract_structured_analysis = AsyncMock(return_value=StructuredAnalysis(overall_assessment='assessment'))
    agent.archiver = SimpleNamespace(archive_analysis_result=AsyncMock(return_value={'fact_archived': True}))
    evaluation = AsyncMock(return_value=[])
    monkeypatch.setattr(module.hypothesis_ledger, 'list_for_module', AsyncMock(return_value=[]))
    monkeypatch.setattr(module.hypothesis_ledger, 'evaluate_module_hypotheses', evaluation)
    if failure_phase in ('reported_write', 'policy_skip'):
        agent.archiver.archive_analysis_result.return_value = {
            'fact_archived': False, 'rejected_reason': 'fixture rejection',
            'errors': ['Fact persistence failed'] if failure_phase == 'reported_write' else []}
    else:
        failing = {'extraction': agent._extract_structured_analysis,
                   'archive': agent.archiver.archive_analysis_result, 'evaluation': evaluation}[failure_phase]
        failing.side_effect = RuntimeError('fixture postprocessing failure')
    result = await agent.analyze_with_rag(module='api')
    if failure_phase == 'policy_skip':
        assert result['success'] and result['error'] is None
    else:
        assert not result['success'] and result['error']
    assert result['output'] == 'analysis text' and result['fact']['execution_id'] == 'fixture'
    if failure_phase in ('reported_write', 'policy_skip'):
        evaluation.assert_not_awaited()
    if failure_phase == 'evaluation':
        assert result['archive']['fact_archived'] is True
    if failure_phase == 'extraction':
        agent.archiver.archive_analysis_result.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['scope', 'facts', 'hypotheses', 'heuristics', 'rag_context', 'base_task'])
@pytest.mark.parametrize('cancelled', [False, True])
async def test_analysis_context_preparation_failure_stops_before_model_and_keeps_fact(monkeypatch, phase, cancelled):
    import importlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from app.models.schemas import FactSheet
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    provenance = importlib.import_module('app.services.ai.memory.provenance')
    agent = object.__new__(module.AnalysisAgent)
    fact = FactSheet(execution_id='run', module='api', total_requests=42)
    agent._build_fact_sheet = AsyncMock(return_value=fact)
    agent.run = AsyncMock()
    agent._extract_structured_analysis = AsyncMock()
    agent.archiver = SimpleNamespace(archive_analysis_result=AsyncMock())
    calls = {key: AsyncMock(return_value=value) for key, value in
             [('scope', 'scope-a'), ('facts', []), ('hypotheses', [])]}
    calls.update({key: Mock(return_value='context') for key in ['heuristics', 'rag_context', 'base_task']})
    monkeypatch.setattr(provenance, 'load_execution_scope', calls['scope'])
    monkeypatch.setattr(module.fact_ledger, 'get_facts_for_module_version', calls['facts'])
    monkeypatch.setattr(module.hypothesis_ledger, 'list_for_module', calls['hypotheses'])
    monkeypatch.setattr(module, 'get_heuristics_prompt', calls['heuristics'])
    agent._build_rag_context = calls['rag_context']
    agent._build_base_task = calls['base_task']
    calls[phase].side_effect = asyncio.CancelledError() if cancelled else OSError('private fixture detail')
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await agent.analyze_with_rag(execution_id='run', module='api')
    else:
        result = await agent.analyze_with_rag(execution_id='run', module='api')
        assert not result['success'] and 'Context preparation failed' in result['error']
        assert 'private fixture detail' not in result['error']
        assert result['fact']['execution_id'] == 'run' and result['fact']['total_requests'] == 42
    agent.run.assert_not_awaited()
    agent._extract_structured_analysis.assert_not_awaited()
    agent.archiver.archive_analysis_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_analysis_retrieval_uses_verified_execution_scope(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import importlib
    from app.models.schemas import FactSheet
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    provenance = importlib.import_module('app.services.ai.memory.provenance')
    agent = object.__new__(module.AnalysisAgent)
    current = FactSheet(execution_id='current', module='api', version='v1')
    agent._build_fact_sheet = AsyncMock(return_value=current)
    agent.run = AsyncMock(return_value=SimpleNamespace(success=False, output='', error=None, plan=[], iterations=0))
    monkeypatch.setattr(provenance, 'load_execution_scope', AsyncMock(return_value='verified-scope'))
    query = AsyncMock(return_value=[current, current.model_copy(update={'execution_id': 'previous'})])
    hypotheses = AsyncMock(return_value=[])
    monkeypatch.setattr(module.fact_ledger, 'get_facts_for_module_version', query)
    monkeypatch.setattr(module.hypothesis_ledger, 'list_for_module', hypotheses)
    await agent.analyze_with_rag(execution_id='current', module='api', version='v1')
    query.assert_awaited_once_with('api', 'v1', limit=5, context_scope='verified-scope')
    assert hypotheses.call_args.kwargs['source_execution_ids'] == ['previous']


@pytest.mark.asyncio
async def test_fact_history_filters_context_before_limit(tmp_path, sample_jtl):
    ledger = FactLedger(str(tmp_path / 'scoped.db'))
    try:
        for execution_id, scope in [('right', 'project-a-env-one'), ('wrong', 'project-b-env-two')]:
            fact = FactLedger.compute_from_jtl(sample_jtl, execution_id, module='api', version='v1')
            fact.context_scope = scope
            assert await ledger.save(fact)
        facts = await ledger.get_facts_for_module_version('api', 'v1', limit=1, context_scope='project-a-env-one')
        assert [fact.execution_id for fact in facts] == ['right']
    finally:
        await ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [False, True])
@pytest.mark.parametrize('source_failure', ['changed', 'missing', 'unreadable'])
async def test_changed_jtl_is_not_loaded_as_verified_fact(tmp_path, sample_jtl, monkeypatch, strict, source_failure):
    from pathlib import Path
    path = tmp_path / 'evidence.jtl'; path.write_bytes(Path(sample_jtl).read_bytes())
    ledger = FactLedger(str(tmp_path / 'facts.db'))
    try:
        fact = FactLedger.compute_from_jtl(str(path), 'evidence', module='api')
        assert await ledger.save(fact)
        if source_failure == 'changed':
            path.write_text('changed evidence')
        elif source_failure == 'missing':
            path.unlink()
        else:
            def unreadable(_):
                raise OSError('fixture inaccessible evidence')
            monkeypatch.setattr(ledger, '_jtl_hash', unreadable)
        options = {'strict': True} if strict else {}
        assert await ledger.load('evidence', **options) is None
        assert await ledger.get_facts_for_module_version('api', **options) == []
        if source_failure != 'unreadable':
            assert not await ledger.save(fact)
    finally:
        await ledger.close()


def timed_summary(rows, fmt):
    from io import StringIO
    parser = ReportParser()
    if fmt == 'csv':
        content = 'timeStamp,elapsed,label,success,bytes,sentBytes\n'
        content += '\n'.join(f'{ts},{elapsed},api,true,100,50' for ts, elapsed in rows)
        return parser._parse_csv_jtl(StringIO(content))
    content = '<testResults>' + ''.join(
        f'<httpSample ts="{ts}" t="{elapsed}" lb="api" s="true" by="100" sb="50"/>'
        for ts, elapsed in rows) + '</testResults>'
    return parser._parse_xml_jtl(StringIO(content))


@pytest.mark.parametrize('fmt', ['csv', 'xml'])
def test_throughput_includes_last_response_end_and_single_sample_duration(fmt):
    summary = timed_summary([(1000, 10000), (2000, 1000)], fmt)
    assert summary.throughput == 0.2
    assert summary.received_bytes_per_sec == 20
    assert summary.per_label['api']['throughput'] == 0.2
    assert timed_summary([(1000, 100)], fmt).throughput == 10


@pytest.mark.parametrize('fmt', ['csv', 'xml'])
def test_out_of_order_samples_use_earliest_origin_and_real_tail_window(fmt):
    ordered = timed_summary([(1000, 1000), (32000, 1000)], fmt)
    shuffled = timed_summary([(32000, 1000), (1000, 1000)], fmt)
    assert shuffled.model_dump() == ordered.model_dump()
    assert [segment.time_range for segment in shuffled.timeline_segments] == ['0-5s', '30-35s']
    assert shuffled.timeline_segments[-1].throughput == 0.5


@pytest.mark.parametrize('operator,actual,limit', [
    ('unknown', 1, 500), ('lt', float('nan'), 500), ('lt', 1, float('nan')),
    ('gte', float('inf'), 10), ('lt', 1, float('inf')),
])
@pytest.mark.parametrize('severity', ['error', 'warn'])
def test_invalid_sla_operator_and_nonfinite_numbers_fail_closed(operator, actual, limit, severity):
    from app.services.jmeter.sla_engine import SLAEngine, SLAConfig, SLAThreshold
    summary = timed_summary([(1000, 100)], 'csv')
    summary.avg_response_time = actual
    result = SLAEngine().evaluate(summary, SLAConfig([
        SLAThreshold('avg_response_time', operator, limit, severity=severity)]))
    assert result.verdict.value == 'fail'
    assert result.failed_checks == 1 and result.passed_checks == 0
    assert result.violations


@pytest.mark.parametrize("tag", ["sample", "httpSample", "sampleResult"])
def test_xml_sample_formats_preserve_failures(tag):
    from io import StringIO
    from app.services.jmeter.sla_engine import SLAEngine, SLAConfig

    xml = f'<testResults><{tag} t="200" ts="1000" s="false" lb="api" rc="500"/></testResults>'
    summary = ReportParser()._parse_xml_jtl(StringIO(xml))
    assert summary.total_requests == 1
    assert summary.error_rate == 100
    assert not SLAEngine().evaluate(summary, SLAConfig.standard_api_sla()).passed


def test_empty_report_cannot_pass_sla():
    from app.services.jmeter.sla_engine import SLAEngine, SLAConfig

    result = SLAEngine().evaluate(ReportParser()._empty_summary(), SLAConfig.standard_api_sla())
    assert not result.passed
    assert result.passed_checks == 0
    assert result.violations


def test_unknown_sla_metric_is_failure(sample_jtl):
    from app.services.jmeter.sla_engine import SLAEngine, SLAConfig, SLAThreshold

    summary = ReportParser().parse_jtl(sample_jtl)
    result = SLAEngine().evaluate(summary, SLAConfig([SLAThreshold('not_a_metric', 'lt', 1)]))
    assert not result.passed
    assert result.failed_checks == result.total_checks == 1
    assert 'not_a_metric' in result.violations[0]


def test_fact_prompt_serializes_datetime_without_losing_metrics():
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent

    fact = FactSheet(execution_id='unit-test', total_requests=17)
    prompt = AnalysisAgent._build_base_task(None, 'unit-test', fact, '')
    assert '"total_requests":17' in prompt.replace(' ', '')
    assert fact.computed_at.isoformat() in prompt


@pytest.fixture
def sample_jtl():
    return os.path.join(os.path.dirname(__file__), "fixtures", "sample.jtl")


@pytest_asyncio.fixture
async def temp_ledger():
    # Use the project directory for temp files to avoid Windows Unicode path issues
    # with aiosqlite (system temp may contain non-ASCII characters).
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=".") as f:
        path = f.name
    ledger = FactLedger(db_path=path)
    try:
        yield ledger
    finally:
        await ledger.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except OSError:
                pass


def test_compute_from_jtl_matches_report_parser(sample_jtl):
    """FactLedger must produce the same numbers as ReportParser."""
    parser = ReportParser()
    summary = parser.parse_jtl(sample_jtl)

    fact = FactLedger.compute_from_jtl(
        jtl_path=sample_jtl,
        execution_id="exec-001",
        module="user-api",
        version="v1.0",
    )

    assert fact.execution_id == "exec-001"
    assert fact.module == "user-api"
    assert fact.version == "v1.0"
    assert fact.total_requests == summary.total_requests == 10
    assert fact.avg_response_time == summary.avg_response_time
    assert fact.error_rate == summary.error_rate
    assert fact.throughput == summary.throughput
    assert fact.percentiles == summary.percentiles


@pytest.mark.asyncio
@pytest.mark.parametrize('strict', [False, True])
@pytest.mark.parametrize('legacy_hash', [False, True])
async def test_save_and_load_fact(temp_ledger, sample_jtl, strict, legacy_hash):
    """Fact sheet round-trip through SQLite."""
    fact = temp_ledger.compute_from_jtl(
        jtl_path=sample_jtl,
        execution_id="exec-002",
        module="order-api",
        version="v2.1",
    )
    saved = await temp_ledger.save(fact)
    assert saved is True
    if legacy_hash:
        db = await temp_ledger._get_db()
        await db.execute('UPDATE fact_sheets SET jtl_hash=substr(jtl_hash, 1, 16)')
        await db.commit()
    options = {'strict': True} if strict else {}
    loaded = await temp_ledger.load("exec-002", **options)
    assert loaded is not None
    assert loaded.execution_id == "exec-002"
    assert loaded.total_requests == fact.total_requests
    assert loaded.percentiles["p99"] == fact.percentiles["p99"]
    assert await temp_ledger.load('missing', **options) is None


@pytest.mark.asyncio
async def test_list_facts_by_module_version(temp_ledger, sample_jtl):
    """Query facts by module and version."""
    for exec_id, version in [("exec-a", "v1"), ("exec-b", "v2"), ("exec-c", "v1")]:
        fact = temp_ledger.compute_from_jtl(
            jtl_path=sample_jtl,
            execution_id=exec_id,
            module="payment",
            version=version,
        )
        await temp_ledger.save(fact)

    v1_facts = await temp_ledger.get_facts_for_module_version("payment", "v1")
    assert len(v1_facts) == 2

    all_facts = await temp_ledger.get_facts_for_module_version("payment")
    assert len(all_facts) == 3


@pytest.mark.asyncio
async def test_rebuild_fact_from_jtl(temp_ledger, sample_jtl):
    """Event-sourcing recovery: rebuild fact from immutable JTL."""
    fact = temp_ledger.compute_from_jtl(
        jtl_path=sample_jtl,
        execution_id="exec-rebuild",
        module="api",
        version="v1",
    )
    await temp_ledger.save(fact)

    # Corrupt the stored fact (simulate projection error)
    corrupted = fact.model_copy()
    corrupted.total_requests = 99999
    await temp_ledger.save(corrupted)

    rebuilt = await temp_ledger.rebuild_fact("exec-rebuild")
    assert rebuilt is not None
    assert rebuilt.total_requests == 10
    assert rebuilt.percentiles["p99"] == fact.percentiles["p99"]
