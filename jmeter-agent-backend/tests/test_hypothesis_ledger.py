"""Tests for HypothesisLedger — falsifiable interpretation ledger."""

import os
import pytest
import pytest_asyncio

from app.services.ai.memory.hypothesis_ledger import HypothesisLedger
from app.services.ai.memory.fact_ledger import FactLedger
from app.models.schemas import FactSheet, Hypothesis, HypothesisCategory, HypothesisStatus


def _fact(key, p99, **changes):
    """A run of project p in environment env with 5 threads: comparable with the others unless changed."""
    values = dict(execution_id=key, module='api', version='v1', project_id='p', environment_fingerprint='env',
                  load={'threads': 5, 'think_time_ms': 1000}, total_requests=10, percentiles={'p99': p99})
    return FactSheet(**values | changes)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['initialize', 'connection', 'commit'])
@pytest.mark.parametrize('generated_id', [False, True])
async def test_hypothesis_add_returns_persisted_input_snapshot(temp_hypothesis_ledger, monkeypatch, phase, generated_id):
    ledger = temp_hypothesis_ledger
    hyp = Hypothesis(hypothesis_id='' if generated_id else 'original', source_execution_id='source',
                     module='api', statement='prediction', supporting_facts=['p99'])
    await ledger._ensure_table()
    db = await ledger._get_db()
    original_id = None
    target, name = {'initialize': (ledger, '_ensure_table'), 'connection': (ledger, '_get_db'),
                    'commit': (db, 'commit')}[phase]
    original = getattr(target, name)
    calls = 0

    async def suspend_and_mutate(*args, **kwargs):
        nonlocal original_id, calls
        value = await original(*args, **kwargs)
        calls += 1
        if phase in ('connection', 'commit') and calls != 2:
            return value
        original_id = original_id or hyp.hypothesis_id
        hyp.hypothesis_id = 'other'
        hyp.source_execution_id = 'other-source'
        hyp.module = 'other-api'
        hyp.statement = 'changed'
        hyp.supporting_facts.append('throughput')
        return value

    with monkeypatch.context() as patch:
        patch.setattr(target, name, suspend_and_mutate)
        saved_id = await ledger.add(hyp)
    assert saved_id and saved_id != 'other'
    assert saved_id == original_id
    loaded = await ledger.get(saved_id)
    assert loaded.source_execution_id == 'source' and loaded.module == 'api'
    assert loaded.statement == 'prediction' and loaded.supporting_facts == ['p99']
    assert await ledger.get('other') is None


@pytest.mark.asyncio
async def test_hypothesis_add_if_absent_is_atomic_and_preserves_verification(temp_hypothesis_ledger):
    import asyncio
    import sqlite3
    ledger = temp_hypothesis_ledger
    other = HypothesisLedger(ledger.db_path)
    hyp = Hypothesis(hypothesis_id='same', source_execution_id='source', module='api', statement='prediction')
    try:
        await ledger._ensure_table()
        await other._ensure_table()
        assert await asyncio.gather(ledger.add(hyp, if_absent=True), other.add(hyp, if_absent=True)) == ['same', 'same']
        assert len(await ledger.list_for_module('api')) == 1
        assert await ledger.update_status('same', HypothesisStatus.CONFIRMED, verified_by='proof', notes='metric only')
        assert await other.add(hyp, if_absent=True) == 'same'
        loaded = await ledger.get('same')
        assert loaded.status == HypothesisStatus.CONFIRMED
        assert loaded.verified_by_execution_id == 'proof' and loaded.verification_notes == 'metric only'
        with pytest.raises(sqlite3.IntegrityError):
            await ledger.add(hyp)  # Normal inserts still reject duplicate IDs.
        assert (await ledger.get('same')).status == HypothesisStatus.CONFIRMED
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_hypothesis_add_if_absent_does_not_swallow_other_constraints(temp_hypothesis_ledger):
    import sqlite3
    hyp = Hypothesis(hypothesis_id='invalid', source_execution_id='source', module='api', statement='prediction')
    hyp.module = None
    with pytest.raises(sqlite3.IntegrityError):
        await temp_hypothesis_ledger.add(hyp, if_absent=True)
    assert await temp_hypothesis_ledger.get('invalid') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('rollback_fails', [False, True])
