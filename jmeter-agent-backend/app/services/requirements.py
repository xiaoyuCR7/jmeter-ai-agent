"""Performance requirements: extracted from test documents, judged against every run.

A project owns one requirement set: global or per-transaction metric thresholds
plus the intended load profile. Each item keeps the exact document clause it
came from, so a verdict can always be traced back to the source text.
"""
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import lru_cache
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.models.schemas import AIChatMessage, DetailedTestSummary

logger = logging.getLogger(__name__)

Metric = Literal['avg_response_time', 'p90', 'p95', 'p99', 'max_response_time', 'error_rate', 'throughput']
Operator = Literal['lte', 'lt', 'gte', 'gt']

METRIC_LABELS = {
    'avg_response_time': '平均响应时间(ms)', 'p90': 'P90(ms)', 'p95': 'P95(ms)', 'p99': 'P99(ms)',
    'max_response_time': '最大响应时间(ms)', 'error_rate': '错误率(%)', 'throughput': '吞吐量(TPS)',
}
_SUMMARY_FIELDS = {
    'avg_response_time': 'avg_response_time', 'p95': 'p95_response_time', 'p99': 'p99_response_time',
    'max_response_time': 'max_response_time', 'error_rate': 'error_rate', 'throughput': 'throughput',
}
_OPERATOR_TEXT = {'lte': '≤', 'lt': '<', 'gte': '≥', 'gt': '>'}


class RequirementItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    scope: Literal['global', 'transaction'] = 'global'
    transaction: Optional[str] = Field(None, max_length=200, description='Sampler label or a distinctive part of it')
    metric: Metric
    operator: Operator
    value: float
    source: str = Field('', max_length=1000, description='Exact clause quoted from the source document')
    source_verified: bool = Field(False, description='The quoted clause was found in the source text')

    @field_validator('transaction')
    @classmethod
    def _strip(cls, value):
        return value.strip() or None if isinstance(value, str) else value


class LoadProfile(BaseModel):
    concurrency: Optional[int] = Field(None, ge=1)
    ramp_up_seconds: Optional[int] = Field(None, ge=0)
    duration_seconds: Optional[int] = Field(None, ge=1)
    target_tps: Optional[float] = Field(None, gt=0)
    source: str = Field('', max_length=1000)


