"""Deterministic evidence for one execution, shared by reports, the analysis agent and MCP.

Everything here is computed, never generated: requirement verdicts, the
comparison with the active baseline and server-side metrics for the run's time
window. The analysis model receives the same numbers the report page shows.
"""
import json
import logging
from typing import Any, Optional

from pydantic import BaseModel

from app.models.schemas import DetailedTestSummary
from app.services import baseline_compare
from app.services import requirements as requirement_service
from app.services import run_diagnostics
from app.services import server_metrics
from app.services.jmeter import jmx_lint

logger = logging.getLogger(__name__)


class ExecutionEvidence(BaseModel):
    execution_id: str
    project_id: str
    test_plan_id: Optional[str] = None
    requirements: requirement_service.RequirementEvaluation
    baseline: Optional[dict[str, Any]] = None
    server_metrics: Optional[dict[str, Any]] = None
    run: Optional[dict[str, Any]] = None
    plan_issues: list[dict[str, Any]] = []
    planned_load: list[dict[str, Any]] = []
    configured_load: Optional[dict[str, Any]] = None
    load_generator: Optional[dict[str, Any]] = None
    # Requests of the plan that produced no sample: a report must not call the others "the only ones tested".
    unrun_requests: list[str] = []
    throughput_cap: Optional[float] = None  # requests/s the plan's throughput timers allow


CONFIGURED_LOAD_FIELDS = ('test_type', 'concurrent_users', 'ramp_up_seconds', 'duration_seconds', 'target_rps',
                          'think_time_ms')


def _read(jmx_path: Optional[str]) -> str:
    try:
        with open(jmx_path or '', encoding='utf-8') as stream:
            return stream.read()
    except OSError:
        return ''


def _unrun_requests(content: str, summary: DetailedTestSummary) -> list[str]:
    """Enabled samplers of the plan with no result in this run."""
    import xml.etree.ElementTree as ET
    if not content:
        return []
    try:
        root = ET.fromstring(content.encode('utf-8'))
    except ET.ParseError:
        return []
    ran = set(summary.per_label or {})
    labels = [element.get('testname') or '' for element in root.iter()
              if element.tag.endswith(('Sampler', 'SamplerProxy')) and element.get('enabled') != 'false']
    return [label for label in dict.fromkeys(labels) if label and label not in ran][:40]


def _plan_issues(jmx_path: Optional[str]) -> list[dict]:
    """What is wrong with the plan that ran. Older plans are never rewritten, so a report has to say it."""
    content = _read(jmx_path)
    return jmx_lint.lint(content) if content else []


async def build_evidence(execution, summary: DetailedTestSummary) -> ExecutionEvidence:
    """``execution`` is a TestExecutionInfo or its dict form."""
    get = execution.get if isinstance(execution, dict) else lambda key, default=None: getattr(execution, key, default)
    project_id = get('project_id') or 'default'
    try:
        stored = await requirement_service.get_requirements(project_id)
    except Exception as error:
        logger.warning('Requirements unavailable for %s: %s', project_id, error)
        stored = None
    try:
        server = await server_metrics.evidence_for(execution, summary)
    except Exception as error:
        logger.warning('Server metrics unavailable for %s: %s', get('id'), error)
        server = {'status': 'error', 'error': type(error).__name__}
    try:
        history = await baseline_compare.evidence_for(execution, summary)
    except Exception as error:
        logger.warning('Baseline comparison unavailable for %s: %s', get('id'), error)
        history = None
    try:
        run = run_diagnostics.build(summary)
    except Exception as error:
        logger.warning('Run diagnostics unavailable for %s: %s', get('id'), error)
        run = None
    config = (get('context_snapshot') or {}).get('test_config') or {}
    content = _read(get('jmx_path'))
    return ExecutionEvidence(
        unrun_requests=_unrun_requests(content, summary) if summary.total_requests else [],
        throughput_cap=requirement_service.throughput_cap(get('jmx_path')),
        execution_id=get('id'), project_id=project_id, test_plan_id=get('test_plan_id'), run=run,
        plan_issues=_plan_issues(get('jmx_path')), planned_load=jmx_lint.planned_load(content),
        configured_load={key: config.get(key) for key in CONFIGURED_LOAD_FIELDS} if config else None,
        load_generator=_load_generator(get('resource_snapshot_pre'), get('resource_snapshot_post')),
        requirements=requirement_service.evaluate_run(stored, summary, get('jmx_path')),
        baseline=history, server_metrics=server)