async def test_failed_hypothesis_add_is_not_inserted_by_later_read(temp_hypothesis_ledger, monkeypatch, cancelled, rollback_fails):
    import asyncio
    from unittest.mock import AsyncMock
    ledger = temp_hypothesis_ledger
    await ledger._ensure_table()
    db = await ledger._get_db()
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
    hyp = Hypothesis(hypothesis_id='new', source_execution_id='source', module='api', statement='prediction')
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await ledger.add(hyp)
    assert await ledger.get('new') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_count', [1, 70])
@pytest.mark.parametrize('strict', [False, True])
async def test_hypothesis_history_skips_bad_rows_and_fills_filtered_limit(temp_hypothesis_ledger, invalid_count, strict):
    ledger = temp_hypothesis_ledger
    for i in range(invalid_count + 1):
        await ledger.add(Hypothesis(hypothesis_id=f'h-{i:03}', source_execution_id='allowed',
                                    module='api', version='v1', statement='prediction'))
    await ledger.add(Hypothesis(hypothesis_id='other', source_execution_id='other-source', module='api', statement='unrelated'))
    db = await ledger._get_db()
    await db.execute("UPDATE hypotheses SET supporting_facts = 'broken-json', updated_at = '2099-01-01' WHERE hypothesis_id != 'h-000'")
    await db.commit()
    result = await ledger.list_for_module('api', version='v1', limit=1, status=HypothesisStatus.UNTESTED,
                                         source_execution_ids=['allowed'], **({'strict': True} if strict else {}))
    assert [hyp.hypothesis_id for hyp in result] == ['h-000']
    assert await ledger.get('h-001', **({'strict': True} if strict else {})) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [-1, 1.5, True])
@pytest.mark.parametrize('strict', [False, True])
async def test_hypothesis_history_invalid_limit_is_rejected_before_database(temp_hypothesis_ledger, monkeypatch, limit, strict):
    from unittest.mock import AsyncMock
    ensure = AsyncMock(side_effect=AssertionError('should not open database'))
    monkeypatch.setattr(temp_hypothesis_ledger, '_ensure_table', ensure)
    with pytest.raises(ValueError, match='limit'):
        await temp_hypothesis_ledger.list_for_module('api', limit=limit, **({'strict': True} if strict else {}))
    ensure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [{'limit': 0}, {'source_execution_ids': []}])
@pytest.mark.parametrize('strict', [False, True])
async def test_empty_hypothesis_query_does_not_open_database(temp_hypothesis_ledger, monkeypatch, kwargs, strict):
    from unittest.mock import AsyncMock
    ensure = AsyncMock(side_effect=AssertionError('should not open database'))
    monkeypatch.setattr(temp_hypothesis_ledger, '_ensure_table', ensure)
    assert await temp_hypothesis_ledger.list_for_module('api', **kwargs, **({'strict': True} if strict else {})) == []
    ensure.assert_not_awaited()


@pytest.fixture
def sample_jtl():
    return os.path.join(os.path.dirname(__file__), "fixtures", "sample.jtl")


@pytest_asyncio.fixture
async def temp_hypothesis_ledger(tmp_path):
    ledger = HypothesisLedger(db_path=str(tmp_path / 'hypotheses.db'))
    try:
        yield ledger
    finally:
        await ledger.close()


