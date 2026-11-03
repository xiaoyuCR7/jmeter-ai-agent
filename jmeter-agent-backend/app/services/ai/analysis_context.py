"""What a report and a follow-up question know besides the run itself, read the same way by both.

In order of how comparable it is: earlier runs of the plan under the same configuration, runs of
the plan under another load, the same requests in other plans of the project, open and decided
hypotheses, and lessons people confirmed. None of it may block a report.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.models.schemas import FactSheet
from app.services.ai.memory import fact_ledger, hypothesis_ledger

logger = logging.getLogger(__name__)

MAX_RECENT_RUNS = 3
MAX_OTHER_LOADS = 3
MAX_REQUEST_LINES = 8
RUNS_PER_REQUEST = 2  # So one busy plan does not fill every line.
PROJECT_FACTS = 20
MAX_OPEN_HYPOTHESES = 5
MAX_DECIDED_HYPOTHESES = 3


@dataclass
class Recall:
    text: str = ''
    numbers: list[float] = field(default_factory=list)  # Values the number check may see quoted.
    counts: dict = field(default_factory=dict)
    experience_ids: list[str] = field(default_factory=list)


def run_context(execution) -> dict:
    """Project, environment and the load the run really had: thread groups of the JMX it ran."""
    from app.services.jmeter import jmx_lint
    snapshot = getattr(execution, 'context_snapshot', None) or {}
    config = snapshot.get('test_config') or {}
    try:
        groups = jmx_lint.planned_load(Path(execution.jmx_path).read_text(encoding='utf-8'))
    except (AttributeError, OSError, TypeError, ValueError):
        groups = []
    threads = sum(group['threads'] for group in groups) or config.get('concurrent_users') or 0
    duration = max((group['duration_seconds'] for group in groups), default=0) or config.get('duration_seconds') or 0
    return {'project_id': snapshot.get('project_id') or getattr(execution, 'project_id', '') or '',
            'environment_fingerprint': snapshot.get('environment_fingerprint') or '',
            'load': {'threads': threads, 'duration_seconds': duration, 'think_time_ms': config.get('think_time_ms')}}


def _totals(fact: FactSheet) -> list[float]:
    return [fact.throughput, fact.avg_response_time, fact.error_rate, *fact.percentiles.values()]


def fact_values(facts: list[FactSheet]) -> list[float]:
    values = [value for fact in facts[:MAX_RECENT_RUNS] for value in _totals(fact)]
    return [float(value) for value in values if isinstance(value, (int, float))]


async def recent_facts(module: str, version: str, scope, execution_id: str, **read_options) -> list[FactSheet]:
    """Earlier runs of the same plan under the same configuration."""
    if not scope:
        return []
    found = await fact_ledger.get_facts_for_module_version(module, version, limit=5, context_scope=scope, **read_options)
    return [item for item in found if item.execution_id != execution_id]


def format_recent_facts(facts: list[FactSheet]) -> str:
    if not facts:
        return ''
    lines = [f'## 这个计划最近的运行（同样配置，程序记录的实测值，共 {len(facts)} 条）']
    for fact in facts[:MAX_RECENT_RUNS]:
        lines.append(f"- [{fact.execution_id[:8]}] {fact.test_type}: TPS={fact.throughput}, "
                     f"平均={fact.avg_response_time}ms, P95={fact.percentiles.get('p95', 'N/A')}ms, "
                     f"P99={fact.percentiles.get('p99', 'N/A')}ms, 错误率={fact.error_rate}%")
    return '\n'.join(lines)


def previous_requests(fact: FactSheet, labels: set[str]) -> tuple[str, list[float]]:
    """Each request of the newest comparable run, so a change can be traced to the request that moved."""
    lines, numbers = [], []
    for label in sorted(labels)[:MAX_REQUEST_LINES]:
        row = (fact.per_label or {}).get(label) or {}
        if not row.get('count'):
            continue
        lines.append(f"- {label}：P95={row.get('p95_response_time')}ms，平均={row.get('avg_response_time')}ms，"
                     f"错误率={row.get('error_rate')}%，样本 {row.get('count')}")
        numbers += [row.get(key) for key in ('p95_response_time', 'avg_response_time', 'error_rate')]
    if not lines:
        return '', []
    return (f'## 上一次同样配置的运行 [{fact.execution_id[:8]}] 各接口\n' + '\n'.join(lines),
            [float(value) for value in numbers if isinstance(value, (int, float))])


def _load_text(load: dict) -> str:
    parts = [f"线程数 {load.get('threads') or '未知'}"]
    if load.get('duration_seconds'):
        parts.append(f"时长 {load['duration_seconds']}s")
    if load.get('think_time_ms') is not None:
        parts.append(f"think time {load['think_time_ms']}ms")
    return '，'.join(parts)


def format_other_loads(facts: list[FactSheet], load: dict) -> str:
    if not facts:
        return ''
    lines = [f'## 这个计划在其它负载下的运行（同一环境；本次 {_load_text(load)}，看负载变化时用）']
    for fact in facts:
        lines.append(f"- [{fact.execution_id[:8]}] {_load_text(fact.load)}：TPS={fact.throughput}, "
                     f"P95={fact.percentiles.get('p95', 'N/A')}ms, 错误率={fact.error_rate}%")
    return '\n'.join(lines)


def same_requests(facts: list[FactSheet], labels: set[str], plan_names: dict) -> tuple[str, list[float]]:
    """The same requests measured in other plans of the project, newest first."""
    lines, numbers = [], []
    for label in sorted(labels):
        measured = [fact for fact in facts if ((fact.per_label or {}).get(label) or {}).get('count')]
        for fact in measured[:RUNS_PER_REQUEST]:
            if len(lines) == MAX_REQUEST_LINES:
                break
            row = fact.per_label[label]
            lines.append(f"- {label}：计划「{plan_names.get(fact.module, fact.module)}」的运行 [{fact.execution_id[:8]}]"
                         f"（{_load_text(fact.load)}）P95={row.get('p95_response_time')}ms，"
                         f"平均={row.get('avg_response_time')}ms，错误率={row.get('error_rate')}%")
            numbers += [row.get(key) for key in ('p95_response_time', 'avg_response_time', 'error_rate', 'throughput')]
    if not lines:
        return '', []
    return ('## 同一项目其它计划里的相同接口（环境或负载可能不同，只作参照）\n' + '\n'.join(lines),
            [float(value) for value in numbers if isinstance(value, (int, float))])


def relevant_hypotheses(hypotheses: list, plan_id: str, labels: set[str], execution_id: str = '') -> list:
    """Earlier hypotheses about this plan or about a request this run has; a run's own are not history."""
    def about_run(hyp):
        return hyp.source_execution_id != execution_id and (
            hyp.module == plan_id or (hyp.prediction or {}).get('label') in labels)
    found = [hyp for hyp in hypotheses if about_run(hyp)]
    by_status = {}
    for hyp in found:
        by_status.setdefault(hyp.status.value, []).append(hyp)
    return (by_status.get('untested', [])[:MAX_OPEN_HYPOTHESES] + by_status.get('confirmed', [])[:MAX_DECIDED_HYPOTHESES]
            + by_status.get('refuted', [])[:MAX_DECIDED_HYPOTHESES])