def _load_generator(before: Optional[dict], after: Optional[dict]) -> Optional[dict]:
    """CPU and memory of the machine running JMeter: before, after and, sampled every few seconds, during the run.

    ``saturated`` means the machine spent a fifth of the run or more at its limit, so the run measured
    the load generator as much as the target (see jmeter/load_generator.py).
    """
    def usage(snapshot):
        return {key: snapshot.get(key) for key in ('cpu_percent', 'memory_percent')} if snapshot else None
    if not before and not after:
        return None
    machine = {'before': usage(before), 'after': usage(after)}
    during = (after or {}).get('during')
    if during:
        machine['during'] = during
        machine['saturated'] = bool(during.get('saturated'))
    return machine


def load_generator_saturated(execution) -> bool:
    """Whether the run's own machine was at its limit for much of the run."""
    get = execution.get if isinstance(execution, dict) else lambda key, default=None: getattr(execution, key, default)
    return bool(((get('resource_snapshot_post') or {}).get('during') or {}).get('saturated'))


def _format_load_generator(machine: Optional[dict]) -> str:
    if not machine:
        return ''
    during = machine.get('during')
    if not during:
        return (f'## 压测机资源（运行前后各一次）\n{json.dumps(machine, ensure_ascii=False)}'
                '\n这只是起止两个时刻，不代表运行期间的峰值。运行前已经很高时，要考虑压测机本身限制了负载。')
    text = (f'## 压测机资源（运行期间每 {during.get("interval_seconds", 5):g} 秒采样一次，共 {during.get("samples")} 次）\n'
            f'{json.dumps(machine, ensure_ascii=False)}')
    if machine.get('saturated'):
        share = float(during.get('saturated_share') or 0) * 100
        return (text + f'\n**压测机饱和**：运行期间有 {share:.0f}% 的时间 CPU 或内存达到上限'
                f'（CPU 峰值 {during.get("cpu_peak")}%）。这次测到的响应时间和吞吐量受压测机自身限制，'
                '不能当作被测系统的性能；必须在整体评估和分析限制里写明，并建议降低单机负载或改用分布式后重测。'
                '不要据此提出服务端瓶颈的假设。')
    return text + '\n压测机在运行期间没有达到上限，结果可以按被测系统的表现解读。'


def format_for_prompt(evidence: ExecutionEvidence) -> str:
    load = evidence.configured_load
    return '\n\n'.join(filter(None, [
        f'## 配置负载\n{json.dumps(load, ensure_ascii=False)}' if load else '',
        _format_planned_load(evidence.planned_load),
        _format_pacing(evidence.throughput_cap),
        _format_unrun(evidence.unrun_requests),
        _format_plan_issues(evidence.plan_issues),
        _format_load_generator(evidence.load_generator),
        requirement_service.format_for_prompt(evidence.requirements),
        run_diagnostics.format_for_prompt(evidence.run),
        baseline_compare.format_for_prompt(evidence.baseline),
        server_metrics.format_for_prompt(evidence.server_metrics),
    ]))


def _format_planned_load(groups: list[dict]) -> str:
    if not groups:
        return ''
    return ('## planned_load（测试计划实际的线程组，负载是否符合预期以此为准）\n'
            + json.dumps(groups, ensure_ascii=False, separators=(',', ':'))
            + '\ndelay_seconds 是开始时间，duration_seconds 是持续时间，各线程组叠加后就是预期的线程曲线。')


def _format_pacing(cap: Optional[float]) -> str:
    if not cap:
        return ''
    return (f'## 计划的吞吐上限\n计划里的吞吐定时器把总吞吐限制在约 {cap:g} 请求/秒。实测吞吐接近这个值时，'
            '是计划在限速，不能当作服务端的处理上限，也不要据此提出"服务端到顶"的假设。')


def _format_unrun(labels: list[str]) -> str:
    if not labels:
        return ''
    return ('## 计划里有、这次却没有任何结果的请求\n' + '、'.join(labels)
            + '\n它们没有被执行（常见原因：前面的请求失败后线程按计划停止，或控制器跳过了它们）。'
            '不要说其余请求是"唯一被测的接口"，也不要对这些请求的性能下结论。')


def _format_plan_issues(issues: list[dict]) -> str:
    if not issues:
        return ''
    lines = '\n'.join(f"- {issue['message']}" for issue in issues)
    return f'## 测试计划本身的问题（程序检查）\n{lines}\n解读结果时要考虑这些问题，例如断言失效时错误率可能偏低。'


def comparison_values(evidence: Optional[ExecutionEvidence]) -> list[float]:
    """Numbers the analysis may quote from other runs and from the requirements."""
    if evidence is None:
        return []
    values = [item.expected for item in evidence.requirements.items]
    if evidence.throughput_cap:
        values.append(evidence.throughput_cap)  # The plan's own limit may be quoted.
    for comparison in ((evidence.baseline or {}).get('baseline'), (evidence.baseline or {}).get('previous')):
        for row in [*((comparison or {}).get('deltas') or []), *((comparison or {}).get('transactions') or [])]:
            values += [row.get('baseline'), row.get('target')]
    return [float(value) for value in values if isinstance(value, (int, float))]
