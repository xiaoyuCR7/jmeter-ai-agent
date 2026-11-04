"""Runs started to answer a question: does a hypothesis hold, how much load does the system take,
and did this run pass its gate.

Code decides every step. A verification run reruns the hypothesis' plan at the load its prediction
names; the hypothesis ledger then judges it as it judges any later run. A capacity search raises the
load step by step and stops at the first step that fails a requirement, errors too much or stops
gaining throughput. The gate turns a run's evidence into pass, fail or error for a CI job.
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
        raise ValueError('这条假设已经有结论，不需要再验证')
    expected = hyp.prediction or prediction.from_text(hyp.testable_prediction)
    if not prediction.checkable(expected):
        raise ValueError('这条假设没有程序能检查的预测，或者要先做一项改动再验证；请改完后手动运行，再在报告里判定')
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
        threads=int(threads) if threads else None, purpose=purpose)


# ── Capacity search ───────────────────────────────────────

MAX_STEPS = 12
# A step that turned less than this share of its added users into throughput has saturated something.
PLATEAU_SHARE = 0.2


class CapacityStep(BaseModel):
    threads: int
    execution_id: Optional[str] = None
    status: str = 'pending'  # pending, running, passed, stopped, failed
    throughput: Optional[float] = None
    p95: Optional[float] = None
    error_rate: Optional[float] = None
    requirements: Optional[str] = None  # pass, fail, incomplete, none
    note: str = ''


class CapacitySearch(BaseModel):
    id: str
    plan_id: str
    project_id: str
    status: str = 'running'  # running, finished, failed, cancelled
    max_error_rate: float
    steps: list[CapacityStep]
    capacity_threads: Optional[int] = None  # The highest load that passed every check.
    capacity_throughput: Optional[float] = None
    stop_reason: str = ''
    created_at: datetime = Field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None


def capacity_steps(start: int, maximum: int, factor: float) -> list[int]:
    """Loads to try: from ``start`` multiplied by ``factor`` up to ``maximum``, which is always the last."""
    if start < 1 or maximum < start:
        raise ValueError('起始线程数至少为 1，且不大于最大线程数')
    if not 1.1 <= factor <= 4:
        raise ValueError('每步倍数需要在 1.1 到 4 之间')
    steps, current = [start], start
    while current < maximum and len(steps) < MAX_STEPS:
        current = min(maximum, max(current + 1, math.ceil(current * factor)))
        steps.append(current)
    if steps[-1] != maximum:
        raise ValueError(f'最多 {MAX_STEPS} 步；请调大倍数或调小最大线程数')
    return steps


def judge_step(step: CapacityStep, previous: Optional[CapacityStep], max_error_rate: float) -> Optional[str]:
    """Why the search stops at this step, or None when the system took this load."""
    if step.requirements == 'fail':
        return '性能需求不达标'
    if step.error_rate is not None and step.error_rate > max_error_rate:
        return f'错误率 {step.error_rate:g}% 超过 {max_error_rate:g}%'
    if previous is not None and previous.throughput and step.throughput is not None:
        added = (step.threads - previous.threads) / previous.threads
        gained = (step.throughput - previous.throughput) / previous.throughput
        if added > 0 and gained < PLATEAU_SHARE * added:
            return f'吞吐量不再随负载增长（线程 +{added * 100:.0f}%，吞吐量 {gained * 100:+.1f}%）'
    return None


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
            return CapacitySearch.model_validate_json(stream.read())
    except (OSError, ValueError):
        return None


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
        return requirement_service.evaluate(stored, execution.summary,
                                            requirement_service.sampler_routes(execution.jmx_path)).verdict
    except Exception as error:
        logger.warning('Requirements not evaluated for %s: %s', execution.id, error)
        return None


async def start_capacity_search(plan_id: str, *, start_threads: int, max_threads: int, factor: float = 1.5,
                                max_error_rate: float = 1.0, approved_sha256=None, confirm_production=False):
    """Prepare the first step now, so an approval or production prompt reaches the caller; run the rest after."""
    plan = store.get_test_plan(plan_id)
    if plan is None:
        raise LookupError('Test plan not found')
    if any(not task.done() for task in _running.values()):
        raise RuntimeError('已有一个容量探索在运行，等它结束或取消后再开始')
    steps = capacity_steps(start_threads, max_threads, factor)
    search = CapacitySearch(id=uuid.uuid4().hex, plan_id=plan_id, project_id=plan.project_id,
                            max_error_rate=max_error_rate, steps=[CapacityStep(threads=n) for n in steps])
    first = await execution_lifecycle.prepare(
        plan.jmx_path, test_plan_id=plan_id, approved_sha256=approved_sha256, confirm_production=confirm_production,
        threads=steps[0], purpose={'kind': 'capacity', 'search_id': search.id, 'step': 0})
    search.steps[0].execution_id = first.id
    _save(search)
    _running[search.id] = asyncio.create_task(_search(search, plan, first, approved_sha256, confirm_production))
    return search


async def _search(search: CapacitySearch, plan, first, approved_sha256, confirm_production) -> None:
    previous: Optional[CapacityStep] = None
    try:
        for index, step in enumerate(search.steps):
            if index == 0:
                prepared = first
            else:
                prepared = await execution_lifecycle.prepare(
                    plan.jmx_path, test_plan_id=plan.id, approved_sha256=approved_sha256,
                    confirm_production=confirm_production, threads=step.threads,
                    purpose={'kind': 'capacity', 'search_id': search.id, 'step': index})
            step.execution_id, step.status = prepared.id, 'running'
            _save(search)
            # Its own task: a guard stop or a cancel of the run must not cancel the search with it.
            execution_lifecycle.submit_prepared(prepared)
            execution = await execution_lifecycle.wait(prepared.id)
            if execution is None or execution.status.value != 'completed' or not execution.summary:
                step.status = 'failed'
                step.note = (execution.error_message if execution else '') or '运行没有完成'
                search.status, search.stop_reason = 'failed', f'{step.threads} 线程的运行没有完成'
                return
            summary = execution.summary
            step.throughput, step.error_rate = summary.throughput, summary.error_rate
            step.p95 = (summary.percentiles or {}).get('p95')
            step.requirements = await requirement_verdict(execution)
            reason = judge_step(step, previous, search.max_error_rate)
            if reason:
                step.status, step.note = 'stopped', reason
                search.stop_reason = f'{step.threads} 线程：{reason}'
                break
            step.status = 'passed'
            search.capacity_threads, search.capacity_throughput = step.threads, step.throughput
            previous = step
            _save(search)
        else:
            search.stop_reason = f'最大负载 {search.steps[-1].threads} 线程仍然通过'
        search.status = 'finished'
    except asyncio.CancelledError:
        search.status, search.stop_reason = 'cancelled', '已取消'
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
        search.finished_at = datetime.now()
        _save(search)
        _running.pop(search.id, None)


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
            search.status, search.stop_reason, search.finished_at = 'failed', '后端重启，探索中断', datetime.now()
            _save(search)


# ── CI gate ───────────────────────────────────────────────

async def gate(execution_id: str) -> dict:
    """pass, fail or error for one run: requirements first, then regressions against the baseline.

    ``pending`` while the run or its report is still being written, so a CI job polls until it changes.
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
        result['warnings'].append('Some requirements could not be evaluated')
    elif verdict in (None, 'none'):
        result['warnings'].append('No performance requirements are set; only the baseline was checked')
    if baseline.get('verdict') == 'regression':
        result['reasons'].append(f"Regression against baseline {baseline.get('name')}: {baseline.get('summary')}")
    result['gate'] = 'fail' if result['reasons'] else 'pass'
    return result