@pytest_asyncio.fixture
async def temp_fact_ledger(tmp_path):
    ledger = FactLedger(db_path=str(tmp_path / 'facts.db'))
    try:
        yield ledger
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_status_transitions_do_not_keep_contradictory_verification(temp_hypothesis_ledger):
    ledger = temp_hypothesis_ledger
    hid = await ledger.add(Hypothesis(hypothesis_id='', source_execution_id='source',
                                      module='api', statement='prediction'))
    assert await ledger.update_status(hid, HypothesisStatus.CONFIRMED, verified_by='pass', notes='first evidence')
    assert await ledger.update_status(hid, HypothesisStatus.REFUTED, refuted_by='fail', notes='second evidence')
    hyp = await ledger.get(hid)
    assert hyp.verified_by_execution_id is None and hyp.refuted_by_execution_id == 'fail'
    assert await ledger.update_status(hid, HypothesisStatus.CONFIRMED, verified_by='pass-again')
    hyp = await ledger.get(hid)
    assert hyp.refuted_by_execution_id is None and hyp.verified_by_execution_id == 'pass-again'
    assert hyp.verification_notes is None
    assert await ledger.update_status(hid, HypothesisStatus.UNTESTED)
    hyp = await ledger.get(hid)
    assert hyp.verified_by_execution_id is None and hyp.refuted_by_execution_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [HypothesisStatus.CONFIRMED, HypothesisStatus.REFUTED, HypothesisStatus.DEPRECATED])
async def test_automatic_evaluation_skips_resolved_or_deprecated_hypotheses(temp_hypothesis_ledger, monkeypatch, status):
    import importlib
    from unittest.mock import AsyncMock
    module = importlib.import_module('app.services.ai.memory.hypothesis_ledger')
    ledger = temp_hypothesis_ledger
    hid = await ledger.add(Hypothesis(hypothesis_id='', source_execution_id='old', module='api',
        category=HypothesisCategory.RECOMMENDATION, statement='prediction', testable_prediction='p99 decrease'))
    assert await ledger.update_status(hid, status, notes='retained decision')
    before = await ledger.get(hid)
    load = AsyncMock(side_effect=AssertionError('resolved hypothesis must not read facts'))
    monkeypatch.setattr(module.fact_ledger, 'load', load)
    result = await ledger.evaluate_against_facts(hid, 'new')
    assert result.model_dump() == before.model_dump()
    load.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['fact_read', 'write_wait'])
@pytest.mark.parametrize('new_status', list(HypothesisStatus))
async def test_automatic_evaluation_preserves_a_newer_decision(temp_hypothesis_ledger, monkeypatch, phase, new_status):
    import importlib
    from unittest.mock import AsyncMock
    module = importlib.import_module('app.services.ai.memory.hypothesis_ledger')
    ledger = temp_hypothesis_ledger
    other = HypothesisLedger(ledger.db_path)
    hid = await ledger.add(Hypothesis(hypothesis_id='', source_execution_id='old', module='api', version='v1',
        category=HypothesisCategory.RECOMMENDATION, statement='prediction', testable_prediction='p99 decrease'))
    db = await ledger._get_db()
    await db.execute("UPDATE hypotheses SET updated_at = '2000-01-01T00:00:00' WHERE hypothesis_id = ?", (hid,))
    await db.commit()
    facts = {key: _fact(key, value) for key, value in [('old', 100), ('new', 50)]}
    newer = None

    async def change_decision():
        nonlocal newer
        assert await other.update_status(hid, new_status,
            verified_by='manual-proof' if new_status == HypothesisStatus.CONFIRMED else None,
            refuted_by='manual-proof' if new_status == HypothesisStatus.REFUTED else None,
            notes='newer decision')
        newer = await other.get(hid)

    async def load(execution_id):
        if phase == 'fact_read' and execution_id == 'new':
            await change_decision()
        return facts[execution_id]

    original_ensure, calls = ledger._ensure_table, 0
    async def ensure():
        nonlocal calls
        await original_ensure()
        calls += 1
        if phase == 'write_wait' and calls == 2:
            await change_decision()

    monkeypatch.setattr(module.fact_ledger, 'load', AsyncMock(side_effect=load))
    monkeypatch.setattr(ledger, '_ensure_table', ensure)
    try:
        result = await ledger.evaluate_against_facts(hid, 'new')
        assert newer is not None
        assert result.model_dump() == newer.model_dump()
        assert (await ledger.get(hid)).model_dump() == newer.model_dump()
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_update_missing_hypothesis_does_not_report_success(temp_hypothesis_ledger):
    assert not await temp_hypothesis_ledger.update_status('missing', HypothesisStatus.CONFIRMED, verified_by='run')


