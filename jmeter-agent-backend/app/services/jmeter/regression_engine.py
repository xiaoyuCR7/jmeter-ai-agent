"""RegressionEngine — compare two test runs and detect regressions.

Produces a structured delta report: what changed, by how much,
and whether it constitutes a regression worth investigating.
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
import math

from app.models.schemas import DetailedTestSummary


class DeltaDirection(str, Enum):
    BETTER = "better"
    WORSE = "worse"
    STABLE = "stable"


@dataclass
class MetricDelta:
    """Change in a single metric between two runs."""
    metric: str
    baseline_value: float
    target_value: float
    delta_absolute: float
    delta_percent: Optional[float]
    direction: DeltaDirection
    is_regression: bool = False
    regression_threshold_pct: float = 20.0  # >20% worse = regression

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "baseline": round(self.baseline_value, 2),
            "target": round(self.target_value, 2),
            "delta_abs": round(self.delta_absolute, 2),
            "delta_pct": round(self.delta_percent, 1) if self.delta_percent is not None else None,
            "direction": self.direction.value,
            "is_regression": self.is_regression,
        }


@dataclass
class RegressionReport:
    """Full regression comparison report."""
    baseline_name: str
    target_name: str
    deltas: list[MetricDelta] = field(default_factory=list)
    regressions: list[MetricDelta] = field(default_factory=list)
    improvements: list[MetricDelta] = field(default_factory=list)
    summary: str = ""
    overall_verdict: str = "stable"  # stable / regression / improvement / mixed

    def to_dict(self) -> dict:
        return {
            "baseline": self.baseline_name,
            "target": self.target_name,
            "deltas": [d.to_dict() for d in self.deltas],
            "regressions": [d.to_dict() for d in self.regressions],
            "improvements": [d.to_dict() for d in self.improvements],
            "regression_count": len(self.regressions),
            "improvement_count": len(self.improvements),
            "summary": self.summary,
            "verdict": self.overall_verdict,
        }


class RegressionEngine:
    """Compare two test runs and flag regressions.

    Usage:
        engine = RegressionEngine()
        report = engine.compare(
            baseline=baseline_summary,
            target=current_summary,
            baseline_name="v1.0",
            target_name="v1.1",
        )
    """

    # Metrics to compare and their "worse" direction. A single sample sets min and max,
    # so they are shown for context and can never make a run a regression.
    METRICS = [
        ("avg_response_time", "higher_is_worse"),
        ("p95_response_time", "higher_is_worse"),
        ("p99_response_time", "higher_is_worse"),
        ("max_response_time", "informational"),
        ("min_response_time", "informational"),
        ("error_rate", "higher_is_worse"),
        ("throughput", "lower_is_worse"),
    ]
    # 0.1% -> 0.2% doubles the rate without meaning anything; errors need to move in points.
    ERROR_RATE_MIN_POINTS = 1.0

    def __init__(self, regression_threshold_pct: float = 20.0):
        """
        Args:
            regression_threshold_pct: Percentage change to flag as regression.
                e.g. 20 means a 20%+ increase in response time is a regression.
        """
        self.regression_threshold_pct = regression_threshold_pct

    def compare(
        self,
        baseline: DetailedTestSummary,
        target: DetailedTestSummary,
        baseline_name: str = "baseline",
        target_name: str = "current",
    ) -> RegressionReport:
        """Compare two test summaries and produce a regression report."""
        deltas: list[MetricDelta] = []
        regressions: list[MetricDelta] = []
        improvements: list[MetricDelta] = []
        comparable_count = 0
        unavailable = False

        for metric_name, direction_rule in self.METRICS:
            base_val = getattr(baseline, metric_name, None)
            tgt_val = getattr(target, metric_name, None)

            if base_val is None or tgt_val is None or isinstance(base_val, bool) or isinstance(tgt_val, bool):
                unavailable = True
                continue
            try:
                base_val, tgt_val = float(base_val), float(tgt_val)
            except (TypeError, ValueError, OverflowError):
                unavailable = True
                continue
            if not all(math.isfinite(v) and v >= 0 for v in (base_val, tgt_val)):
                unavailable = True
                continue
            comparable_count += 1
            if base_val == 0 and tgt_val == 0:
                continue

            base_val = float(base_val)
            tgt_val = float(tgt_val)
            delta_abs = tgt_val - base_val
            delta_pct = (delta_abs / base_val * 100) if base_val else None
            if delta_pct is not None and not math.isfinite(delta_pct):
                delta_pct = None

            # Determine direction
            if direction_rule == "higher_is_worse":
                if tgt_val > base_val:
                    direction = DeltaDirection.WORSE
                    is_regression = delta_pct is None or delta_pct > self.regression_threshold_pct
                    if metric_name == "error_rate":
                        is_regression = is_regression and delta_abs >= self.ERROR_RATE_MIN_POINTS
                elif tgt_val < base_val:
                    direction = DeltaDirection.BETTER
                    is_regression = False
                else:
                    direction = DeltaDirection.STABLE
                    is_regression = False
            elif direction_rule == "lower_is_worse":
                if tgt_val < base_val:
                    direction = DeltaDirection.WORSE
                    is_regression = delta_pct is None or abs(delta_pct) > self.regression_threshold_pct
                elif tgt_val > base_val:
                    direction = DeltaDirection.BETTER
                    is_regression = False
                else:
                    direction = DeltaDirection.STABLE
                    is_regression = False
            else:
                direction = (DeltaDirection.WORSE if tgt_val > base_val else
                             DeltaDirection.BETTER if tgt_val < base_val else DeltaDirection.STABLE)
                is_regression = False

            delta = MetricDelta(
                metric=metric_name,
                baseline_value=base_val,
                target_value=tgt_val,
                delta_absolute=delta_abs,
                delta_percent=delta_pct,
                direction=direction,
                is_regression=is_regression,
                regression_threshold_pct=self.regression_threshold_pct,
            )
            deltas.append(delta)
            if is_regression:
                regressions.append(delta)
            elif direction == DeltaDirection.BETTER and direction_rule != "informational":
                improvements.append(delta)

        # Overall verdict
        if not comparable_count or (unavailable and not regressions):
            overall = "unknown"
        elif not regressions and improvements:
            overall = "improvement"
        elif regressions and not improvements:
            overall = "regression"
        elif regressions and improvements:
            overall = "mixed"
        else:
            overall = "stable"

        # Build summary
        summary_parts = []
        def describe(delta):
            change = (f"{delta.delta_percent:+.1f}%" if delta.delta_percent is not None
                      else f"{delta.delta_absolute:+.2f} absolute (percentage unavailable)")
            return f"{delta.metric} {change}"

        if regressions:
            summary_parts.append(
                f"{len(regressions)} regression(s): "
                + ", ".join(describe(r) for r in regressions)
            )
        if improvements:
            summary_parts.append(
                f"{len(improvements)} improvement(s): "
                + ", ".join(describe(i) for i in improvements)
            )
        if not summary_parts:
            summary_parts.append("Insufficient comparable measurements" if overall == "unknown"
                                 else "No significant changes detected")

        return RegressionReport(
            baseline_name=baseline_name,
            target_name=target_name,
            deltas=deltas,
            regressions=regressions,
            improvements=improvements,
            summary=" | ".join(summary_parts),
            overall_verdict=overall,
        )
