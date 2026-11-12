"""Runs started to answer a question: does a hypothesis hold, how much load does the system take,
and did this run pass its gate.

Code decides every step. A verification run reruns the hypothesis' plan at the load its prediction
names; the hypothesis ledger then judges it as it judges any later run. A capacity search raises the
load step by step and stops at the first step that fails a requirement, errors too much or stops
gaining throughput; it runs that step again before believing it, then tries the loads in between. The gate turns a run's evidence into pass, fail or error for a CI job.
"""
import asyncio
import logging
import math
import os
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.text import tr
from app.services.execution_lifecycle import TERMINAL, execution_lifecycle, validate_id
from app.services.storage import store

logger = logging.getLogger(__name__)


# ── Verification runs ─────────────────────────────────────

async def start_verification(hypothesis_id: str, *, approved_sha256=None, confirm_production=False):
    """Run the hypothesis' plan at the load its prediction is about; returns the new run."""
    from app.services.ai.memory import fact_ledger, hypothesis_ledger, prediction
    from app.models.schemas import HypothesisStatus
    hyp = await hypothesis_ledger.get(hypothesis_id)
    if hyp is None:
        raise LookupError('Hypothesis not found')
    if hyp.status != HypothesisStatus.UNTESTED:
        raise ValueError(tr('这条假设已经有结论，不需要再验证',
                            'This hypothesis is already settled; there is nothing left to verify'))
    expected = hyp.prediction or prediction.from_text(hyp.testable_prediction)
    if not prediction.checkable(expected):
        raise ValueError(tr(
            '这条假设没有程序能检查的预测，或者要先做一项改动再验证；请改完后手动运行，再在报告里判定',
            'This hypothesis has no prediction a program can check, or needs a change made first; make the '
            'change, run the plan yourself, then settle it on the report page'))
    plan = store.get_test_plan(hyp.module)
    if plan is None:
        raise LookupError('The plan this hypothesis came from no longer exists')
    threads = expected.get('threads')
    if threads is None:
        source = await fact_ledger.load(hyp.source_execution_id)
        threads = (source.load or {}).get('threads') if source else None
    purpose = {'kind': 'verification', 'hypothesis_id': hypothesis_id, 'prediction': prediction.describe(expected)}
    return await execution_lifecycle.submit(
        plan.jmx_path, test_plan_id=plan.id, approved_sha256=approved_sha256, confirm_production=confirm_production,
        threads=int(threads) if threads else None, purpose=purpose, wait_for_resources=True)


# ── Capacity search ───────────────────────────────────────

MAX_STEPS = 12
MAX_REFINE_STEPS = 4
REFINE_MIN_GAP = 0.1  # Of the last passing load: a closer pair than this is answer enough.
# A step that turned less than this share of its added users into throughput has saturated something.
PLATEAU_SHARE = 0.2


class CapacityStep(BaseModel):
    threads: int
    execution_id: Optional[str] = None
    # pending, running, passed, stopped, failed, skipped (never run: the search ended first),
    # retried (stopped once, then passed when run again: noise, not the limit)
    status: str = 'pending'
    phase: str = 'ramp'  # ramp: the planned steps. confirm: a stopping step run again. refine: between pass and stop.
    throughput: Optional[float] = None
    p95: Optional[float] = None
    error_rate: Optional[float] = None
    requirements: Optional[str] = None  # pass, fail, incomplete, none
    generator_saturated: bool = False  # The machine running JMeter was at its limit: the numbers are its own.
    note: str = ''


class CapacitySearch(BaseModel):
    id: str
    plan_id: str
    project_id: str
    status: str = 'running'  # running, finished, failed, cancelled
    max_error_rate: float
    confirm_stop: bool = True
    refine_steps: int = 2
    steps: list[CapacityStep]
    capacity_threads: Optional[int] = None  # The highest load that passed every check.
    capacity_throughput: Optional[float] = None
    stop_reason: str = ''
    report_execution_id: Optional[str] = None  # The one step whose AI report covers the whole search.
    created_at: datetime = Field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None


