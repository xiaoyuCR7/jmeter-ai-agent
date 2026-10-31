"""ML-powered anomaly detection and metrics correlation.

Uses lightweight statistical methods (no heavy ML deps):
- Median and MAD for spike detection, which one spike cannot inflate the way it inflates a mean
- Drops measured against the median, away from the ramp-up and the tail
- Linear slope for trend analysis
"""

import math
import logging
from typing import Optional
from dataclasses import dataclass, field
from collections import defaultdict

from app.core.time import utc_now_naive

logger = logging.getLogger(__name__)


@dataclass
class Anomaly:
    """A detected performance anomaly."""
    metric: str
    value: float
    expected_range: tuple[float, float]  # (min, max) expected
    severity: str  # "low" | "medium" | "high"
    timestamp: Optional[str] = None
    description: str = ""


@dataclass
class AnomalyReport:
    """Result of anomaly detection on a test report."""
    total_anomalies: int
    anomalies: list[Anomaly] = field(default_factory=list)
    health_score: float = 100.0  # 0-100, higher = healthier
    summary: str = ""

    def model_dump(self) -> dict:
        return {
            "total_anomalies": self.total_anomalies,
            "health_score": round(self.health_score, 1),
            "anomalies": [
                {"metric": a.metric, "value": a.value,
                 "expected_range": tuple(v if math.isfinite(v) else None for v in a.expected_range),
                 "severity": a.severity,
                 "description": a.description}
                for a in self.anomalies
            ],
            "summary": self.summary,
        }


