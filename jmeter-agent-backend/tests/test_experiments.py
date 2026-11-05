"""Verification runs, capacity searches and the CI gate, with the lifecycle replaced by fakes."""
import asyncio
from types import SimpleNamespace

import pytest

from app.models.schemas import (DetailedTestSummary, Hypothesis, HypothesisStatus, TestExecutionStatus as Status)
from app.services import experiments
from app.services.experiments import CapacityStep, capacity_steps, judge_step


def test_capacity_steps_end_at_the_maximum():
    assert capacity_steps(10, 50, 1.5) == [10, 15, 23, 35, 50]
    assert capacity_steps(1, 3, 1.1) == [1, 2, 3]
    with pytest.raises(ValueError):
        capacity_steps(1, 100_000, 1.1)  # More than MAX_STEPS.
    with pytest.raises(ValueError):
        capacity_steps(20, 10, 1.5)


def step(threads, throughput, error_rate=0.0, requirements='pass'):
    return CapacityStep(threads=threads, throughput=throughput, error_rate=error_rate, requirements=requirements)


def test_a_step_stops_the_search_on_requirements_errors_or_a_plateau():
    assert judge_step(step(10, 100), None, 1.0) is None
    assert judge_step(step(15, 140), step(10, 100), 1.0) is None
    assert '需求' in judge_step(step(15, 140, requirements='fail'), step(10, 100), 1.0)
    assert '错误率' in judge_step(step(15, 140, error_rate=3), step(10, 100), 1.0)
    assert '吞吐量' in judge_step(step(20, 105), step(10, 100), 1.0)


class FakeLifecycle:
    """Runs finish at once with the throughput a function of their threads."""

    def __init__(self, throughput_of, error_of=lambda threads: 0.0):
        self.throughput_of, self.error_of, self.runs, self.cancelled = throughput_of, error_of, {}, []

    async def prepare(self, jmx_path, **kwargs):
        execution_id = f"run-{len(self.runs)}"
        threads = kwargs['threads']
        summary = DetailedTestSummary(total_requests=1000, avg_response_time=100, min_response_time=1,
                                      max_response_time=500, error_rate=self.error_of(threads), received_bytes_per_sec=0,
                                      sent_bytes_per_sec=0,
                                      throughput=self.throughput_of(threads), percentiles={'p95': 200})
        run = SimpleNamespace(id=execution_id, status=Status.COMPLETED, summary=summary, error_message=None,
                              project_id='p', jmx_path=jmx_path, kwargs=kwargs)
        self.runs[execution_id] = run
        return run

    def submit_prepared(self, prepared):
        return None

    async def wait(self, execution_id):
        await asyncio.sleep(0)
        return self.runs[execution_id]

    async def cancel(self, execution_id):
        self.cancelled.append(execution_id)
        return True


@pytest.fixture
def plan(monkeypatch, tmp_path):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'BASE_DIR', str(tmp_path))
    saved = SimpleNamespace(id='plan-1', project_id='p', jmx_path=str(tmp_path / 'plan.jmx'))
    monkeypatch.setattr(experiments.store, 'get_test_plan', lambda plan_id: saved if plan_id == 'plan-1' else None)

    async def verdict(execution):
        return 'pass'
    monkeypatch.setattr(experiments, 'requirement_verdict', verdict)
    return saved


async def finish(search):
    await experiments._running[search.id]
    return experiments.load_search(search.id)


@pytest.mark.asyncio
async def test_a_capacity_search_stops_where_throughput_stops_growing(plan, monkeypatch):
    fake = FakeLifecycle(lambda threads: min(threads, 40) * 10.0)
    monkeypatch.setattr(experiments, 'execution_lifecycle', fake)
    search = await experiments.start_capacity_search('plan-1', start_threads=10, max_threads=100, factor=2)
    done = await finish(search)
    assert [s.threads for s in done.steps] == [10, 20, 40, 80, 100]
    assert done.status == 'finished' and done.capacity_threads == 40 and done.capacity_throughput == 400
    # The load after the stop never ran, and does not read as waiting to.
    assert [s.status for s in done.steps] == ['passed', 'passed', 'passed', 'stopped', 'skipped']
    assert '吞吐量' in done.stop_reason
    assert fake.runs['run-1'].kwargs['purpose'] == {'kind': 'capacity', 'search_id': search.id, 'step': 1}
    assert experiments.list_searches('plan-1')[0].id == search.id


