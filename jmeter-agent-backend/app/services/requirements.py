"""Performance requirements: extracted from test documents, judged against every run.

A project owns one requirement set: global or per-transaction metric thresholds
plus the intended load profile. Each item keeps the exact document clause it
came from, so a verdict can always be traced back to the source text.
"""
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import lru_cache
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, Field, field_validator

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
_OPERATOR_SYMBOLS = {'<=': 'lte', '≤': 'lte', '<': 'lt', '>=': 'gte', '≥': 'gte', '>': 'gt'}
LOAD_TOLERANCE = 0.9  # A run within 10% of the required load counts as having reached it.
# A throughput limit at or barely above the required TPS leaves the requirement no room to pass.
CAP_HEADROOM = 1.05


class RequirementItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    # each: every request on its own ("所有接口 P95 < 500ms"); global would let fast requests hide a slow one.
    scope: Literal['global', 'each', 'transaction'] = 'global'
    transaction: Optional[str] = Field(None, max_length=200, description='Sampler label or a distinctive part of it')
    metric: Metric
    operator: Operator
    value: float
    source: str = Field('', max_length=1000, description='Exact clause quoted from the source document')
    source_verified: bool = Field(False, description='The quoted clause was found in the source text')

    @field_validator('operator', mode='before')
    @classmethod
    def _operator_symbols(cls, value):
        # Models often write the symbol instead of the code.
        return _OPERATOR_SYMBOLS.get(value.strip(), value) if isinstance(value, str) else value

    @field_validator('transaction')
    @classmethod
    def _strip(cls, value):
        return value.strip() or None if isinstance(value, str) else value