@pytest.mark.asyncio
async def test_concurrent_evaluations_do_not_overwrite_the_first_decision(temp_hypothesis_ledger, monkeypatch):
    import asyncio
    import importlib
    from unittest.mock import AsyncMock
    module = importlib.import_module('app.services.ai.memory.hypothesis_ledger')
    ledger = temp_hypothesis_ledger
    hid = await ledger.add(Hypothesis(hypothesis_id='', source_execution_id='old', module='api', version='v1',
        category=HypothesisCategory.RECOMMENDATION, statement='prediction', testable_prediction='p99 decrease'))
    facts = {key: _fact(key, value)
             for key, value in [('old', 100), ('improved', 50), ('regressed', 150)]}
    monkeypatch.setattr(module.fact_ledger, 'load', AsyncMock(side_effect=lambda key: facts[key]))
    original_get, original_update = ledger.get, ledger.update_status
    both_read, first_written = asyncio.Event(), asyncio.Event()
    reads = 0

    async def get(key):
        nonlocal reads
        value = await original_get(key)
        reads += 1
        if reads <= 2:
            if reads == 2:
                both_read.set()
            await both_read.wait()
        return value

    async def update(*args, **kwargs):
        if kwargs.get('refuted_by') == 'regressed':
            await first_written.wait()
        result = await original_update(*args, **kwargs)
        if kwargs.get('verified_by') == 'improved':
            first_written.set()
        return result

    monkeypatch.setattr(ledger, 'get', get)
    monkeypatch.setattr(ledger, 'update_status', update)
    results = await asyncio.wait_for(asyncio.gather(
        ledger.evaluate_against_facts(hid, 'improved'),
        ledger.evaluate_against_facts(hid, 'regressed')), timeout=10)
    final = await original_get(hid)
    assert final.status == HypothesisStatus.CONFIRMED and final.verified_by_execution_id == 'improved'
    assert final.refuted_by_execution_id is None
    assert all(result.model_dump() == final.model_dump() for result in results)


@pytest.mark.asyncio
async def test_same_status_note_update_preserves_existing_evidence(temp_hypothesis_ledger):
    ledger = temp_hypothesis_ledger
    hid = await ledger.add(Hypothesis(hypothesis_id='', source_execution_id='source',
                                      module='api', statement='prediction'))
    await ledger.update_status(hid, HypothesisStatus.CONFIRMED, verified_by='pass', notes='old note')
    await ledger.update_status(hid, HypothesisStatus.CONFIRMED, notes='amended note')
    hyp = await ledger.get(hid)
    assert hyp.verified_by_execution_id == 'pass' and hyp.verification_notes == 'amended note'
    await ledger.update_status(hid, HypothesisStatus.DEPRECATED)
    hyp = await ledger.get(hid)
    assert hyp.verified_by_execution_id == 'pass' and hyp.verification_notes == 'amended note'


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('rollback_fails', [False, True])
async def test_failed_status_commit_is_not_committed_by_a_later_read(temp_hypothesis_ledger, monkeypatch, cancelled, rollback_fails):
    import asyncio
    from unittest.mock import AsyncMock
    ledger = temp_hypothesis_ledger
    hid = await ledger.add(Hypothesis(hypothesis_id='', source_execution_id='source',
                                      module='api', statement='prediction'))
    db = await ledger._get_db()
    original_commit = db.commit
    commits = 0
    async def fail_status_commit():
        nonlocal commits
        commits += 1
        if commits == 2:  # First commit belongs to _ensure_table, second to UPDATE.
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError('simulated commit failure')
        await original_commit()
    monkeypatch.setattr(db, 'commit', fail_status_commit)
    if rollback_fails:
        monkeypatch.setattr(db, 'rollback', AsyncMock(side_effect=RuntimeError('rollback unavailable')))
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await ledger.update_status(hid, HypothesisStatus.CONFIRMED, verified_by='run')
    else:
        assert not await ledger.update_status(hid, HypothesisStatus.CONFIRMED, verified_by='run')
    hyp = await ledger.get(hid)  # _ensure_table commits again; failed UPDATE must be gone.
    assert hyp.status == HypothesisStatus.UNTESTED and hyp.verified_by_execution_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize('new_p99', [50, 150])