def capacity_steps(start: int, maximum: int, factor: float) -> list[int]:
    """Loads to try: from ``start`` multiplied by ``factor`` up to ``maximum``, which is always the last."""
    if start < 1 or maximum < start:
        raise ValueError(tr('起始线程数至少为 1，且不大于最大线程数',
                            'Start with at least 1 thread and no more than the maximum'))
    if not 1.1 <= factor <= 4:
        raise ValueError(tr('每步倍数需要在 1.1 到 4 之间', 'The step factor must be between 1.1 and 4'))
    steps, current = [start], start
    while current < maximum and len(steps) < MAX_STEPS:
        current = min(maximum, max(current + 1, math.ceil(current * factor)))
        steps.append(current)
    if steps[-1] != maximum:
        raise ValueError(tr(f'最多 {MAX_STEPS} 步；请调大倍数或调小最大线程数',
                            f'At most {MAX_STEPS} steps; use a larger factor or a lower maximum'))
    return steps


def saturated_reason() -> str:
    return tr('压测机饱和（运行期间 CPU 或内存长时间达到上限），这一步的结果不可信；请降低单机负载或改用分布式执行',
              'The load generator was saturated (CPU or memory at its limit for much of the run), so this step '
              'cannot be trusted; put less load on one machine or run distributed')


def judge_step(step: CapacityStep, previous: Optional[CapacityStep], max_error_rate: float) -> Optional[str]:
    """Why the search stops at this step, or None when the system took this load."""
    if step.generator_saturated:
        return saturated_reason()  # First: every other number of this step is the load generator's, not the target's.
    if step.requirements == 'fail':
        return tr('性能需求不达标', 'Performance requirements not met')
    if step.error_rate is not None and step.error_rate > max_error_rate:
        return tr(f'错误率 {step.error_rate:g}% 超过 {max_error_rate:g}%',
                  f'Error rate {step.error_rate:g}% is above {max_error_rate:g}%')
    if previous is not None and previous.throughput and step.throughput is not None:
        added = (step.threads - previous.threads) / previous.threads
        gained = (step.throughput - previous.throughput) / previous.throughput
        if added > 0 and gained < PLATEAU_SHARE * added:
            return tr(f'吞吐量不再随负载增长（线程 +{added * 100:.0f}%，吞吐量 {gained * 100:+.1f}%）',
                      f'Throughput no longer grows with load (threads +{added * 100:.0f}%, '
                      f'throughput {gained * 100:+.1f}%)')
    return None


def refine_load(passed: int, stopped: int) -> Optional[int]:
    """The load halfway between the last that passed and the first that did not; None once they are close."""
    if stopped - passed <= max(1, math.ceil(passed * REFINE_MIN_GAP)):
        return None
    return (passed + stopped) // 2


def _searches_dir() -> str:
    return os.path.join(settings.BASE_DIR, 'capacity_searches')


def _save(search: CapacitySearch) -> None:
    os.makedirs(_searches_dir(), exist_ok=True)
    path = os.path.join(_searches_dir(), f'{search.id}.json')
    with open(path + '.tmp', 'w', encoding='utf-8') as stream:
        stream.write(search.model_dump_json(indent=2))
    os.replace(path + '.tmp', path)


def load_search(search_id: str) -> Optional[CapacitySearch]:
    validate_id(search_id)
    path = os.path.join(_searches_dir(), f'{search_id}.json')
    try:
        with open(path, encoding='utf-8') as stream:
            search = CapacitySearch.model_validate_json(stream.read())
    except (OSError, ValueError):
        return None
    if search.status != 'running':
        _mark_unrun(search)  # Searches saved before "skipped" existed still read "waiting".
    return search


def _mark_unrun(search: CapacitySearch) -> None:
    """An ended search leaves its higher loads unrun: "waiting" read as if they still would."""
    for step in search.steps:
        if step.status == 'pending' and not step.execution_id:
            step.status = 'skipped'


def list_searches(plan_id: str) -> list[CapacitySearch]:
    try:
        names = sorted(os.listdir(_searches_dir()), reverse=True)
    except OSError:
        return []
    found = []
    for name in names:
        if name.endswith('.json'):
            search = load_search(name[:-5])
            if search is not None and search.plan_id == plan_id:
                found.append(search)
    return sorted(found, key=lambda search: search.created_at, reverse=True)


