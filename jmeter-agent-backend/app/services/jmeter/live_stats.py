"""What a run is doing right now, read from its JTL while JMeter writes it.

The run guard already reads the new result rows every few seconds; it hands them here too. Rows are
grouped into 5-second buckets by their start time, so the page can draw throughput, response time,
errors and threads over time. JMeter writes a row when the request ends, so a bucket is still filling
for as long as a request can take: the newest bucket is marked partial and a bucket's raw times are
kept until it is SETTLE_MS old. Everything is in memory and dropped when the run ends; the report
then has the exact numbers.
"""
import math
import time
from collections import OrderedDict
from typing import Optional

BUCKET_MS = 5_000
KEEP_BUCKETS = 720  # One hour of 5-second points; older ones scroll off.
SETTLE_MS = 30_000
WINDOW_MS = 30_000
TOP_LABELS = 8


def _p95(times: list) -> float:
    ordered = sorted(times)
    return float(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]) if ordered else 0.0


class _Bucket:
    __slots__ = ('count', 'errors', 'total_ms', 'threads', 'times', 'p95')

    def __init__(self):
        self.count = self.errors = self.total_ms = self.threads = 0
        self.times: Optional[list] = []
        self.p95 = 0.0

    def add(self, elapsed: int, success: bool, threads: int) -> None:
        self.count += 1
        self.errors += 0 if success else 1
        self.total_ms += elapsed
        self.threads = max(self.threads, threads)
        if self.times is not None:
            self.times.append(elapsed)

    def settle(self) -> None:
        if self.times is not None:
            self.p95, self.times = _p95(self.times), None

    def percentile(self) -> float:
        return _p95(self.times) if self.times is not None else self.p95