async def test_evaluation_write_failure_is_not_silently_reported_as_processed(temp_hypothesis_ledger, monkeypatch, new_p99):
    import importlib
    from unittest.mock import AsyncMock
    module = importlib.import_module('app.services.ai.memory.hypothesis_ledger')
    facts = {key: _fact(key, value)
             for key, value in [('old', 100), ('new', new_p99)]}
    monkeypatch.setattr(module.fact_ledger, 'load', AsyncMock(side_effect=lambda key: facts[key]))
    monkeypatch.setattr(temp_hypothesis_ledger, 'update_status', AsyncMock(return_value=False))
    hid = await temp_hypothesis_ledger.add(Hypothesis(hypothesis_id='', source_execution_id='old',
        module='api', version='v1', category=HypothesisCategory.RECOMMENDATION,
        statement='prediction', testable_prediction='p99 decrease'))
    with pytest.raises(RuntimeError, match='persist hypothesis evaluation'):
        await temp_hypothesis_ledger.evaluate_against_facts(hid, 'new')


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['no_old_samples', 'no_new_samples', 'negative_error', 'error_above_100',
                                  'other_project', 'other_environment', 'other_load', 'overflow_change',
                                  'overflow_threshold'])
async def test_unusable_facts_do_not_confirm_or_refute_predictions(temp_hypothesis_ledger, monkeypatch, case):
    import importlib
    from unittest.mock import AsyncMock
    module = importlib.import_module('app.services.ai.memory.hypothesis_ledger')
    facts = {key: _fact(key, 100 if key == 'old' else 50, error_rate=0) for key in ('old', 'new')}
    prediction = 'error_rate below 1%'
    test_type = 'baseline'
    if case == 'no_old_samples': facts['old'].total_requests = 0
    elif case == 'no_new_samples': facts['new'].total_requests = 0
    elif case == 'negative_error': facts['new'].error_rate = -1
    elif case == 'error_above_100': facts['new'].error_rate = 101
    elif case == 'other_project': facts['old'].project_id = 'other'
    elif case == 'other_environment': facts['new'].environment_fingerprint = 'elsewhere'
    elif case == 'other_load': facts['new'].load = {'threads': 50, 'think_time_ms': 1000}
    elif case == 'overflow_change':
        prediction = 'p99 increase'
        facts['old'].percentiles['p99'] = 1e-308
        facts['new'].percentiles['p99'] = 1e308
    elif case == 'overflow_threshold': prediction = 'p99 decrease by >' + '9' * 400 + '%'
    monkeypatch.setattr(module.fact_ledger, 'load', AsyncMock(side_effect=lambda key: facts[key]))
    hid = await temp_hypothesis_ledger.add(Hypothesis(hypothesis_id='', source_execution_id='old',
        module='api', version='v1', test_type=test_type, statement='prediction',
        category=HypothesisCategory.RECOMMENDATION, testable_prediction=prediction))
    hyp = await temp_hypothesis_ledger.evaluate_against_facts(hid, 'new')
    assert hyp.status == HypothesisStatus.UNTESTED
    assert hyp.verified_by_execution_id is None and hyp.refuted_by_execution_id is None


