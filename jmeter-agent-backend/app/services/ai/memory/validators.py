"""Validators — extract numeric claims from LLM output and verify against ground-truth facts.

The rule is simple: LLM is allowed to make numeric statements, but every statement
must be re-derivable from the FactSheet. If it cannot be re-derived, it is rejected.
"""

import re
import logging
from typing import Iterable, Optional
from dataclasses import dataclass, field

from app.models.schemas import FactSheet, MetricClaim, ValidationResult

logger = logging.getLogger(__name__)


# Tolerance for floating point comparison
DEFAULT_TOLERANCE = 0.05  # 5% relative or absolute
ABSOLUTE_TOLERANCE_MS = 50  # Most a rounded response time may be off by ("1.2 s" for 1234 ms)...
MIN_ABSOLUTE_MS = 5  # ...shrinking with the value, so 55 ms never passes for a 10 ms request.
RESPONSE_TIME_METRICS = ("avg_response_time", "max_response_time", "p99", "p95", "p90", "p50")


# Only these may stand between a metric and its value. "P99 恶化了 30%" or "P95、P99 分别为" are
# not statements of the metric's value, and an open gap would read them as if they were.
_GAP = (r"(?:\s|[:：=≈~（(]|响应时间|耗时|延迟|实测|实际|整体|总体|约为|大约|大概|约|为|是|达到|高达|达|仅"
        r"|response\s+times?|latency|overall|measured|reached|around|about|approximately|roughly|only"
        r"|of|is|was|were|at)*")
# A number followed by a range mark is a time span ("错误率 5-10 s 为 72%"), not the metric's value.
_NOT_VALUE = r"(?!\s*(?:[%％倍\-–—~至到]|\d|\.\d|x\b|times))"
_TIME = _GAP + r"(\d+(?:\.\d+)?)\s*(ms|毫秒|seconds?\b|s\b|秒)?" + _NOT_VALUE
_RATE = _GAP + r"(\d+(?:\.\d+)?)\s*(req/s|requests?/s(?:ec)?|rps|tps|请求/秒)?" + _NOT_VALUE
_PERCENT = _GAP + r"(\d+(?:\.\d+)?)\s*(%|％|percent)?(?!\s*(?:[倍\-–—~至到]|\d|\.\d|x\b|times))"

# A clause that names another run or a target may quote its numbers.
_COMPARISON = re.compile(
    r"基线|上次|上一次|之前|此前|历史|以前|前一次|对比|相比|较|比|从|由|需求|目标|阈值|要求|限值|上限|预期|SLA"
    r"|baseline|previous|prior|earlier|last\s+run|history|compared|than|from|versus|vs\.?|target|requirement"
    r"|threshold|limit|expected|goal", re.IGNORECASE)
_OVERALL = re.compile(r"整体|总体|全部请求|所有请求|全局|overall|in\s+total|all\s+requests", re.IGNORECASE)
_CLAUSE_END = re.compile(r"[。；;！？!?\n]|\.(?=\s|$)|，(?=\s*(?:而|但|其中|整体|总体))")


@dataclass
class _Span:
    claim: MetricClaim
    start: int
    end: int


