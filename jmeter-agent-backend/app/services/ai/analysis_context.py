"""History and archived knowledge, read the same way by the report and by follow-up questions."""
import asyncio
import logging
from typing import Optional

from app.models.schemas import FactSheet
from app.services.ai.memory import fact_ledger

logger = logging.getLogger(__name__)

MAX_KNOWLEDGE_CHARS = 3000
MAX_RECENT_RUNS = 3


async def recent_facts(module: str, version: str, scope: Optional[str], execution_id: str,
                       **read_options) -> list[FactSheet]:
    """Earlier runs of the same plan under the same configuration; other scopes are not comparable."""
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


def fact_values(facts: list[FactSheet]) -> list[float]:
    """Numbers an analysis may quote from those runs without them counting as wrong about this one."""
    values = []
    for fact in facts[:MAX_RECENT_RUNS]:
        values += [fact.throughput, fact.avg_response_time, fact.error_rate, *fact.percentiles.values()]
    return [float(value) for value in values if isinstance(value, (int, float))]


async def knowledge_context(retriever, summary: dict, module: str) -> str:
    """Standards, known issues and defect reviews people archived. Optional: analysis works without them."""
    try:
        found = await asyncio.to_thread(retriever.retrieve, summary, 5, module)
    except Exception as error:
        logger.warning('Knowledge retrieval skipped: %s', type(error).__name__)
        return ''
    if not (found['similar_defects'] or found['known_issues'] or found['module_standards']):
        return ''
    return ('\n\n## 知识库资料（人工归档，只作线索，不是本次测试的事实）\n'
            + found['formatted_context'][:MAX_KNOWLEDGE_CHARS])
