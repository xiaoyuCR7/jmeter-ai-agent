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

logger = logging.getLogger(__name__)


class ExecutionEvidence(BaseModel):
    execution_id: str
    project_id: str
    test_plan_id: Optional[str] = None
    requirements: requirement_service.RequirementEvaluation
    baseline: Optional[dict[str, Any]] = None
    server_metrics: Optional[dict[str, Any]] = None
    run: Optional[dict[str, Any]] = None
    configured_load: Optional[dict[str, Any]] = None


CONFIGURED_LOAD_FIELDS = ('test_type', 'concurrent_users', 'ramp_up_seconds', 'duration_seconds', 'target_rps',
                          'think_time_ms')


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
    return ExecutionEvidence(
        execution_id=get('id'), project_id=project_id, test_plan_id=get('test_plan_id'), run=run,
        configured_load={key: config.get(key) for key in CONFIGURED_LOAD_FIELDS} if config else None,
        requirements=requirement_service.evaluate(stored, summary, requirement_service.sampler_routes(get('jmx_path'))),
        baseline=history, server_metrics=server)


def format_for_prompt(evidence: ExecutionEvidence) -> str:
    load = evidence.configured_load
    return '\n\n'.join(filter(None, [
        f'## 配置负载\n{json.dumps(load, ensure_ascii=False)}' if load else '',
        requirement_service.format_for_prompt(evidence.requirements),
        run_diagnostics.format_for_prompt(evidence.run),
        baseline_compare.format_for_prompt(evidence.baseline),
        server_metrics.format_for_prompt(evidence.server_metrics),
    ]))


def comparison_values(evidence: Optional[ExecutionEvidence]) -> list[float]:
    """Numbers the analysis may quote from other runs and from the requirements."""
    if evidence is None:
        return []
    values = [item.expected for item in evidence.requirements.items]
    for comparison in ((evidence.baseline or {}).get('baseline'), (evidence.baseline or {}).get('previous')):
        for row in [*((comparison or {}).get('deltas') or []), *((comparison or {}).get('transactions') or [])]:
            values += [row.get('baseline'), row.get('target')]
    return [float(value) for value in values if isinstance(value, (int, float))]