class MetricClaimExtractor:
    """Extract numeric metric claims from analysis text, in Chinese or English."""

    # metric_name_pattern -> (canonical_name, unit)
    METRIC_PATTERNS = [
        (r"(?<![a-z])p99" + _TIME, "p99", "ms"),
        (r"(?<![a-z])p95" + _TIME, "p95", "ms"),
        (r"(?<![a-z])p90" + _TIME, "p90", "ms"),
        (r"(?<![a-z])p50" + _TIME, "p50", "ms"),
        (r"(?<![a-z])median" + _TIME, "p50", "ms"),
        (r"平均(?:响应时间|延迟|耗时)" + _TIME, "avg_response_time", "ms"),
        (r"(?<![a-z])(?:average|avg|mean)\s+(?:response\s+times?|latency)" + _TIME, "avg_response_time", "ms"),
        (r"最大(?:响应时间|延迟|耗时)" + _TIME, "max_response_time", "ms"),
        (r"(?<![a-z])(?:max|maximum|peak)\s+(?:response\s+times?|latency)" + _TIME, "max_response_time", "ms"),
        (r"吞吐量?" + _RATE, "throughput", "req/s"),
        (r"(?<![a-z])throughput" + _RATE, "throughput", "req/s"),
        (r"(?<![a-z])TPS" + _RATE, "throughput", "req/s"),
        (r"(?<![a-z])RPS" + _RATE, "throughput", "req/s"),
        (r"错误率" + _PERCENT, "error_rate", "%"),
        (r"(?<![a-z])(?:error|failure)\s+rate" + _PERCENT, "error_rate", "%"),
        (r"总请求数?" + _GAP + r"(\d+)(?![\d.%％倍])", "total_requests", ""),
        (r"(?<![a-z])total\s+(?:of\s+)?requests" + _GAP + r"(\d+)(?![\d.%％倍])", "total_requests", ""),
    ]

    def extract(self, text: str) -> list[MetricClaim]:
        """Extract all metric claims from analysis text."""
        return [span.claim for span in self.spans(text)]

    def spans(self, text: str) -> list[_Span]:
        """The claims with where they stand, so the validator can read the clause around each."""
        spans = []
        for pattern, canonical_name, default_unit in self.METRIC_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                value_str = match.group(1)
                unit_group = match.group(2) if match.lastindex and match.lastindex >= 2 else None
                try:
                    value = float(value_str)
                except ValueError:
                    continue

                unit = default_unit
                if unit_group:
                    unit = self._normalize_unit(unit_group)

                # Convert seconds to milliseconds if needed
                if unit == "s" and canonical_name in RESPONSE_TIME_METRICS:
                    value *= 1000
                    unit = "ms"

                spans.append(_Span(MetricClaim(
                    metric_name=canonical_name,
                    claimed_value=value,
                    unit=unit,
                    context=text[max(0, match.start() - 20):match.end() + 20],
                ), match.start(), match.end()))
        return sorted(spans, key=lambda span: span.start)

    @staticmethod
    def _normalize_unit(unit_str: str) -> str:
        u = unit_str.lower().strip()
        if u in ("ms", "毫秒"):
            return "ms"
        if u in ("s", "秒", "second", "seconds"):
            return "s"
        if u in ("req/s", "rps", "tps", "请求/秒") or u.startswith("request"):
            return "req/s"
        if u in ("%", "％", "percent"):
            return "%"
        return u


_SCOPE_KEYS = {"p99": "p99_response_time", "p95": "p95_response_time", "p90": "p90_response_time",
               "p50": "p50_response_time", "total_requests": "count"}