_running: dict[str, asyncio.Task] = {}


async def requirement_verdict(execution) -> Optional[str]:
    from app.services import requirements as requirement_service
    try:
        stored = await requirement_service.get_requirements(execution.project_id)
        return requirement_service.evaluate_run(stored, execution.summary, execution.jmx_path).verdict
    except Exception as error:
        logger.warning('Requirements not evaluated for %s: %s', execution.id, error)
        return None


async def start_capacity_search(plan_id: str, *, start_threads: int, max_threads: int, factor: float = 1.5,
                                max_error_rate: float = 1.0, confirm_stop: bool = True, refine_steps: int = 2,
                                approved_sha256=None, confirm_production=False):
    """Prepare the first step now, so an approval or production prompt reaches the caller; run the rest after.

    ``confirm_stop`` runs a stopping load once more before believing it: one run is one sample. ``refine_steps``
    then tries that many loads between the last that passed and the first that did not.
    """
    plan = store.get_test_plan(plan_id)
    if plan is None:
        raise LookupError('Test plan not found')
    if any(not task.done() for task in _running.values()):
        raise RuntimeError(tr('已有一个容量探索在运行，等它结束或取消后再开始',
                              'A capacity search is already running; wait for it to end or cancel it first'))
    if not 0 <= refine_steps <= MAX_REFINE_STEPS:
        raise ValueError(tr(f'细化步数需要在 0 到 {MAX_REFINE_STEPS} 之间',
                            f'Refinement steps must be between 0 and {MAX_REFINE_STEPS}'))
    steps = capacity_steps(start_threads, max_threads, factor)
    search = CapacitySearch(id=uuid.uuid4().hex, plan_id=plan_id, project_id=plan.project_id,
                            max_error_rate=max_error_rate, confirm_stop=confirm_stop, refine_steps=refine_steps,
                            steps=[CapacityStep(threads=n) for n in steps])
    first = await execution_lifecycle.prepare(
        plan.jmx_path, test_plan_id=plan_id, approved_sha256=approved_sha256, confirm_production=confirm_production,
        threads=steps[0], purpose={'kind': 'capacity', 'search_id': search.id, 'step': 0})
    search.steps[0].execution_id = first.id
    _save(search)
    _running[search.id] = asyncio.create_task(_search(search, plan, first, approved_sha256, confirm_production))
    return search


async def _run_step(search: CapacitySearch, plan, step: CapacityStep, approvals: dict, prepared=None) -> bool:
    """Run one load and fill the step with what it measured; False when the run did not complete."""
    from app.services.evidence import load_generator_saturated
    if prepared is None:
        prepared = await execution_lifecycle.prepare(
            plan.jmx_path, test_plan_id=plan.id, threads=step.threads, **approvals,
            purpose={'kind': 'capacity', 'search_id': search.id, 'step': search.steps.index(step)})
    step.execution_id, step.status = prepared.id, 'running'
    _save(search)
    # Its own task: a guard stop or a cancel of the run must not cancel the search with it. Held until
    # the machine has settled: the previous step's load would otherwise leak into this one.
    execution_lifecycle.submit_prepared(prepared, wait_for_resources=True)
    execution = await execution_lifecycle.wait(prepared.id)
    if execution is None or execution.status.value != 'completed' or not execution.summary:
        step.status = 'failed'
        step.note = (execution.error_message if execution else '') or tr('运行没有完成', 'The run did not complete')
        return False
    summary = execution.summary
    step.throughput, step.error_rate = summary.throughput, summary.error_rate
    step.p95 = (summary.percentiles or {}).get('p95')
    step.requirements = await requirement_verdict(execution)
    step.generator_saturated = load_generator_saturated(execution)
    return True