@pytest.mark.asyncio
async def test_a_capacity_search_that_passes_every_step_says_so(plan, monkeypatch):
    monkeypatch.setattr(experiments, 'execution_lifecycle', FakeLifecycle(lambda threads: threads * 10.0))
    done = await finish(await experiments.start_capacity_search('plan-1', start_threads=10, max_threads=30, factor=2))
    assert done.capacity_threads == 30 and '仍然通过' in done.stop_reason


@pytest.fixture
def reports(monkeypatch):
    from app.core.config import settings
    from app.services import post_run
    monkeypatch.setattr(settings, 'AUTO_ANALYSIS_ENABLED', True)
    written = []

    async def start_reanalysis(execution_id, ai_model=None, lang=None):
        written.append(execution_id)
    monkeypatch.setattr(post_run, 'start_reanalysis', start_reanalysis)
    return written


@pytest.mark.asyncio
async def test_a_search_writes_one_report_on_the_step_that_stopped_it(plan, monkeypatch, reports):
    monkeypatch.setattr(experiments, 'execution_lifecycle', FakeLifecycle(lambda threads: min(threads, 40) * 10.0))
    done = await finish(await experiments.start_capacity_search('plan-1', start_threads=10, max_threads=100, factor=2))
    assert reports == ['run-3'] and done.report_execution_id == 'run-3'  # 80 threads, where throughput stopped.


@pytest.mark.asyncio
async def test_a_search_that_passes_everything_reports_on_its_last_step(plan, monkeypatch, reports):
    monkeypatch.setattr(experiments, 'execution_lifecycle', FakeLifecycle(lambda threads: threads * 10.0))
    done = await finish(await experiments.start_capacity_search('plan-1', start_threads=10, max_threads=30, factor=2))
    assert reports == ['run-2'] and done.report_execution_id == 'run-2'


@pytest.mark.asyncio
async def test_a_failed_run_ends_the_search(plan, monkeypatch):
    fake = FakeLifecycle(lambda threads: threads * 10.0)
    original = fake.prepare

    async def prepare(jmx_path, **kwargs):
        run = await original(jmx_path, **kwargs)
        if kwargs['threads'] > 10:
            run.status, run.error_message = Status.CANCELLED, '运行守护停止了这次运行'
        return run
    fake.prepare = prepare
    monkeypatch.setattr(experiments, 'execution_lifecycle', fake)
    done = await finish(await experiments.start_capacity_search('plan-1', start_threads=10, max_threads=40, factor=2))
    assert done.status == 'failed' and done.capacity_threads == 10 and done.steps[1].note == '运行守护停止了这次运行'


@pytest.mark.asyncio
async def test_only_one_search_runs_at_a_time(plan, monkeypatch):
    blocker = asyncio.get_running_loop().create_future()
    monkeypatch.setitem(experiments._running, 'other', blocker)
    with pytest.raises(RuntimeError):
        await experiments.start_capacity_search('plan-1', start_threads=1, max_threads=2, factor=2)
    blocker.cancel()


@pytest.mark.asyncio
async def test_a_verification_run_uses_the_predicted_load(plan, monkeypatch):
    from app.services.ai.memory import hypothesis_ledger
    hyp = Hypothesis(hypothesis_id='h1', source_execution_id='src', module='plan-1', statement='GET /items queues',
                     prediction={'metric': 'avg', 'label': 'GET /items', 'change': 'increase', 'value': 30,
                                 'threads': 40})

    async def get(hypothesis_id, **_):
        return hyp if hypothesis_id == 'h1' else None
    monkeypatch.setattr(hypothesis_ledger, 'get', get)
    submitted = {}

    async def submit(jmx_path, **kwargs):
        submitted.update(kwargs)
        return 'run'
    monkeypatch.setattr(experiments.execution_lifecycle, 'submit', submit)
    assert await experiments.start_verification('h1') == 'run'
    assert submitted['threads'] == 40 and submitted['purpose']['hypothesis_id'] == 'h1'

    hyp.prediction = {**hyp.prediction, 'after': '调大连接池'}
    with pytest.raises(ValueError):
        await experiments.start_verification('h1')
    hyp.prediction.pop('after')
    hyp.status = HypothesisStatus.CONFIRMED
    with pytest.raises(ValueError):
        await experiments.start_verification('h1')