@pytest.mark.parametrize('status', [HypothesisStatus.CONFIRMED, HypothesisStatus.REFUTED])
def test_prompt_preserves_prediction_verification_scope_and_execution(status):
    hyp = Hypothesis(hypothesis_id='h', source_execution_id='source', module='api',
        category=HypothesisCategory.RECOMMENDATION, statement='pool changes improve latency', status=status,
        testable_prediction='p99 decrease', verified_by_execution_id='verified-run' if status == HypothesisStatus.CONFIRMED else None,
        refuted_by_execution_id='refuted-run' if status == HypothesisStatus.REFUTED else None,
        verification_notes='Only the metric prediction was checked; no intervention was verified.')
    prompt = HypothesisLedger.format_for_prompt([hyp])
    assert hyp.verification_notes in prompt and hyp.testable_prediction in prompt
    assert ('verified-run' if status == HypothesisStatus.CONFIRMED else 'refuted-run') in prompt
    assert '已被实验确认的结论' not in prompt


@pytest.mark.asyncio
async def test_add_and_get_hypothesis(temp_hypothesis_ledger):
    hyp = Hypothesis(
        hypothesis_id="",
        source_execution_id="exec-001",
        module="api",
        version="v1",
        category=HypothesisCategory.BOTTLENECK,
        statement="瓶颈在数据库连接池",
        confidence=0.6,
        status=HypothesisStatus.UNTESTED,
        supporting_facts=["avg_response_time", "error_rate"],
        testable_prediction="把连接池翻倍后 p99 应下降 >20%",
        falsification_design="调整连接池大小后重测同一接口",
    )

    hid = await temp_hypothesis_ledger.add(hyp)
    assert hid

    loaded = await temp_hypothesis_ledger.get(hid)
    assert loaded is not None
    assert loaded.statement == "瓶颈在数据库连接池"
    assert loaded.status == HypothesisStatus.UNTESTED
    assert loaded.supporting_facts == ["avg_response_time", "error_rate"]


@pytest.mark.asyncio
async def test_hypothesis_cannot_verify_itself(temp_hypothesis_ledger, monkeypatch):
    from unittest.mock import AsyncMock
    import importlib
    module = importlib.import_module('app.services.ai.memory.hypothesis_ledger')
    fact = type('Fact', (), dict(module='api', version='v1', error_rate=0))()
    monkeypatch.setattr(module.fact_ledger, 'load', AsyncMock(return_value=fact))
    hyp = Hypothesis(hypothesis_id='', source_execution_id='same-run', module='api', version='v1',
                     statement='error rate prediction', testable_prediction='error_rate below 1%')
    hid = await temp_hypothesis_ledger.add(hyp)
    assert (await temp_hypothesis_ledger.evaluate_against_facts(hid, 'same-run')).status == HypothesisStatus.UNTESTED


@pytest.mark.asyncio
@pytest.mark.parametrize('prediction,old,new,threads', [
    ('p99 should decrease by >20%', 100, 99, 5),
    ('p99 should decrease', 100, 50, 8),
])
async def test_hypothesis_respects_threshold_and_context(temp_hypothesis_ledger, monkeypatch, prediction, old, new, threads):
    from unittest.mock import AsyncMock
    import importlib
    module = importlib.import_module('app.services.ai.memory.hypothesis_ledger')
    facts = {'old': _fact('old', old), 'new': _fact('new', new, load={'threads': threads, 'think_time_ms': 1000})}
    monkeypatch.setattr(module.fact_ledger, 'load', AsyncMock(side_effect=lambda key: facts[key]))
    hyp = Hypothesis(hypothesis_id='', source_execution_id='old', module='api', version='v1',
                     statement='testable prediction', category=HypothesisCategory.RECOMMENDATION,
                     testable_prediction=prediction)
    hid = await temp_hypothesis_ledger.add(hyp)
    result = await temp_hypothesis_ledger.evaluate_against_facts(hid, 'new')
    # Another load is not the run a load-free prediction was about.
    assert result.status == (HypothesisStatus.REFUTED if threads == 5 else HypothesisStatus.UNTESTED)