async def _search(search: CapacitySearch, plan, first, approved_sha256, confirm_production) -> None:
    approvals = {'approved_sha256': approved_sha256, 'confirm_production': confirm_production}
    previous: Optional[CapacityStep] = None  # The highest load that passed.
    stopped: Optional[CapacityStep] = None   # The lowest load that did not.
    reason = ''

    def passed(step: CapacityStep) -> None:
        nonlocal previous
        step.status = 'passed'
        search.capacity_threads, search.capacity_throughput = step.threads, step.throughput
        previous = step

    try:
        for index, step in enumerate(list(search.steps)):
            if not await _run_step(search, plan, step, approvals, first if index == 0 else None):
                search.status, search.stop_reason = 'failed', tr(
                    f'{step.threads} 线程的运行没有完成', f'The run at {step.threads} threads did not complete')
                return
            reason = judge_step(step, previous, search.max_error_rate)
            if not reason:
                passed(step)
                _save(search)
                continue
            step.status, step.note = 'stopped', reason
            if search.confirm_stop and not step.generator_saturated:
                # One run is one sample; a second at the same load tells a limit from a bad minute.
                repeat = CapacityStep(threads=step.threads, phase='confirm')
                search.steps.append(repeat)
                if not await _run_step(search, plan, repeat, approvals):
                    search.status, search.stop_reason = 'failed', tr(
                        f'{repeat.threads} 线程的复测没有完成',
                        f'The repeat run at {repeat.threads} threads did not complete')
                    return
                again = judge_step(repeat, previous, search.max_error_rate)
                if not again:
                    step.status, step.note = 'retried', tr(
                        f'{reason}；复测通过，按波动处理', f'{reason}; the repeat run passed, so this was noise')
                    passed(repeat)
                    _save(search)
                    continue
                repeat.status, repeat.note, reason = 'stopped', again, again
                step = repeat
            stopped = step
            break
        else:
            search.stop_reason = tr(f'最大负载 {previous.threads} 线程仍然通过',
                                    f'The highest load, {previous.threads} threads, still passed')
        if stopped is not None:
            search.stop_reason = tr(f'{stopped.threads} 线程：{reason}', f'{stopped.threads} threads: {reason}')
            if previous is not None and not stopped.generator_saturated:
                await _refine(search, plan, approvals, previous, stopped, passed)
        search.status = 'finished'
        await _report(search)
    except asyncio.CancelledError:
        search.status, search.stop_reason = 'cancelled', tr('已取消', 'Cancelled')
        for step in search.steps:
            if step.status in ('pending', 'running') and step.execution_id:
                # Also a prepared first step that never started, so it does not wait forever.
                await execution_lifecycle.cancel(step.execution_id)
                step.status = 'failed'
        raise
    except Exception as error:
        logger.exception('Capacity search %s failed', search.id)
        search.status, search.stop_reason = 'failed', str(error)
    finally:
        _mark_unrun(search)
        search.finished_at = datetime.now()
        _save(search)
        _running.pop(search.id, None)


async def _refine(search: CapacitySearch, plan, approvals: dict, previous: CapacityStep, stopped: CapacityStep,
                  passed) -> None:
    """Narrow the gap the ramp left: with a factor of 2 the limit is anywhere in a twofold range."""
    low, high, tried = previous, stopped, 0
    while tried < search.refine_steps:
        threads = refine_load(low.threads, high.threads)
        if threads is None:
            break
        tried += 1
        step = CapacityStep(threads=threads, phase='refine')
        search.steps.append(step)
        if not await _run_step(search, plan, step, approvals):
            break  # The ramp's answer stands; the step says what happened to it.
        reason = judge_step(step, low, search.max_error_rate)
        if reason:
            step.status, step.note, high = 'stopped', reason, step
            if step.generator_saturated:
                break
        else:
            passed(step)
            low = step
        _save(search)
    if tried:
        search.stop_reason += tr(f'；细化后 {low.threads} 线程通过，{high.threads} 线程不通过',
                                 f'; after refining, {low.threads} threads passed and {high.threads} did not')


def report_step(search: CapacitySearch) -> Optional[CapacityStep]:
    """The step that decided the search: the lowest load that did not pass, or the highest that did."""
    ran = [step for step in search.steps if step.execution_id]
    stopped = [step for step in ran if step.status == 'stopped']
    if stopped:
        return min(stopped, key=lambda step: step.threads)
    return max((step for step in ran if step.status == 'passed'), key=lambda step: step.threads, default=None)