async def recall(execution) -> Recall:
    """Everything remembered that bears on this run, each part on its own so one failure costs only that part."""
    try:
        return await _recall(execution)
    except Exception as error:  # Memory is a help to a report, never a reason it is missing.
        logger.warning('Nothing recalled for %s: %s', getattr(execution, 'id', '?'), error)
        return Recall()


async def _recall(execution) -> Recall:
    from app.services.ai.memory import experience
    from app.services.ai.memory.hypothesis_ledger import HypothesisLedger
    from app.services.ai.memory.provenance import snapshot_scope
    from app.services.storage import store
    context = run_context(execution)
    project_id, load = context['project_id'], context['load']
    labels = set(getattr(execution.summary, 'per_label', None) or {})
    plan_id = execution.test_plan_id
    parts, numbers, counts, cited = [], [], {}, []

    try:
        same = await recent_facts(plan_id, 'unknown', snapshot_scope(execution.context_snapshot), execution.id)
        counts['same_config'] = len(same)
        parts.append(format_recent_facts(same))
        numbers += fact_values(same)
        if same:
            text, request_numbers = previous_requests(same[0], labels)
            parts.append(text)
            numbers += request_numbers
        plan_facts = await fact_ledger.get_facts_for_module_version(plan_id, limit=10)
        same_ids = {fact.execution_id for fact in same}
        others = [fact for fact in plan_facts if fact.execution_id != execution.id and fact.execution_id not in same_ids
                  and fact.environment_fingerprint == context['environment_fingerprint']
                  and fact.load.get('threads')][:MAX_OTHER_LOADS]
        counts['other_loads'] = len(others)
        parts.append(format_other_loads(others, load))
        numbers += fact_values(others)
    except Exception as error:
        logger.warning('Plan history unavailable for %s: %s', execution.id, error)

    try:
        project_facts = [fact for fact in await fact_ledger.get_project_facts(project_id, limit=PROJECT_FACTS)
                         if fact.module != plan_id]
        names = {fact.module: getattr(store.get_test_plan(fact.module), 'name', fact.module) for fact in project_facts}
        text, request_numbers = same_requests(project_facts, labels, names)
        counts['other_plans'] = len(request_numbers) // 4
        parts.append(text)
        numbers += request_numbers
    except Exception as error:
        logger.warning('Project history unavailable for %s: %s', execution.id, error)

    try:
        hypotheses = (await hypothesis_ledger.list_for_project(project_id, limit=100) if project_id
                      else await hypothesis_ledger.list_for_module(plan_id, limit=20))
        chosen = relevant_hypotheses(hypotheses, plan_id, labels, execution.id)
        counts['hypotheses'] = len(chosen)
        if chosen:
            parts.append('## 这个项目的历史假设（预测成立只说明数字按预测变化，不代表原因已证实）\n'
                         + HypothesisLedger.format_for_prompt(chosen))
    except Exception as error:
        logger.warning('Hypotheses unavailable for %s: %s', execution.id, error)

    try:
        lessons = await asyncio.to_thread(experience.relevant, project_id, labels) if project_id else []
        counts['experiences'] = len(lessons)
        cited = [doc.id for doc in lessons]
        parts.append(experience.format_for_prompt(lessons))
        if lessons:
            await asyncio.to_thread(experience.cite, lessons)
    except Exception as error:
        logger.warning('Lessons unavailable for %s: %s', execution.id, error)

    text = '\n\n'.join(part.strip('\n') for part in parts if part)
    return Recall(text='\n\n' + text if text else '', numbers=numbers, counts=counts, experience_ids=cited)