class FactValidator:
    """Validate LLM analysis claims against a deterministic FactSheet.

    Each claim is read with its clause. A clause that names a request is checked against that
    request only; one that says "overall" against the whole run only; one that names a baseline,
    an earlier run or a target may also quote ``extra_values``. Anything else may match any scope
    the run measured, and is counted as unbound.
    """

    def __init__(self, tolerance: float = DEFAULT_TOLERANCE, absolute_ms: float = ABSOLUTE_TOLERANCE_MS):
        self.tolerance = tolerance
        self.absolute_ms = absolute_ms
        self.extractor = MetricClaimExtractor()

    @staticmethod
    def _get_fact_value(fact: FactSheet, metric_name: str) -> Optional[float]:
        """Resolve a canonical metric name to its ground-truth value."""
        if metric_name == "total_requests":
            return float(fact.total_requests)
        if metric_name == "avg_response_time":
            return fact.avg_response_time
        if metric_name == "max_response_time":
            return fact.max_response_time
        if metric_name == "error_rate":
            return fact.error_rate
        if metric_name == "throughput":
            return fact.throughput
        if metric_name in fact.percentiles:
            return fact.percentiles[metric_name]
        return None

    @staticmethod
    def _values(scopes: list, metric_name: str) -> list[float]:
        key = _SCOPE_KEYS.get(metric_name, metric_name)
        return [float(scope[key]) for scope in scopes
                if isinstance(scope, dict) and isinstance(scope.get(key), (int, float))]

    @classmethod
    def _label_values(cls, fact: FactSheet, label: str, metric_name: str) -> list[float]:
        stats = (fact.per_label or {}).get(label) or {}
        scopes = [stats, stats.get("success") or {}]
        if fact.raw_summary is not None:
            steady = (fact.raw_summary.diagnostics or {}).get("steady") or {}
            scopes.append((steady.get("labels") or {}).get(label) or {})
        return cls._values(scopes, metric_name)

    @classmethod
    def _overall_values(cls, fact: FactSheet, metric_name: str) -> list[float]:
        """The whole run at other cuts: successes only, the steady window, the timing phases of a request."""
        summary = fact.raw_summary
        values = []
        if summary is not None:
            steady = (summary.diagnostics or {}).get("steady") or {}
            values += cls._values([summary.success or {}, steady], metric_name)
            if metric_name == "avg_response_time":
                # "新建连接平均耗时 933ms" is an average too, of one phase of the request.
                timing = (summary.diagnostics or {}).get("timing") or {}
                values += [float(value) for value in timing.values() if isinstance(value, (int, float))]
        return values

    @classmethod
    def _also_measured(cls, fact: FactSheet, metric_name: str) -> list[float]:
        """The same metric at every scope: per transaction, per time segment, steady window, successes only."""
        values = [value for label in (fact.per_label or {}) for value in cls._label_values(fact, label, metric_name)]
        values += cls._values([segment.model_dump() | {"count": segment.request_count}
                               for segment in fact.timeline_segments], metric_name)
        return values + cls._overall_values(fact, metric_name)

    @staticmethod
    def _matches(claimed: float, actual: float, metric_name: str, absolute_ms: float, tolerance: float) -> bool:
        """Compare claimed vs actual with domain-aware tolerance."""
        diff = abs(claimed - actual)
        if metric_name in RESPONSE_TIME_METRICS:
            # Rounding to a coarser unit is fine; the allowance shrinks with the value.
            if diff <= min(absolute_ms, max(MIN_ABSOLUTE_MS, abs(actual) * 0.1)):
                return True
        # Relative / percentage tolerance for others
        if actual == 0:
            return diff <= tolerance
        return diff / abs(actual) <= tolerance

    @staticmethod
    def _clause(text: str, span: _Span) -> tuple[str, str]:
        """The clause up to the claim, and the whole clause it stands in."""
        starts = [m.end() for m in _CLAUSE_END.finditer(text, 0, span.start)]
        begin = starts[-1] if starts else 0
        tail = _CLAUSE_END.search(text, span.end)
        return text[begin:span.start], text[begin:tail.start() if tail else len(text)]

    @staticmethod
    def _named_label(fact: FactSheet, before: str) -> Optional[str]:
        """The request named last before the claim in its clause; of two at one place, the longer."""
        best, best_at = None, -1
        lowered = before.lower()
        for label in fact.per_label or {}:
            if len(label) < 2:
                continue
            at = lowered.rfind(label.lower())
            if at > best_at or (at == best_at >= 0 and len(label) > len(best or '')):
                best, best_at = label, at
        return best if best_at >= 0 else None

    def validate(self, analysis_text: str, fact: FactSheet,
                 extra_values: Iterable[float] = ()) -> ValidationResult:
        """Validate all numeric claims in analysis_text against fact.

        ``extra_values`` are measurements of other runs or requirement limits the text may quote.
        """
        extra_values = list(extra_values)
        spans = self.extractor.spans(analysis_text)
        mismatches = []
        missing_fields = []
        unbound = 0

        for span in spans:
            claim = span.claim
            actual = self._get_fact_value(fact, claim.metric_name)
            if actual is None:
                missing_fields.append(claim.metric_name)
                continue

            before, clause = self._clause(analysis_text, span)
            label = self._named_label(fact, before)
            if label is not None:
                scope, known = label, self._label_values(fact, label, claim.metric_name)
                if not known:  # The run did not measure this metric for the request; nothing to check.
                    continue
            elif _OVERALL.search(before):
                scope, known = "overall", [actual, *self._overall_values(fact, claim.metric_name)]
            else:
                unbound += 1
                scope, known = "", [actual, *self._also_measured(fact, claim.metric_name)]
            if _COMPARISON.search(clause):
                known += extra_values
            if not any(self._matches(
                claimed=claim.claimed_value,
                actual=value,
                metric_name=claim.metric_name,
                absolute_ms=self.absolute_ms,
                tolerance=self.tolerance,
            ) for value in known):
                mismatches.append({
                    "metric": claim.metric_name,
                    "claimed": claim.claimed_value,
                    "actual": known[0],
                    "unit": claim.unit or "",
                    "scope": scope,
                    "context": claim.context,
                })

        is_valid = not mismatches and not missing_fields

        corrected_summary = None
        if not is_valid:
            lines = ["以下数字声明与原始 JTL 计算结果不符："]
            for m in mismatches:
                where = f"（{m['scope']}）" if m["scope"] else ""
                lines.append(f"- {m['metric']}{where}: LLM 声明 {m['claimed']}{m['unit']}，实际 {m['actual']}{m['unit']}")
            corrected_summary = "\n".join(lines)

        return ValidationResult(
            is_valid=is_valid,
            claims=[span.claim for span in spans],
            mismatches=mismatches,
            missing_fields=list(set(missing_fields)),
            unbound_claims=unbound,
            corrected_summary=corrected_summary,
        )