class LiveStats:
    def __init__(self, planned_seconds: Optional[float] = None, planned_threads: int = 0):
        self.planned_seconds, self.planned_threads = planned_seconds, planned_threads
        self.started = time.monotonic()
        self.buckets: 'OrderedDict[int, _Bucket]' = OrderedDict()
        self.labels: dict[str, list] = {}  # label -> [requests, errors, total ms]
        self.first_ms: Optional[int] = None
        self.latest_ms: Optional[int] = None
        self.requests = self.errors = self.total_ms = 0

    def add(self, stamp: int, elapsed: int, success: bool, label: str = '', threads: int = 0) -> None:
        key = stamp // BUCKET_MS
        bucket = self.buckets.get(key)
        if bucket is None:
            if len(self.buckets) >= KEEP_BUCKETS and key < next(iter(self.buckets)):
                return  # Older than everything kept: it has scrolled off.
            out_of_order = bool(self.buckets) and key < next(reversed(self.buckets))
            bucket = self.buckets[key] = _Bucket()
            if out_of_order:  # A long request that started earlier was written later.
                self.buckets = OrderedDict(sorted(self.buckets.items()))
        bucket.add(elapsed, success, threads)
        self.first_ms = stamp if self.first_ms is None else min(self.first_ms, stamp)
        self.latest_ms = stamp if self.latest_ms is None else max(self.latest_ms, stamp)
        self.requests += 1
        self.errors += 0 if success else 1
        self.total_ms += elapsed
        stats = self.labels.setdefault(label or '-', [0, 0, 0])
        stats[0] += 1
        stats[1] += 0 if success else 1
        stats[2] += elapsed

    def add_row(self, row: dict) -> None:
        try:
            stamp, elapsed = int(row['timeStamp']), int(row.get('elapsed') or 0)
        except (KeyError, TypeError, ValueError):
            return
        try:
            threads = int(row.get('allThreads') or 0)
        except ValueError:
            threads = 0
        self.add(stamp, elapsed, str(row.get('success', '')).lower() == 'true', row.get('label') or '', threads)

    def tidy(self) -> None:
        """Settle buckets no late row can reach any more, and drop the oldest past KEEP_BUCKETS."""
        if self.latest_ms is None:
            return
        for key, bucket in self.buckets.items():
            if (key + 1) * BUCKET_MS < self.latest_ms - SETTLE_MS:
                bucket.settle()
        while len(self.buckets) > KEEP_BUCKETS:
            self.buckets.popitem(last=False)

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started

    def progress(self) -> Optional[int]:
        """Elapsed time against the planned duration; unknown (None) when the run ends by loop count."""
        if not self.planned_seconds:
            return None
        return max(1, min(99, round(100 * self.elapsed_seconds() / self.planned_seconds)))

    def snapshot(self) -> dict:
        self.tidy()
        elapsed = self.elapsed_seconds()
        series = []
        origin = next(iter(self.buckets)) if self.buckets else 0
        for key, bucket in self.buckets.items():
            series.append({
                't': (key - origin) * BUCKET_MS // 1000, 'requests': bucket.count,
                'throughput': round(bucket.count / (BUCKET_MS / 1000), 2),
                'error_rate': round(100 * bucket.errors / bucket.count, 2) if bucket.count else 0.0,
                'avg': round(bucket.total_ms / bucket.count, 1) if bucket.count else 0.0,
                'p95': round(bucket.percentile(), 1), 'threads': bucket.threads,
                # Still filling: requests that started then may not have ended yet.
                'partial': self.latest_ms is not None and (key + 1) * BUCKET_MS > self.latest_ms,
            })
        return {
            'elapsed_seconds': round(elapsed),
            'planned_seconds': self.planned_seconds,
            'remaining_seconds': max(0, round(self.planned_seconds - elapsed)) if self.planned_seconds else None,
            'planned_threads': self.planned_threads,
            'totals': self._totals(series),
            'window': self._window(),
            'series': series,
            'labels': self._labels(),
        }

    def _totals(self, series: list) -> dict:
        span = (self.latest_ms - self.first_ms) / 1000 if self.requests else 0
        return {
            'requests': self.requests, 'errors': self.errors,
            'error_rate': round(100 * self.errors / self.requests, 2) if self.requests else 0.0,
            'avg': round(self.total_ms / self.requests, 1) if self.requests else 0.0,
            'throughput': round(self.requests / span, 2) if span > 0 else 0.0,
            'threads': series[-1]['threads'] if series else 0,
        }

    def _window(self) -> dict:
        """The last 30 seconds of finished buckets: how the run is doing now, not on average."""
        if self.latest_ms is None:
            return {'seconds': 0, 'requests': 0, 'throughput': 0.0, 'error_rate': 0.0, 'avg': 0.0, 'p95': 0.0}
        recent = [bucket for key, bucket in self.buckets.items()
                  if (key + 1) * BUCKET_MS <= self.latest_ms and (key + 1) * BUCKET_MS > self.latest_ms - WINDOW_MS]
        count = sum(bucket.count for bucket in recent)
        seconds = len(recent) * BUCKET_MS / 1000
        times = [value for bucket in recent for value in (bucket.times or [])]
        return {
            'seconds': seconds, 'requests': count,
            'throughput': round(count / seconds, 2) if seconds else 0.0,
            'error_rate': round(100 * sum(b.errors for b in recent) / count, 2) if count else 0.0,
            'avg': round(sum(b.total_ms for b in recent) / count, 1) if count else 0.0,
            'p95': round(_p95(times), 1),
        }

    def _labels(self) -> list:
        """The requests to look at first: the most errors, then the slowest."""
        rows = [{'label': label, 'requests': count, 'errors': errors,
                 'error_rate': round(100 * errors / count, 2), 'avg': round(total / count, 1)}
                for label, (count, errors, total) in self.labels.items()]
        rows.sort(key=lambda row: (-row['errors'], -row['avg']))
        return rows[:TOP_LABELS]


_live: dict[str, LiveStats] = {}


def start(execution_id: str, planned_seconds: Optional[float] = None, planned_threads: int = 0) -> LiveStats:
    stats = _live[execution_id] = LiveStats(planned_seconds, planned_threads)
    return stats


def get(execution_id: str) -> Optional[LiveStats]:
    return _live.get(execution_id)


def finish(execution_id: str) -> None:
    _live.pop(execution_id, None)