class LoadProfile(BaseModel):
    # Unknown keys are dropped, so accept the names models actually send; "concurrent_users"
    # once lost a saved concurrency of 10 without any error.
    concurrency: Optional[int] = Field(None, ge=1, validation_alias=AliasChoices(
        'concurrency', 'concurrent_users', 'users', 'threads'))
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
    {"id": "R1", "scope": "global"、"each" 或 "transaction",
     "transaction": "接口（scope=transaction 时必填，否则为 null）",
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
- scope：说"所有接口 / 每个接口 / 任一接口"的写 each（每个接口各自都要满足）；说"整体 / 平均 / 系统 / 总"的写 global；只说某一个接口的写 transaction。
- 给了"接口清单"时，transaction 必须原样写成清单里的一项（如 "POST /api/orders"），不要写中文名称；对应不上的写进 assumptions。
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


def _endpoint_list(endpoints) -> str:
    lines = [f"- {item['route']}" + (f"（{item['name']}）" if item.get('name') else '') for item in endpoints[:200]]
    return '\n\n## 接口清单（transaction 只能写其中一项的 METHOD /path）\n' + '\n'.join(lines)


async def extract_requirements(document: str, adapter, source_name: str = '',
                               endpoints: Optional[list[dict]] = None) -> PerformanceRequirements:
    """``endpoints`` ({route: "POST /api/orders", name}) lets a requirement name the request a plan has.

    Without it "下单接口 P95 < 800ms" was kept as "下单接口", matched no sampler, and was never judged.
    """
    if not document.strip():
        raise ValueError('Document text is empty')
    response = await adapter.chat([
        AIChatMessage(role='system', content=EXTRACTION_PROMPT),
        AIChatMessage(role='user', content=document[:60000] + (_endpoint_list(endpoints) if endpoints else '')),
    ], temperature=0, json_mode=True)
    data = _json_object(response if isinstance(response, str) else str(response))
    items = []
    for index, raw in enumerate(data.get('items') or [], start=1):
        try:
            raw = {**raw, 'id': str(raw.get('id') or f'R{index}')}
            if raw.get('scope') not in ('transaction', 'each'):
                raw['scope'] = 'global'
            if raw['scope'] != 'transaction':
                raw['transaction'] = None
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
    note: Optional[str] = Field(None, description='Why the item could not be judged, e.g. plan_caps_throughput:50')


class LoadGap(BaseModel):
    field: Literal['concurrency', 'duration_seconds', 'target_tps']
    expected: float
    actual: float


class RequirementEvaluation(BaseModel):
    verdict: Literal['pass', 'fail', 'incomplete', 'none']
    passed: int = 0
    failed: int = 0
    missing: int = 0
    items: list[RequirementVerdict] = Field(default_factory=list)
    load_profile: Optional[LoadProfile] = None
    load_gaps: list[LoadGap] = Field(default_factory=list, description='Where the run fell short of the required load')


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


def _plan_bytes(jmx_path: str) -> bytes:
    # Cache by content, not mtime: a plan rewritten within one clock tick keeps its mtime.
    with open(jmx_path, 'rb') as handle:
        return handle.read()


@lru_cache(maxsize=64)
def _sampler_routes_cached(content: bytes) -> dict[str, frozenset[str]]:
    routes: dict[str, set[str]] = {}
    for element in ET.fromstring(content).iter():
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
        return _sampler_routes_cached(_plan_bytes(jmx_path))
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
        # A documented route has templates ("GET /api/products/{id}") where the sampler has a value.
        templated = [re.compile(re.sub(r'\\\{[^/]*?\\\}', '[^/]+', re.escape(key)) + '$') for key in keys if '{' in key]
        if templated and not by_route:
            by_route = [label for label in labels
                        if any(pattern.match(route) for pattern in templated for route in routes.get(label, ()))]
        if by_route:
            return by_route
    needle = transaction.lower()
    return [label for label in labels if needle in label.lower()]


def _label_value(stats: dict, metric: str) -> Optional[float]:
    key = {'p90': 'p90_response_time', 'p95': 'p95_response_time', 'p99': 'p99_response_time'}.get(metric, metric)
    # Fast failures must not satisfy a latency or throughput limit. Reports parsed before
    # success stats existed have only the combined numbers.
    source = stats if metric == 'error_rate' else stats.get('success') or stats
    value = source.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _global_value(summary: DetailedTestSummary, metric: str) -> Optional[float]:
    if metric != 'error_rate' and summary.success:
        return _label_value({'success': summary.success}, metric)
    if metric == 'p90':
        value = (summary.percentiles or {}).get('p90')
    else:
        value = getattr(summary, _SUMMARY_FIELDS[metric], None)
    return float(value) if isinstance(value, (int, float)) else None


def _passes(actual: float, operator: str, expected: float) -> bool:
    return {'lte': actual <= expected, 'lt': actual < expected,
            'gte': actual >= expected, 'gt': actual > expected}[operator]


def _load_gaps(profile: Optional[LoadProfile], summary: DetailedTestSummary) -> list[LoadGap]:
    """A pass only counts at the load the requirements were written for."""
    load = (summary.diagnostics or {}).get('load') or {}
    if profile is None or not load:
        return []
    measured = {'concurrency': load.get('peak_threads'), 'duration_seconds': load.get('duration_seconds'),
                'target_tps': _sustained_throughput(summary)}
    gaps = []
    for name, actual in measured.items():
        expected = getattr(profile, name)
        if expected and actual is not None and actual < expected * LOAD_TOLERANCE:
            gaps.append(LoadGap(field=name, expected=expected, actual=actual))
    return gaps


def _worst(values: list[float], operator: str) -> float:
    # Every matching label must satisfy the requirement, so judge the worst one.
    return max(values) if operator in ('lte', 'lt') else min(values)


def _sustained_throughput(summary: DetailedTestSummary) -> Optional[float]:
    """Successful requests per second once the planned users are all running.

    Over the whole run the ramp-up counts too: a run that held 49 TPS at its load averaged 47.
    """
    steady = (summary.diagnostics or {}).get('steady') or {}
    window = ((summary.diagnostics or {}).get('load') or {}).get('steady_window') or {}
    if isinstance(steady.get('throughput'), (int, float)) and window.get('seconds'):
        return float(steady['throughput'])
    return _global_value(summary, 'throughput')


def throughput_cap(jmx_path: Optional[str]) -> Optional[float]:
    """Requests per second the plan's throughput timers allow in total, or None when nothing limits them.

    A Constant Throughput Timer set to the required TPS can only ever reach it, never pass it.
    """
    if not jmx_path:
        return None
    try:
        return _throughput_cap_cached(_plan_bytes(jmx_path))
    except OSError:
        return None


def evaluate_run(requirements: Optional['PerformanceRequirements'], summary: DetailedTestSummary,
                 jmx_path: Optional[str]) -> 'RequirementEvaluation':
    """Judge a run against the requirements, reading what the plan that ran allows."""
    return evaluate(requirements, summary, sampler_routes(jmx_path), throughput_cap(jmx_path))


@lru_cache(maxsize=64)
def _throughput_cap_cached(content: bytes) -> Optional[float]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    threads = 0
    for group in root.iter():
        if group.tag.endswith('ThreadGroup') and group.get('enabled') != 'false':
            value = next((p.text for p in group if p.get('name') == 'ThreadGroup.num_threads'), None)
            threads += int(value) if value and value.strip().isdigit() else 0
    caps = []
    for timer in root.iter('ConstantThroughputTimer'):
        if timer.get('enabled') == 'false':
            continue
        per_minute = next((float(p.findtext('value') or p.text or 0) for p in timer
                           if (p.get('name') or p.findtext('name')) == 'throughput'), None)
        mode = next((p.text for p in timer if p.get('name') == 'calcMode'), '0')
        if not per_minute:
            continue
        # Mode 0 paces each thread on its own; the others share the rate among the active threads.
        caps.append(per_minute / 60 * (threads if mode.strip() == '0' and threads else 1))
    for timer in root.iter('PreciseThroughputTimer'):
        if timer.get('enabled') == 'false':
            continue
        values = {(p.get('name') or ''): (p.text or '') for p in timer}
        try:
            caps.append(float(values.get('throughput', 0)) / max(float(values.get('throughputPeriod', 3600)), 1))
        except ValueError:
            pass
    return min(caps) if caps else None


def evaluate(requirements: Optional[PerformanceRequirements], summary: DetailedTestSummary,
             routes: Optional[dict[str, frozenset[str]]] = None,
             plan_cap: Optional[float] = None) -> RequirementEvaluation:
    """``routes`` comes from :func:`sampler_routes` for the JMX that produced ``summary``, and
    ``plan_cap`` from :func:`throughput_cap` for it."""
    if requirements is None or not requirements.items:
        return RequirementEvaluation(verdict='none', load_profile=requirements.load_profile if requirements else None)
    verdicts = []
    for item in requirements.items:
        labels: list[str] = []
        actual = None
        note = None
        if summary.total_requests > 0:
            if item.scope in ('transaction', 'each') and (item.transaction or item.scope == 'each'):
                per_label = summary.per_label or {}
                candidates = list(per_label) if item.scope == 'each' else _matching_labels(summary, item.transaction, routes)
                values = {label: v for label in candidates
                          if (v := _label_value(per_label[label], item.metric)) is not None}
                actual = _worst(list(values.values()), item.operator) if values else None
                labels = list(values) if item.scope == 'transaction' else \
                    [label for label, v in values.items() if not _passes(v, item.operator, item.value)]
            elif item.metric == 'throughput':
                actual = _sustained_throughput(summary)
            else:
                actual = _global_value(summary, item.metric)
        status = 'missing' if actual is None else ('pass' if _passes(actual, item.operator, item.value) else 'fail')
        if status == 'fail' and item.metric == 'throughput' and item.operator in ('gte', 'gt') \
                and plan_cap is not None and plan_cap <= item.value * CAP_HEADROOM:
            # The plan never sends more than this, so a shortfall says nothing about the system.
            status = 'missing'
            note = f'plan_caps_throughput:{plan_cap:g}'
        gap = None
        if status == 'fail' and item.value:
            gap = round(abs(actual - item.value) / abs(item.value) * 100, 1)
        target = f'[{item.transaction}] ' if item.scope == 'transaction' else ('[每个接口] ' if item.scope == 'each' else '')
        verdicts.append(RequirementVerdict(
            id=item.id, description=f'{target}{METRIC_LABELS[item.metric]} {_OPERATOR_TEXT[item.operator]} {item.value:g}',
            transaction=item.transaction, matched_labels=labels, metric=item.metric, operator=item.operator,
            expected=item.value, actual=actual, status=status, gap_pct=gap, note=note,
            source=item.source, source_verified=item.source_verified))
    failed = sum(v.status == 'fail' for v in verdicts)
    missing = sum(v.status == 'missing' for v in verdicts)
    gaps = _load_gaps(requirements.load_profile, summary)
    return RequirementEvaluation(
        verdict='fail' if failed else ('incomplete' if missing or gaps else 'pass'),
        passed=len(verdicts) - failed - missing, failed=failed, missing=missing,
        items=verdicts, load_profile=requirements.load_profile, load_gaps=gaps)


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
        where = f'（不达标的接口：{", ".join(v.matched_labels[:8])}）' if v.status == 'fail' and v.matched_labels \
            and not v.transaction else ''
        lines.append(f'- {v.id} {v.description}：实际 {actual} → {v.status}{gap}{where}.{quote}')
        if v.note and v.note.startswith('plan_caps_throughput:'):
            cap = v.note.split(':', 1)[1]
            lines.append(f'  计划里的吞吐定时器把总吞吐限制在约 {cap} 请求/秒，不高于这条要求，所以它在这个计划下不可能通过：'
                         '这次没达到是计划限速造成的，不能据此判断服务端的处理能力。')
    names = {'concurrency': '并发', 'duration_seconds': '时长(秒)', 'target_tps': '成功 TPS'}
    for item in evaluation.load_gaps:
        lines.append(f'- 负载未达到需求：{names[item.field]} 要求 {item.expected:g}，实际 {item.actual:g}。'
                     '在这个负载下的达标不能算满足需求。')
    lines.append('时延和吞吐只按成功样本判定，全部失败时记为 missing；吞吐取并发达到计划峰值后的稳态窗口，不含爬坡。'
                 '[每个接口] 的要求按最差的接口判定。')
    lines.append('分析时必须逐条引用上述需求编号说明达标情况；不达标项要结合事实表与其它证据给出可能原因。')
    return '\n'.join(lines)