@pytest.mark.asyncio
async def test_hypothesis_status_transition(temp_hypothesis_ledger):
    hyp = Hypothesis(
        hypothesis_id="",
        source_execution_id="exec-001",
        module="api",
        version="v1",
        category=HypothesisCategory.ROOT_CAUSE,
        statement="根因是锁竞争",
    )
    hid = await temp_hypothesis_ledger.add(hyp)

    await temp_hypothesis_ledger.update_status(
        hid, HypothesisStatus.CONFIRMED, verified_by="exec-002", notes="retest passed"
    )
    loaded = await temp_hypothesis_ledger.get(hid)
    assert loaded.status == HypothesisStatus.CONFIRMED
    assert loaded.verified_by_execution_id == "exec-002"


@pytest.mark.asyncio
async def test_evaluate_hypothesis_against_new_fact(
    temp_hypothesis_ledger, temp_fact_ledger, sample_jtl
):
    # Use the module-level fact_ledger singleton for hypothesis evaluation
    from app.services.ai.memory import hypothesis_ledger as hl
    from app.services.ai.memory import fact_ledger as fl

    # Save original paths and restore after test
    original_hl_db = hl.db_path
    original_fl_db = fl.db_path
    hl.db_path = temp_hypothesis_ledger.db_path
    fl.db_path = temp_fact_ledger.db_path

    try:
        # First execution: p99 ~250ms
        fact1 = fl.compute_from_jtl(
            sample_jtl, execution_id="exec-old", module="api", version="v1"
        )
        fact1.project_id, fact1.environment_fingerprint, fact1.load = 'p', 'env', {'threads': 5}
        await fl.save(fact1)

        hyp = Hypothesis(
            hypothesis_id="",
            source_execution_id="exec-old",
            module="api",
            version="v1",
            category=HypothesisCategory.RECOMMENDATION,
            statement="增加连接池后 p99 会下降",
            testable_prediction="p99 should decrease",
        )
        hid = await hl.add(hyp)

        # Second execution with lower p99 (simulate improvement)
        # We reuse the same JTL for the test, but manually tweak the fact
        fact2 = fact1.model_copy()
        fact2.execution_id = "exec-new"
        fact2.percentiles = {**fact1.percentiles, "p99": fact1.percentiles["p99"] * 0.5}
        await fl.save(fact2)

        updated = await hl.evaluate_against_facts(hid, "exec-new")
        assert updated.status == HypothesisStatus.CONFIRMED
        assert updated.verified_by_execution_id == "exec-new"
    finally:
        await fl.close()
        await hl.close()
        hl.db_path = original_hl_db
        fl.db_path = original_fl_db