class PerformanceRequirements(BaseModel):
    items: list[RequirementItem] = Field(default_factory=list)
    load_profile: LoadProfile = Field(default_factory=LoadProfile)
    assumptions: list[str] = Field(default_factory=list, description='Gaps or interpretations the extractor had to make')
    source_name: str = ''
    updated_at: Optional[str] = None

    @field_validator('items')
    @classmethod
    def _unique_ids(cls, items):
        ids = [item.id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError('Requirement ids must be unique')
        return items


# ── Persistence ──────────────────────────────────────────────

async def get_requirements(project_id: str) -> Optional[PerformanceRequirements]:
    from app.models.db import PerformanceRequirementModel
    from app.services.db_store import db_store
    async with await db_store._session() as session:
        row = await session.get(PerformanceRequirementModel, project_id)
        return PerformanceRequirements.model_validate(row.data) if row else None


async def save_requirements(project_id: str, requirements: PerformanceRequirements) -> PerformanceRequirements:
    from app.models.db import PerformanceRequirementModel
    from app.services.db_store import db_store
    requirements = requirements.model_copy(update={'updated_at': datetime.now().isoformat(timespec='seconds')})
    async with await db_store._session() as session:
        row = await session.get(PerformanceRequirementModel, project_id)
        if row is None:
            session.add(PerformanceRequirementModel(project_id=project_id, data=requirements.model_dump()))
        else:
            row.data = requirements.model_dump()
        await session.commit()
    return requirements


async def delete_requirements(project_id: str) -> bool:
    from app.models.db import PerformanceRequirementModel
    from app.services.db_store import db_store
    async with await db_store._session() as session:
        row = await session.get(PerformanceRequirementModel, project_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


# ── Extraction ───────────────────────────────────────────────

EXTRACTION_PROMPT = """你是性能测试需求分析师。从下面的测试文档中抽取**可量化的性能需求**，只输出一个 JSON 对象，不要输出其它文字。

JSON 结构：
{
  "items": [
    {"id": "R1", "scope": "global" 或 "transaction",
     "transaction": "接口/事务名称（scope=transaction 时必填，否则为 null）",
     "metric": "avg_response_time|p90|p95|p99|max_response_time|error_rate|throughput",
     "operator": "lte|lt|gte|gt", "value": 数字,
     "source": "从文档中逐字摘录的原句"}
  ],
  "load_profile": {"concurrency": 整数或null, "ramp_up_seconds": 整数或null,
                   "duration_seconds": 整数或null, "target_tps": 数字或null, "source": "逐字摘录的原句"},
  "assumptions": ["你不得不做出的解释或文档缺失的信息"]
}

规则：
- 响应时间统一换算为毫秒；错误率用百分数（0.1% 写 0.1）；吞吐量用 每秒请求数。
- "响应时间不超过 X" 若未说明分位，使用 avg_response_time，并在 assumptions 里说明。
- source 必须是文档中原样出现的句子，不能改写；找不到原句的需求不要输出。
- 文档没有提到的指标不要编造。
"""


def _normalize(text: str) -> str:
    return re.sub(r'\s+', '', text or '').lower()


def _json_object(text: str) -> dict:
    fenced = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find('{'):text.rfind('}') + 1]
    if not candidate:
        raise ValueError('Model returned no JSON object')
    return json.loads(candidate)


def mark_verified_sources(requirements: PerformanceRequirements, document: str) -> PerformanceRequirements:
    """A requirement is only as trustworthy as its quote; flag quotes absent from the document."""
    haystack = _normalize(document)
    for item in requirements.items:
        item.source_verified = bool(item.source) and _normalize(item.source) in haystack
    return requirements


async def extract_requirements(document: str, adapter, source_name: str = '') -> PerformanceRequirements:
    if not document.strip():
        raise ValueError('Document text is empty')
    response = await adapter.chat([
        AIChatMessage(role='system', content=EXTRACTION_PROMPT),
        AIChatMessage(role='user', content=document[:60000]),
    ], temperature=0)
    data = _json_object(response if isinstance(response, str) else str(response))
    items = []
    for index, raw in enumerate(data.get('items') or [], start=1):
        try:
            raw = {**raw, 'id': str(raw.get('id') or f'R{index}')}
            if raw.get('scope') != 'transaction':
                raw['scope'], raw['transaction'] = 'global', None
            items.append(RequirementItem.model_validate(raw))
        except Exception as error:
            logger.info('Dropped malformed extracted requirement %s: %s', index, error)
    seen, unique = set(), []
    for item in items:
        if item.id in seen:
            item.id = f'{item.id}-{len(unique) + 1}'
        seen.add(item.id)
        unique.append(item)
    try:
        profile = LoadProfile.model_validate(data.get('load_profile') or {})
    except Exception:
        profile = LoadProfile()
    requirements = PerformanceRequirements(
        items=unique, load_profile=profile,
        assumptions=[str(a) for a in data.get('assumptions') or []][:20], source_name=source_name)
    return mark_verified_sources(requirements, document)


# ── Evaluation ───────────────────────────────────────────────

class RequirementVerdict(BaseModel):
    id: str
    description: str
    transaction: Optional[str] = None
    matched_labels: list[str] = Field(default_factory=list)
    metric: Metric
    operator: Operator
    expected: float
    actual: Optional[float] = None
    status: Literal['pass', 'fail', 'missing']
    gap_pct: Optional[float] = Field(None, description='How far the actual value is beyond the limit, in percent')
    source: str = ''
    source_verified: bool = False


class RequirementEvaluation(BaseModel):
    verdict: Literal['pass', 'fail', 'incomplete', 'none']
    passed: int = 0
    failed: int = 0
    missing: int = 0
    items: list[RequirementVerdict] = Field(default_factory=list)
    load_profile: Optional[LoadProfile] = None


_HTTP_METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')
_ROUTE_PATTERN = re.compile(r'^\s*(?:(%s)\s+)?(\S+)\s*$' % '|'.join(_HTTP_METHODS), re.IGNORECASE)


def _normalize_path(path: str) -> str:
    if '://' in path:
        path = urlsplit(path).path
    path = path.split('?', 1)[0].strip()
    return path.rstrip('/') or '/'


def _route_keys(transaction: str) -> set[str]:
    """'POST /api/pizza' -> {'POST /api/pizza'}; a bare '/api/pizza' matches any method."""
    match = _ROUTE_PATTERN.match(transaction)
    if not match or not (match.group(2).startswith('/') or '://' in match.group(2)):
        return set()
    method, path = match.group(1), _normalize_path(match.group(2))
    return {f'{method.upper()} {path}'} if method else {path}


@lru_cache(maxsize=64)
def _sampler_routes_cached(jmx_path: str, mtime_ns: int) -> dict[str, frozenset[str]]:
    routes: dict[str, set[str]] = {}
    for element in ET.parse(jmx_path).iter():
        if element.get('testclass') not in ('HTTPSamplerProxy', 'HTTPSampler'):
            continue
        props = {prop.get('name'): (prop.text or '') for prop in element if prop.tag == 'stringProp'}
        path = props.get('HTTPSampler.path', '')
        if not path:
            continue
        path = _normalize_path(path)
        method = (props.get('HTTPSampler.method') or 'GET').upper()
        routes.setdefault(element.get('testname', ''), set()).update({f'{method} {path}', path})
    return {label: frozenset(keys) for label, keys in routes.items()}


def sampler_routes(jmx_path: Optional[str]) -> dict[str, frozenset[str]]:
    """Map each HTTP sampler label in the JMX that ran to 'METHOD /path' keys.

    JMeter reports sampler names ("RecommendPizza"), while documents name endpoints
    ("POST /api/pizza"); this lets a requirement written either way find its sampler.
    """
    if not jmx_path:
        return {}
    try:
        return _sampler_routes_cached(jmx_path, os.stat(jmx_path).st_mtime_ns)
    except (OSError, ET.ParseError) as error:
        logger.warning('Cannot read sampler routes from %s: %s', jmx_path, error)
        return {}


def _matching_labels(summary: DetailedTestSummary, transaction: str,
                     routes: Optional[dict[str, frozenset[str]]] = None) -> list[str]:
    labels = list((summary.per_label or {}).keys())
    exact = [label for label in labels if label == transaction]
    if exact:
        return exact
    keys = _route_keys(transaction)
    if keys and routes:
        by_route = [label for label in labels if keys & routes.get(label, frozenset())]
        if by_route:
            return by_route
    needle = transaction.lower()
    return [label for label in labels if needle in label.lower()]


def _label_value(stats: dict, metric: str) -> Optional[float]:
    key = {'p90': 'p90_response_time', 'p95': 'p95_response_time', 'p99': 'p99_response_time'}.get(metric, metric)
    value = stats.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _global_value(summary: DetailedTestSummary, metric: str) -> Optional[float]:
    if metric == 'p90':
        value = (summary.percentiles or {}).get('p90')
    else:
        value = getattr(summary, _SUMMARY_FIELDS[metric], None)
    return float(value) if isinstance(value, (int, float)) else None


def _passes(actual: float, operator: str, expected: float) -> bool:
    return {'lte': actual <= expected, 'lt': actual < expected,
            'gte': actual >= expected, 'gt': actual > expected}[operator]


def _worst(values: list[float], operator: str) -> float:
    # Every matching label must satisfy the requirement, so judge the worst one.
    return max(values) if operator in ('lte', 'lt') else min(values)


def evaluate(requirements: Optional[PerformanceRequirements], summary: DetailedTestSummary,
             routes: Optional[dict[str, frozenset[str]]] = None) -> RequirementEvaluation:
    """``routes`` comes from :func:`sampler_routes` for the JMX that produced ``summary``."""
    if requirements is None or not requirements.items:
        return RequirementEvaluation(verdict='none', load_profile=requirements.load_profile if requirements else None)
    verdicts = []
    for item in requirements.items:
        labels: list[str] = []
        actual = None
        if summary.total_requests > 0:
            if item.scope == 'transaction' and item.transaction:
                labels = _matching_labels(summary, item.transaction, routes)
                values = [v for v in (_label_value((summary.per_label or {})[label], item.metric) for label in labels)
                          if v is not None]
                actual = _worst(values, item.operator) if values else None
            else:
                actual = _global_value(summary, item.metric)
        status = 'missing' if actual is None else ('pass' if _passes(actual, item.operator, item.value) else 'fail')
        gap = None
        if status == 'fail' and item.value:
            gap = round(abs(actual - item.value) / abs(item.value) * 100, 1)
        target = f'[{item.transaction}] ' if item.scope == 'transaction' else ''
        verdicts.append(RequirementVerdict(
            id=item.id, description=f'{target}{METRIC_LABELS[item.metric]} {_OPERATOR_TEXT[item.operator]} {item.value:g}',
            transaction=item.transaction, matched_labels=labels, metric=item.metric, operator=item.operator,
            expected=item.value, actual=actual, status=status, gap_pct=gap,
            source=item.source, source_verified=item.source_verified))
    failed = sum(v.status == 'fail' for v in verdicts)
    missing = sum(v.status == 'missing' for v in verdicts)
    return RequirementEvaluation(
        verdict='fail' if failed else ('incomplete' if missing else 'pass'),
        passed=len(verdicts) - failed - missing, failed=failed, missing=missing,
        items=verdicts, load_profile=requirements.load_profile)


def format_for_prompt(evaluation: RequirementEvaluation) -> str:
    """Compact, deterministic block the analysis model must reason from."""
    if evaluation.verdict == 'none':
        return '## 性能需求对照\n本项目尚未录入性能需求，无法判定是否达标；请只做客观分析，不要声称达标或不达标。'
    lines = [f'## 性能需求对照（确定性计算，结论：{evaluation.verdict}，'
             f'通过 {evaluation.passed} / 不通过 {evaluation.failed} / 无法评估 {evaluation.missing}）']
    for v in evaluation.items:
        actual = '无数据' if v.actual is None else f'{v.actual:g}'
        gap = f'，超出 {v.gap_pct}%' if v.gap_pct is not None else ''
        quote = f' 原文：「{v.source}」' if v.source else ''
        lines.append(f'- {v.id} {v.description}：实际 {actual} → {v.status}{gap}.{quote}')
    lines.append('分析时必须逐条引用上述需求编号说明达标情况；不达标项要结合事实表与其它证据给出可能原因。')
    return '\n'.join(lines)