def correction_request(mismatches: list[dict], output_shape: str, lang: str = 'zh-CN') -> str:
    """Ask the model to fix the numbers the validator could not find in the facts, once."""
    lines = [f"- {m['metric']} 写成了 {m['claimed']}{m.get('unit', '')}"
             f"（{m.get('scope') or '整体'}实测 {m['actual']}{m.get('unit', '')}），"
             f"原文：「{m.get('context', '').strip()}」" for m in mismatches]
    return ("程序核对了你写的数字，下面这些在事实表和证据里都找不到对应的值：\n" + "\n".join(lines) +
            "\n\n逐个改正：换成事实表或证据里对应范围的值；找不到依据的就删掉这个数字。"
            f"其它内容保持不变。{output_shape}"
            + ("Keep writing in English." if lang == "en" else ""))


class HypothesisExtractor:
    """Extract structured hypotheses from LLM analysis text.

    In the future this can be replaced by a structured LLM call (response_format=json).
    For now, use lightweight regex/markers to avoid introducing extra LLM latency.
    """

    def extract(self, analysis_text: str, execution_id: str, module: str, version: Optional[str] = None) -> list:
        """Extract hypothesis objects from text.

        Returns a list of dicts that can be passed to HypothesisLedger.add().
        """
        from app.models.schemas import Hypothesis, HypothesisCategory, HypothesisStatus
        hypotheses = []

        # Look for explicit hypothesis markers
        # Pattern: 【假设】... 或 "假设：..." 或 "初步判断：..."
        marker_patterns = [
            r"【假设】[:：]?\s*(.+?)(?=\n\n|\n【|\Z)",
            r"假设[:：]\s*(.+?)(?=\n\n|\n\d+\.|[##]|\Z)",
            r"初步判断[:：]\s*(.+?)(?=\n\n|\n\d+\.|[##]|\Z)",
            r"瓶颈[:：]\s*(.+?)(?=\n\n|\n\d+\.|[##]|\Z)",
        ]

        seen = set()
        for pattern in marker_patterns:
            for match in re.finditer(pattern, analysis_text, re.DOTALL | re.IGNORECASE):
                statement = match.group(1).strip()
                if len(statement) < 10 or statement in seen:
                    continue
                seen.add(statement)

                category = HypothesisCategory.BOTTLENECK
                if any(k in statement for k in ("建议", "优化", "推荐", "应该", "可以")):
                    category = HypothesisCategory.RECOMMENDATION
                elif any(k in statement for k in ("根因", "原因", "由于", "因为")):
                    category = HypothesisCategory.ROOT_CAUSE

                hypotheses.append(Hypothesis(
                    hypothesis_id="",
                    source_execution_id=execution_id,
                    module=module,
                    version=version or "unknown",
                    category=category,
                    statement=statement,
                    confidence=0.5,
                    status=HypothesisStatus.UNTESTED,
                ))

        return hypotheses


# Module singletons
claim_extractor = MetricClaimExtractor()
fact_validator = FactValidator()
hypothesis_extractor = HypothesisExtractor()