class MLAnomalyDetector:
    """Statistical anomaly detection for test results.

    Usage:
        detector = MLAnomalyDetector()
        report = detector.analyze_timeline(timeline_segments)
        # Or with baseline comparison:
        report = detector.compare_to_baseline(current_metrics, baseline_metrics)
    """

    MIN_SEGMENTS = 4
    # Below these floors a tiny spread would turn ordinary jitter into outliers.
    MIN_SPREAD_SHARE = 0.1
    MIN_ERROR_SPREAD_POINTS = 1.0
    MAD_TO_SIGMA = 1.4826

    def __init__(self, sensitivity: float = 3.5):
        """
        Args:
            sensitivity: Modified z-score threshold (lower = more sensitive). Default 3.5.
        """
        self.sensitivity = sensitivity

    def analyze_timeline(self, timeline_segments: list) -> AnomalyReport:
        """Detect anomalies in a test's timeline data."""
        if not timeline_segments:
            return AnomalyReport(total_anomalies=0, health_score=100, summary="No data")

        anomalies = []

        # Extract metric series
        response_times = []
        error_rates = []
        throughputs = []

        ranges = []
        for seg in timeline_segments:
            ranges.append(getattr(seg, 'time_range', None) or (seg.get('time_range') if isinstance(seg, dict) else None))
            if hasattr(seg, 'avg_response_time'):
                val = seg.avg_response_time
            elif isinstance(seg, dict):
                val = seg.get('avg_response_time', 0) or seg.get('avg', 0)
            else:
                val = 0
            response_times.append(float(val))

            if hasattr(seg, 'error_rate'):
                val = seg.error_rate
            elif isinstance(seg, dict):
                val = seg.get('error_rate', 0)
            else:
                val = 0
            error_rates.append(float(val))

            if hasattr(seg, 'throughput'):
                val = seg.throughput
            elif isinstance(seg, dict):
                val = seg.get('throughput', 0)
            else:
                val = 0
            throughputs.append(float(val))

        # Response time anomalies
        rt_anomalies = self._detect_spikes(response_times, "response_time", ranges)
        anomalies.extend(rt_anomalies)

        # Error rate anomalies
        err_anomalies = self._detect_spikes(error_rates, "error_rate", ranges)
        anomalies.extend(err_anomalies)

        # Throughput drops
        thr_anomalies = self._detect_drops(throughputs, "throughput", ranges)
        anomalies.extend(thr_anomalies)

        # Response time trend divergence
        trend_anomalies = self._detect_trend_divergence(response_times)
        anomalies.extend(trend_anomalies)

        # Calculate health score
        health_score = self._calculate_health_score(
            response_times, error_rates, throughputs, anomalies
        )

        # Build summary
        severity_counts = defaultdict(int)
        for a in anomalies:
            severity_counts[a.severity] += 1

        summary_parts = []
        if severity_counts["high"] > 0:
            summary_parts.append(f"{severity_counts['high']} critical anomalies detected")
        if severity_counts["medium"] > 0:
            summary_parts.append(f"{severity_counts['medium']} warnings")
        if not summary_parts:
            summary_parts.append("No significant anomalies detected")

        return AnomalyReport(
            total_anomalies=len(anomalies),
            anomalies=anomalies,
            health_score=health_score,
            summary=" | ".join(summary_parts),
        )

    def compare_to_baseline(self, current: dict, baseline: dict) -> AnomalyReport:
        """Compare current metrics to a baseline and flag anomalies."""
        anomalies = []
        metrics_to_check = [
            ("avg_response_time", "ms", "lt"),
            ("p95_response_time", "ms", "lt"),
            ("p99_response_time", "ms", "lt"),
            ("error_rate", "%", "lt"),
            ("throughput", "RPS", "gt"),
        ]

        compared = 0
        for metric, unit, direction in metrics_to_check:
            try:
                curr_val = float(current[metric])
                base_val = float(baseline[metric])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(curr_val) or not math.isfinite(base_val):
                continue

            if base_val == 0:
                continue
            compared += 1

            change_pct = (curr_val - base_val) / base_val * 100

            if direction == "lt" and change_pct > 20:
                severity = "high" if change_pct > 50 else "medium"
                anomalies.append(Anomaly(
                    metric=metric,
                    value=round(float(curr_val), 2),
                    expected_range=(0, round(float(base_val) * 1.2, 2)),
                    severity=severity,
                    description=f"{metric}: {curr_val}{unit} vs baseline {base_val}{unit} ({change_pct:+.1f}%)",
                ))
            elif direction == "gt" and change_pct < -20:
                severity = "high" if change_pct < -50 else "medium"
                anomalies.append(Anomaly(
                    metric=metric,
                    value=round(float(curr_val), 2),
                    expected_range=(round(float(base_val) * 0.8, 2), float("inf")),
                    severity=severity,
                    description=f"{metric}: {curr_val}{unit} dropped from baseline {base_val}{unit} ({change_pct:+.1f}%)",
                ))

        health_score = max(0, 100 - len(anomalies) * 15)
        summary = f"Compared to baseline: {len(anomalies)} deviations found" if anomalies else "Matches baseline — no deviations"
        if not compared:
            summary = "Insufficient comparable baseline metrics"

        return AnomalyReport(
            total_anomalies=len(anomalies),
            anomalies=anomalies,
            health_score=health_score,
            summary=summary,
        )

    # ── Internal detection methods ──────────────────────

    @staticmethod
    def _median(values: list[float]) -> float:
        data = sorted(values)
        mid = len(data) // 2
        return data[mid] if len(data) % 2 else (data[mid - 1] + data[mid]) / 2

    @staticmethod
    def _where(ranges: Optional[list], index: int) -> str:
        return ranges[index] if ranges and ranges[index] else f"segment {index}"

    def _detect_spikes(self, values: list[float], metric: str, ranges: Optional[list] = None) -> list[Anomaly]:
        """Flag values far above the median. Only upward: a fast or error-free segment is not a problem."""
        if len(values) < self.MIN_SEGMENTS:
            return []
        anomalies = []
        median = self._median(values)
        mad = self._median([abs(v - median) for v in values]) * self.MAD_TO_SIGMA
        floor = self.MIN_ERROR_SPREAD_POINTS if metric == "error_rate" else median * self.MIN_SPREAD_SHARE
        spread = max(mad, floor)
        if spread == 0:
            return []

        for i, val in enumerate(values):
            score = (val - median) / spread
            if score > self.sensitivity:
                severity = "high" if score > self.sensitivity * 1.5 else "medium"
                anomalies.append(Anomaly(
                    metric=metric,
                    value=val,
                    expected_range=(max(0, median - self.sensitivity * spread),
                                    median + self.sensitivity * spread),
                    severity=severity,
                    description=f"{metric} spike at {self._where(ranges, i)}: {val:.2f} "
                                f"(median={median:.2f}, score={score:.1f})",
                ))
        return anomalies

    def _detect_drops(self, values: list[float], metric: str, ranges: Optional[list] = None) -> list[Anomaly]:
        """Detect significant drops between the ramp-up and the tail, where low rates are expected."""
        if len(values) < self.MIN_SEGMENTS:
            return []
        anomalies = []
        median = self._median(values)
        limit = median * 0.5
        inside = [i for i, val in enumerate(values) if val >= limit]
        if not inside:
            return []
        for i in range(inside[0], inside[-1] + 1):
            val = values[i]
            if val < limit:
                anomalies.append(Anomaly(
                    metric=metric,
                    value=val,
                    expected_range=(limit, float("inf")),
                    severity="medium",
                    description=f"{metric} drop at {self._where(ranges, i)}: {val:.2f} (median={median:.2f})",
                ))
        return anomalies

    def _detect_trend_divergence(self, values: list[float]) -> list[Anomaly]:
        """Detect if response times show a steadily worsening trend."""
        if len(values) < 5:
            return []
        # Simple linear regression slope
        n = len(values)
        x_mean = (n - 1) / 2
        y_mean = sum(values) / n
        numerator = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        if denominator == 0:
            return []

        slope = numerator / denominator
        y_start = y_mean - slope * x_mean

        if y_start > 0 and slope > 0:
            change_pct = (slope * n) / y_start * 100
            if change_pct > 30:
                return [Anomaly(
                    metric="response_time_trend",
                    value=round(slope, 4),
                    expected_range=(-float("inf"), 0),
                    severity="medium",
                    description=f"Response time trending upward: +{change_pct:.1f}% over test duration",
                )]
        return []

    def _calculate_health_score(self, rt: list, err: list, thr: list,
                                 anomalies: list) -> float:
        """Calculate a 0-100 health score."""
        score = 100.0

        # Penalize for anomalies
        for a in anomalies:
            if a.severity == "high":
                score -= 15
            elif a.severity == "medium":
                score -= 8
            else:
                score -= 3

        # Penalize for high error rates
        avg_err = sum(err) / len(err) if err else 0
        if avg_err > 5:
            score -= 20
        elif avg_err > 1:
            score -= 10

        return max(0, min(100, score))


ml_detector = MLAnomalyDetector()
