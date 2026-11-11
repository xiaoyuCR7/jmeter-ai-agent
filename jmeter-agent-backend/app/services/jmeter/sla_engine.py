"""SLAEngine — automated pass/fail evaluation against defined thresholds.

Enables CI/CD integration by providing a deterministic yes/no verdict
instead of relying solely on LLM-generated natural-language analysis.
"""

from dataclasses import dataclass, field
from typing import Dict, List
from enum import Enum
import math

from app.models.schemas import DetailedTestSummary


class SLAVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"  # borderline — within tolerance but close to threshold


@dataclass
class SLAThreshold:
    """A single SLA threshold rule."""
    metric: str                # avg_response_time / p95 / p99 / error_rate / throughput
    operator: str              # lt / gt / lte / gte
    value: float
    label: str = ""            # human-readable name, e.g. "/api/users"
    severity: str = "error"    # error → fail the test; warn → warn only

    def validation_error(self, actual) -> str:
        if self.operator not in {"lt", "gt", "lte", "gte"}:
            return f"Unknown SLA operator: {self.operator}"
        try:
            if not math.isfinite(float(actual)) or not math.isfinite(float(self.value)):
                return f"Non-finite SLA value for {self.metric}"
        except (ValueError, TypeError, OverflowError):
            return f"Invalid numeric SLA value for {self.metric}"
        return ""

    def evaluate(self, actual: float) -> tuple[bool, str]:
        """Return (violated, message)."""
        invalid = self.validation_error(actual)
        if invalid:
            return True, invalid
        actual, limit = float(actual), float(self.value)
        if self.operator == "lt":
            violated = actual >= limit
        elif self.operator == "gt":
            violated = actual <= limit
        elif self.operator == "lte":
            violated = actual > limit
        else:
            violated = actual < limit

        if violated:
            msg = (f"{self.label or self.metric}: actual {actual:.2f} "
                   f"{self.operator} threshold {limit:.2f}")
        else:
            msg = ""
        return violated, msg


@dataclass
class SLAConfig:
    """SLA/Pass-Fail configuration for a test plan."""
    thresholds: List[SLAThreshold] = field(default_factory=list)
    description: str = ""

    @classmethod
    def standard_api_sla(cls) -> "SLAConfig":
        """Factory: reasonable defaults for API performance testing."""
        return cls(
            description="Standard API SLA",
            thresholds=[
                SLAThreshold("avg_response_time", "lt", 500, "Average Response Time"),
                SLAThreshold("p95", "lt", 1000, "P95 Response Time"),
                SLAThreshold("p99", "lt", 2000, "P99 Response Time"),
                SLAThreshold("error_rate", "lt", 1.0, "Error Rate (<1%)"),
            ],
        )

    @classmethod
    def strict_api_sla(cls) -> "SLAConfig":
        """Factory: strict thresholds for critical APIs."""
        return cls(
            description="Strict API SLA",
            thresholds=[
                SLAThreshold("avg_response_time", "lt", 200, "Average Response Time"),
                SLAThreshold("p95", "lt", 500, "P95 Response Time"),
                SLAThreshold("p99", "lt", 1000, "P99 Response Time"),
                SLAThreshold("error_rate", "lt", 0.5, "Error Rate (<0.5%)"),
            ],
        )


@dataclass
class SLAResult:
    """Result of SLA evaluation against a test report."""
    verdict: SLAVerdict
    total_checks: int
    passed_checks: int
    failed_checks: int
    warning_checks: int
    violations: List[str] = field(default_factory=list)
    per_label_results: Dict[str, dict] = field(default_factory=dict)
    summary: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == SLAVerdict.PASS


class SLAEngine:
    """Evaluates test results against defined SLA thresholds.

    Usage:
        engine = SLAEngine()
        config = SLAConfig.standard_api_sla()
        result = engine.evaluate(test_summary, config)
        if not result.passed:
            print(f"SLA failed: {result.violations}")
    """

    # Map SLA metric names to DetailedTestSummary field names
    METRIC_MAP = {
        "avg_response_time": "avg_response_time",
        "p95": "p95_response_time",
        "p99": "p99_response_time",
        "error_rate": "error_rate",
        "throughput": "throughput",
    }

    def evaluate(
        self, summary: DetailedTestSummary, config: SLAConfig
    ) -> SLAResult:
        """Evaluate a test summary against SLA thresholds.

        Args:
            summary: Parsed test report (DetailedTestSummary).
            config: SLA threshold configuration.

        Returns:
            SLAResult with verdict, violations, and per-label breakdown.
        """
        violations: list[str] = []
        warnings: list[str] = []
        total = len(config.thresholds)
        passed = 0
        failed = 0
        warned = 0
        per_label: Dict[str, dict] = {}

        if summary.total_requests <= 0:
            return SLAResult(
                verdict=SLAVerdict.FAIL, total_checks=max(total, 1),
                passed_checks=0, failed_checks=max(total, 1), warning_checks=0,
                violations=["No samples available; SLA cannot be evaluated"],
                summary="SLA FAIL: no samples available",
            )

        for threshold in config.thresholds:
            # Get the metric value from the summary
            metric_key = self.METRIC_MAP.get(threshold.metric, threshold.metric)
            actual = getattr(summary, metric_key, None)

            if actual is None:
                # Try per-label breakdown if available
                per_label_data = getattr(summary, "per_label", None) or {}
                found = False
                for label, label_stats in per_label_data.items():
                    actual = label_stats.get(metric_key)
                    if actual is not None:
                        found = True
                        violated, msg = threshold.evaluate(actual)
                        if violated:
                            if threshold.severity == "warn" and not threshold.validation_error(actual):
                                warnings.append(f"[{label}] {msg}")
                                warned += 1
                            else:
                                violations.append(f"[{label}] {msg}")
                                failed += 1
                        else:
                            passed += 1
                        per_label[label] = per_label.get(label, {})
                        per_label[label][threshold.metric] = {
                            "actual": actual, "threshold": threshold.value,
                            "operator": threshold.operator, "passed": not violated,
                        }
                if not found:
                    violations.append(f"Unknown or unavailable SLA metric: {threshold.metric}")
                    failed += 1
                continue

            # Global metric
            violated, msg = threshold.evaluate(actual)
            if violated:
                if threshold.severity == "warn" and not threshold.validation_error(actual):
                    warnings.append(msg)
                    warned += 1
                else:
                    violations.append(msg)
                    failed += 1
            else:
                passed += 1

        # Determine verdict
        if failed > 0:
            verdict = SLAVerdict.FAIL
        elif warned > 0:
            verdict = SLAVerdict.WARN
        else:
            verdict = SLAVerdict.PASS

        summary_text = (
            f"SLA {verdict.value.upper()}: {passed}/{total} checks passed"
            + (f", {failed} failed" if failed else "")
            + (f", {warned} warnings" if warned else "")
        )

        return SLAResult(
            verdict=verdict,
            total_checks=total,
            passed_checks=passed,
            failed_checks=failed,
            warning_checks=warned,
            violations=violations + warnings,
            per_label_results=per_label,
            summary=summary_text,
        )

    def to_dict(self, result: SLAResult) -> dict:
        """Serialize an SLAResult for JSON responses."""
        return {
            "verdict": result.verdict.value,
            "passed": result.passed,
            "total_checks": result.total_checks,
            "passed_checks": result.passed_checks,
            "failed_checks": result.failed_checks,
            "warning_checks": result.warning_checks,
            "violations": result.violations,
            "per_label_results": result.per_label_results,
            "summary": result.summary,
        }