@pytest.mark.asyncio
async def test_prompt_formatting(temp_hypothesis_ledger):
    for status, stmt in [
        (HypothesisStatus.CONFIRMED, "已确认的结论"),
        (HypothesisStatus.UNTESTED, "未验证假设"),
        (HypothesisStatus.REFUTED, "已被证伪"),
    ]:
        hyp = Hypothesis(
            hypothesis_id="",
            source_execution_id="exec-001",
            module="api",
            version="v1",
            category=HypothesisCategory.BOTTLENECK,
            statement=stmt,
            status=status,
        )
        await temp_hypothesis_ledger.add(hyp)

    hyps = await temp_hypothesis_ledger.list_for_module("api")
    prompt = HypothesisLedger.format_for_prompt(hyps)

    assert "已记录的确认结果" in prompt and "不代表因果已证实" in prompt
    assert "未验证假设" in prompt
    assert "已记录的证伪结果" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('threads, p95, status', [
    (12, 300, HypothesisStatus.CONFIRMED),   # the stated load, and the request got 50% slower
    (12, 210, HypothesisStatus.REFUTED),     # the stated load, but it hardly moved
    (5, 300, HypothesisStatus.UNTESTED),     # not the load the prediction was about
])
async def test_a_structured_prediction_is_checked_on_the_run_it_names(temp_hypothesis_ledger, monkeypatch,
                                                                       threads, p95, status):
    import importlib
    from unittest.mock import AsyncMock
    module = importlib.import_module('app.services.ai.memory.hypothesis_ledger')
    row = lambda value: {'GET /orders': {'count': 20, 'p95_response_time': value}}
    facts = {'old': _fact('old', 100, per_label=row(200)),
             'new': _fact('new', 100, per_label=row(p95), module='another-plan',
                          load={'threads': threads, 'think_time_ms': 1000})}
    monkeypatch.setattr(module.fact_ledger, 'load', AsyncMock(side_effect=lambda key: facts[key]))
    await temp_hypothesis_ledger.add(Hypothesis(
        hypothesis_id='', source_execution_id='old', module='api', project_id='p', statement='orders queue up',
        category=HypothesisCategory.BOTTLENECK,
        prediction={'metric': 'p95', 'label': 'GET /orders', 'change': 'increase', 'value': 30, 'threads': 12}))
    result = await temp_hypothesis_ledger.evaluate_project_hypotheses('p', 'new')
    assert result[0].status == status
    if status != HypothesisStatus.UNTESTED:
        assert 'GET /orders' in result[0].verification_notes and '只检查了预测' in result[0].verification_notes


@pytest.mark.asyncio
async def test_project_listing_assignment_and_expiry(temp_hypothesis_ledger):
    ledger = temp_hypothesis_ledger
    for hid, project in (('mine', 'p'), ('legacy', ''), ('theirs', 'q')):
        await ledger.add(Hypothesis(hypothesis_id=hid, source_execution_id=hid, module='api', project_id=project,
                                    statement=f'{hid} statement'))
    assert await ledger.assign_project('legacy', 'p') == 1
    assert await ledger.assign_project('mine', 'q') == 0  # A known project is never overwritten.
    assert {h.hypothesis_id for h in await ledger.list_for_project('p')} == {'mine', 'legacy'}
    db = await ledger._get_db()
    await db.execute("UPDATE hypotheses SET created_at = '2000-01-01T00:00:00' WHERE hypothesis_id = 'mine'")
    await db.commit()
    assert await ledger.expire('p', 30) == 1
    expired = await ledger.get('mine')
    assert expired.status == HypothesisStatus.DEPRECATED and '30 天' in expired.verification_notes
    assert (await ledger.get('legacy')).status == HypothesisStatus.UNTESTED


@pytest.mark.asyncio
async def test_a_table_from_before_projects_gains_the_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY, source_execution_id TEXT NOT NULL,
            module TEXT NOT NULL, version TEXT, test_type TEXT, category TEXT NOT NULL, statement TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5, status TEXT NOT NULL DEFAULT 'untested',
            supporting_facts TEXT NOT NULL DEFAULT '[]', testable_prediction TEXT, falsification_design TEXT,
            verified_by_execution_id TEXT, refuted_by_execution_id TEXT, verification_notes TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("INSERT INTO hypotheses (hypothesis_id, source_execution_id, module, category, statement, "
                   "created_at, updated_at) VALUES ('old', 'run', 'api', 'risk', 'kept', '2026-01-01', '2026-01-01')")
    ledger = HypothesisLedger(db_path=str(path))
    try:
        kept = await ledger.get('old')
        assert kept.statement == 'kept' and kept.project_id == '' and kept.prediction is None
        await ledger.add(Hypothesis(hypothesis_id='new', source_execution_id='r2', module='api', project_id='p',
                                    statement='new one', prediction={'metric': 'p95', 'change': 'below', 'value': 300}))
        assert (await ledger.get('new')).prediction == {'metric': 'p95', 'change': 'below', 'value': 300}
    finally:
        await ledger.close()