def run(status=Status.COMPLETED, error_rate=0.0):
    summary = DetailedTestSummary(total_requests=10, avg_response_time=1, min_response_time=1, max_response_time=1,
                                  error_rate=error_rate, throughput=1, percentiles={'p95': 1},
                                  received_bytes_per_sec=0, sent_bytes_per_sec=0)
    return SimpleNamespace(id='run-1', status=status, summary=summary, error_message='boom', project_id='p',
                           jmx_path='x.jmx')


@pytest.mark.asyncio
@pytest.mark.parametrize('requirements,baseline,expected', [
    ('pass', 'stable', 'pass'), ('fail', 'stable', 'fail'), ('pass', 'regression', 'fail'),
    ('incomplete', None, 'pass'), ('none', None, 'pass')])
async def test_the_gate(monkeypatch, requirements, baseline, expected):
    from app.services import baseline_compare
    monkeypatch.setattr(experiments.store, 'get_execution', lambda execution_id: run())

    async def verdict(execution):
        return requirements

    async def history(execution, summary):
        return {'baseline': {'verdict': baseline, 'name': 'b', 'summary': 's'} if baseline else None}
    monkeypatch.setattr(experiments, 'requirement_verdict', verdict)
    monkeypatch.setattr(baseline_compare, 'evidence_for', history)
    result = await experiments.gate('run-1')
    assert result['gate'] == expected and result['requirements'] == requirements


@pytest.mark.asyncio
async def test_the_gate_waits_for_a_running_run_and_errors_on_a_failed_one(monkeypatch):
    monkeypatch.setattr(experiments.store, 'get_execution', lambda execution_id: run(Status.RUNNING))
    assert (await experiments.gate('run-1'))['gate'] == 'pending'
    monkeypatch.setattr(experiments.store, 'get_execution', lambda execution_id: run(Status.FAILED))
    result = await experiments.gate('run-1')
    assert result['gate'] == 'error' and result['reasons'] == ['boom']


def test_the_ci_script_exits_with_the_gate(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('ci_gate', Path(__file__).parent.parent / 'scripts' / 'ci_gate.py')
    ci_gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci_gate)
    answers = iter([{'id': 'run-1'}, {'gate': 'pending', 'status': 'running'},
                    {'gate': 'fail', 'reasons': ['Performance requirements failed']}])
    calls = []

    def call(base, key, method, path):
        calls.append((method, path))
        return next(answers)
    monkeypatch.setattr(ci_gate, 'call', call)
    monkeypatch.setattr(ci_gate.time, 'sleep', lambda seconds: None)
    assert ci_gate.main(['--plan', 'plan-1', '--token', 't']) == 1
    assert calls[0] == ('POST', '/test-plans/plan-1/execute') and calls[-1] == ('GET', '/executions/run-1/gate')
    assert 'FAIL: Performance requirements failed' in capsys.readouterr().err


def test_a_search_saved_before_unrun_steps_were_marked_reads_them_as_not_run(tmp_path, monkeypatch):
    """Records from before the fix showed loads a finished search never reached as "waiting"."""
    monkeypatch.setattr(experiments, '_searches_dir', lambda: str(tmp_path))
    old = experiments.CapacitySearch(id='old1', plan_id='plan-1', project_id='p', status='finished', max_error_rate=1,
                                     steps=[experiments.CapacityStep(threads=10, execution_id='r', status='stopped'),
                                            experiments.CapacityStep(threads=20)])
    (tmp_path / 'old1.json').write_text(old.model_dump_json(), encoding='utf-8')
    assert [s.status for s in experiments.load_search('old1').steps] == ['stopped', 'skipped']