async def _report(search: CapacitySearch) -> None:
    """One AI report per search instead of one per step; it recalls the earlier steps as runs at other loads."""
    step = report_step(search)
    if step is None or not settings.AUTO_ANALYSIS_ENABLED:
        return
    from app.services import post_run
    try:
        await post_run.start_reanalysis(step.execution_id)
        search.report_execution_id = step.execution_id
    except Exception as error:
        logger.warning('No report for capacity search %s: %s', search.id, error)


async def cancel_search(search_id: str) -> bool:
    task = _running.get(search_id)
    if task is None:
        return False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return True


async def stop_searches() -> None:
    for task in list(_running.values()):
        task.cancel()
    await asyncio.gather(*_running.values(), return_exceptions=True)


def recover_searches() -> None:
    """A restart ends a search in flight; say so instead of showing it running forever."""
    try:
        names = os.listdir(_searches_dir())
    except OSError:
        return
    for name in names:
        if not name.endswith('.json'):
            continue
        search = load_search(name[:-5])
        if search is not None and search.status == 'running':
            search.status, search.finished_at = 'failed', datetime.now()
            search.stop_reason = tr('后端重启，探索中断', 'The backend restarted and the search was cut off')
            _save(search)


# ── CI gate ───────────────────────────────────────────────

async def gate(execution_id: str, strict: bool = True) -> dict:
    """pass, fail or error for one run: requirements first, then regressions against the baseline.

    ``pending`` while the run or its report is still being written, so a CI job polls until it changes.
    A pass has to mean something was checked on numbers that can be trusted, so by default:
    a run whose load generator was saturated is an error (its numbers are that machine's, not the
    target's), a run with neither requirements nor a baseline is an error (nothing was checked), and
    a requirement that could not be evaluated fails. ``strict=False`` turns the last two into warnings.
    """
    from app.services import baseline_compare
    execution = store.get_execution(validate_id(execution_id))
    if execution is None:
        raise LookupError('Execution not found')
    result = {'execution_id': execution.id, 'status': execution.status.value, 'reasons': []}
    if execution.status not in TERMINAL or (not execution.summary and execution.report_status == 'running'):
        return {**result, 'gate': 'pending'}  # The process has ended but its results are still being read.
    if execution.status.value != 'completed' or not execution.summary:
        return {**result, 'gate': 'error',
                'reasons': [execution.error_message or f'Run {execution.status.value}']}
    from app.services.evidence import load_generator_saturated
    if load_generator_saturated(execution):
        return {**result, 'gate': 'error', 'reasons': [
            'The load generator was saturated (CPU or memory at its limit for much of the run), so the numbers '
            'measure that machine, not the target. Run with less load per generator, or on a larger one.']}
    verdict = await requirement_verdict(execution)
    result['requirements'] = verdict
    history = None
    try:
        history = await baseline_compare.evidence_for(execution, execution.summary)
    except Exception as error:
        logger.warning('Baseline comparison unavailable for gate of %s: %s', execution.id, error)
    baseline = (history or {}).get('baseline') or {}
    result['baseline'] = baseline.get('verdict')
    result['metrics'] = {'total_requests': execution.summary.total_requests,
                         'error_rate': execution.summary.error_rate, 'throughput': execution.summary.throughput,
                         'p95': (execution.summary.percentiles or {}).get('p95')}
    result['warnings'] = []
    if verdict == 'fail':
        result['reasons'].append('Performance requirements failed')
    elif verdict == 'incomplete':
        unknown = ('Some requirements could not be evaluated (for example, one names a request this run '
                   'did not send)')
        result['reasons' if strict else 'warnings'].append(unknown)
    elif verdict in (None, 'none'):
        if not baseline.get('verdict'):
            nothing = ('Nothing was checked: the project has no performance requirements and no baseline to '
                       'compare with. Set either one, or pass strict=false to accept this run unchecked.')
            if strict:
                return {**result, 'gate': 'error', 'reasons': [nothing]}
            result['warnings'].append(nothing)
        else:
            result['warnings'].append('No performance requirements are set; only the baseline was checked')
    if baseline.get('verdict') == 'regression':
        result['reasons'].append(f"Regression against baseline {baseline.get('name')}: {baseline.get('summary')}")
    result['gate'] = 'fail' if result['reasons'] else 'pass'
    return result
