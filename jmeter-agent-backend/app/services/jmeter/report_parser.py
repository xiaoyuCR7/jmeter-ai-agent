import os
import csv
import heapq
import time
import logging
from typing import Any, Optional
from collections import Counter, defaultdict

from app.models.schemas import DetailedTestSummary, ErrorDetail, TimelineSegment, PerformanceTrend
from app.core.text import tr

logger = logging.getLogger(__name__)


class ReportParser:
    """JMeter报告解析器"""

    @staticmethod
    def _safe_int(val: Any, default: int = 0) -> int:
        """Convert field to int, returning default on empty/None/invalid."""
        if val is None or val == "":
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_str(val: Any, default: str = "") -> str:
        """Convert field to str, returning default on None."""
        if val is None:
            return default
        return str(val)

    @staticmethod
    def _bucket_sort_key(key: str) -> int:
        """Safe sort key for time-bucket strings like '0-30s' or '-30-0s'."""
        try:
            return int(key.split("-")[0])
        except (ValueError, IndexError):
            return 0

    def parse_jtl(self, jtl_path: str) -> DetailedTestSummary:
        """解析JTL结果文件，返回详细的测试摘要"""
        if not os.path.exists(jtl_path):
            raise FileNotFoundError(f"JTL file not found: {jtl_path}")

        # A file JMeter is still flushing must settle first; finished reports need no wait.
        if time.time() - os.path.getmtime(jtl_path) < self.SETTLED_AFTER_SECONDS:
            self._wait_file_stable(jtl_path)

        with open(jtl_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            f.seek(0)

            if first_line.startswith("<"):
                return self._parse_xml_jtl(f)
            else:
                return self._parse_csv_jtl(f)

    SETTLED_AFTER_SECONDS = 2.0

    def _wait_file_stable(self, path: str, max_wait: float = 5.0, interval: float = 0.5):
        """Wait until file size stops changing (JMeter finished flushing)."""
        deadline = time.time() + max_wait
        last_size = -1
        while time.time() < deadline:
            try:
                cur_size = os.path.getsize(path)
            except OSError:
                time.sleep(interval)
                continue
            if cur_size == last_size and cur_size > 0:
                return
            last_size = cur_size
            time.sleep(interval)
        logger.debug("File stabilization timeout for %s (size=%s)", path, last_size)

    def _parse_csv_jtl(self, file) -> DetailedTestSummary:
        """解析CSV格式的JTL，生成详细的分析报告数据"""
        return self._summarize(file, self._csv_rows)

    def _parse_xml_jtl(self, file) -> DetailedTestSummary:
        """解析XML格式的JTL；流式读取，处理完的元素立即释放。"""
        return self._summarize(file, self._xml_rows)

    def _summarize(self, file, rows) -> DetailedTestSummary:
        acc = _Accumulator()
        for row in rows(file):
            acc.add(**row)
        if not acc.has_samples:
            return self._empty_summary()
        if acc.wants_second_pass():
            # The timeline and the steady window need the run's start, length and peak: known only now.
            file.seek(0)
            for row in rows(file):
                acc.add_again(**row)
        return acc.summary(self)

    def _csv_rows(self, file):
        # restval="" prevents None values in DictReader when rows are incomplete
        # (JMeter may still be flushing the CSV buffer when we start reading)
        for row in csv.DictReader(file, restval=""):
            yield dict(
                elapsed=self._safe_int(row.get("elapsed")),
                label=self._safe_str(row.get("label"), "Unnamed") or "Unnamed",
                success=self._safe_str(row.get("success"), "true").lower() == "true",
                code=self._safe_str(row.get("responseCode")),
                message=self._safe_str(row.get("responseMessage")),
                received=self._safe_int(row.get("bytes")),
                sent=self._safe_int(row.get("sentBytes")),
                ts=self._safe_int(row.get("timeStamp")),
                failure=self._safe_str(row.get("failureMessage")),
                connect=self._safe_int(row.get("Connect")),
                latency=self._safe_int(row.get("Latency")),
                threads=self._safe_int(row.get("allThreads")),
            )

    def _xml_rows(self, file):
        import xml.etree.ElementTree as ET

        depth = 0
        for event, sample in ET.iterparse(file, events=("start", "end")):
            if sample.tag not in {"sample", "httpSample", "sampleResult"}:
                continue
            if event == "start":
                depth += 1
                continue
            depth -= 1
            if depth:
                continue  # A nested sub-result is part of its parent's elapsed time.
            failure = next(filter(None, (item.findtext("failureMessage")
                                         for item in sample.iter("assertionResult"))), "")
            yield dict(
                elapsed=self._safe_int(sample.get("t")),
                label=sample.get("lb", "") or "Unnamed",
                success=sample.get("s", "true") == "true",
                code=sample.get("rc", ""),
                message=sample.get("rm", ""),
                received=self._safe_int(sample.get("by")),
                sent=self._safe_int(sample.get("sb")),
                ts=self._safe_int(sample.get("ts")),
                failure=failure,
                connect=self._safe_int(sample.get("ct")),
                latency=self._safe_int(sample.get("lt")),
                threads=self._safe_int(sample.get("na")),
            )
            sample.clear()

    def _analyze_trend(self, segments: list) -> Optional[PerformanceTrend]:
        """分析性能趋势"""
        # A few samples in a ramp-up or tail segment say nothing about the trend.
        counts = sorted(s.request_count for s in segments)
        typical = counts[len(counts) // 2] if counts else 0
        segments = [s for s in segments if s.request_count >= typical * 0.1]
        if len(segments) < 3:
            return None

        def weighted(part, field):
            total = sum(s.request_count for s in part)
            return sum(getattr(s, field) * s.request_count for s in part) / total if total else 0

        # Outer thirds, so a steady climb reads as a direction rather than as noise.
        third = max(len(segments) // 3, 1)
        first_half_avg = weighted(segments[:third], "avg_response_time")
        second_half_avg = weighted(segments[-third:], "avg_response_time")
        first_half_err = weighted(segments[:third], "error_rate")
        second_half_err = weighted(segments[-third:], "error_rate")

        rt_values = [s.avg_response_time for s in segments]
        rt_variance = max(rt_values) - min(rt_values)
        rt_mean = sum(rt_values) / len(rt_values)
        fluctuation_ratio = rt_variance / rt_mean if rt_mean > 0 else 0
        middle_avg = weighted(segments[third:-third], "avg_response_time") if len(segments) > third * 2 else rt_mean
        low, high = sorted((first_half_avg, second_half_avg))
        directional = low * 0.8 <= middle_avg <= high * 1.2

        if second_half_avg > first_half_avg * 1.3 and directional:
            trend = "degrading"
            growth = (second_half_avg / first_half_avg - 1) * 100
            desc = tr(f"性能呈恶化趋势，后半段平均响应时间({second_half_avg:.0f}ms)比前半段({first_half_avg:.0f}ms)增加了 {growth:.0f}%",
                      f"Getting slower: the average response time of the last part ({second_half_avg:.0f} ms) is "
                      f"{growth:.0f}% above the first part ({first_half_avg:.0f} ms)")
            if second_half_err > first_half_err * 1.5 and second_half_err > 1:
                desc += tr(f"，且后半段错误率({second_half_err:.1f}%)显著上升",
                           f", and the error rate rose clearly towards the end ({second_half_err:.1f}%)")
        elif second_half_avg < first_half_avg * 0.8 and directional:
            trend = "improving"
            drop = (1 - second_half_avg / first_half_avg) * 100
            desc = tr(f"性能呈改善趋势，后半段平均响应时间({second_half_avg:.0f}ms)比前半段({first_half_avg:.0f}ms)降低了 {drop:.0f}%",
                      f"Getting faster: the average response time of the last part ({second_half_avg:.0f} ms) is "
                      f"{drop:.0f}% below the first part ({first_half_avg:.0f} ms)")
        elif fluctuation_ratio > 0.5:
            trend = "fluctuating"
            desc = tr(f"性能波动较大，响应时间在 {min(rt_values):.0f}ms ~ {max(rt_values):.0f}ms 之间大幅变化，系统稳定性不足",
                      f"Unsteady: response times swing between {min(rt_values):.0f} ms and {max(rt_values):.0f} ms")
        else:
            trend = "stable"
            desc = tr(f"性能总体稳定，前后半段平均响应时间分别为 {first_half_avg:.0f}ms 和 {second_half_avg:.0f}ms",
                      f"Steady: the average response time was {first_half_avg:.0f} ms in the first part and "
                      f"{second_half_avg:.0f} ms in the last")
            if second_half_err > 5:
                desc += tr(f"，但存在 {second_half_err:.1f}% 的持续错误率",
                           f", with a lasting error rate of {second_half_err:.1f}%")
        
        return PerformanceTrend(trend_direction=trend, description=desc)

    @staticmethod
    def _histogram_percentiles(histogram: Counter, count: int, points=(0.5, 0.9, 0.95, 0.99)) -> list:
        """Exact linear-interpolated percentiles from an integer-millisecond histogram."""
        if not count:
            return [0 for _ in points]
        wanted = {}
        for p in points:
            k = (count - 1) * p
            wanted.setdefault(int(k), None)
            wanted.setdefault(min(int(k) + 1, count - 1), None)
        targets = sorted(wanted)
        seen, index = 0, 0
        for value in sorted(histogram):
            seen += histogram[value]
            while index < len(targets) and targets[index] < seen:
                wanted[targets[index]] = value
                index += 1
            if index == len(targets):
                break
        result = []
        for p in points:
            k = (count - 1) * p
            low, high = int(k), min(int(k) + 1, count - 1)
            result.append(wanted[low] if low == high else wanted[low] * (high - k) + wanted[high] * (k - low))
        return result

    def _percentile(self, sorted_data: list, p: float) -> float:
        """计算百分位数"""
        if not sorted_data:
            return 0
        k = (len(sorted_data) - 1) * p
        f = int(k)
        c = f + 1 if f + 1 < len(sorted_data) else f
        if f == c:
            return sorted_data[f]
        return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)

    def _empty_summary(self) -> DetailedTestSummary:
        return DetailedTestSummary(
            total_requests=0,
            avg_response_time=0,
            min_response_time=0,
            max_response_time=0,
            p95_response_time=0,
            p99_response_time=0,
            error_rate=0,
            throughput=0,
            received_bytes_per_sec=0,
            sent_bytes_per_sec=0,
            percentiles={"p50": 0, "p90": 0, "p95": 0, "p99": 0},
            error_details=[],
            timeline_segments=[],
            performance_trend=None,
            response_time_distribution={}
        )

    def _generate_chart_insights(self, transactions: list, total: dict) -> list[str]:
        """Generate human-readable insights from dashboard chart data."""
        insights = []
        if not total:
            return insights

        # Overall health
        error_pct = total.get("error_pct", 0)
        if error_pct == 0:
            insights.append("✅ Zero errors — test ran cleanly")
        elif error_pct < 1:
            insights.append(f"⚠️ Low error rate: {error_pct}%")
        else:
            insights.append(f"🚨 High error rate: {error_pct}% — investigate errors")

        # Throughput vs latency
        p99 = total.get("p99_response_time", 0)
        tps = total.get("throughput", 0)
        avg = total.get("avg_response_time", 0)

        if avg > 0 and p99 > 0:
            if p99 > avg * 5:
                insights.append(
                    f"📊 High tail latency: P99 ({p99}ms) is {p99/avg:.0f}x the average ({avg}ms) "
                    f"— possible GC pauses, lock contention, or external dependency slowdown"
                )
            elif p99 > avg * 3:
                insights.append(
                    f"📊 Moderate tail latency: P99 ({p99}ms) vs avg ({avg}ms)"
                )

        if tps > 0:
            insights.append(f"📈 Throughput: {tps} req/s across {total.get('sample_count', 0)} samples")

        # Identify slowest transactions
        non_total = [t for t in transactions if t["name"] != "Total" and t.get("avg_response_time", 0) > 0]
        if non_total:
            slowest = sorted(non_total, key=lambda t: t["avg_response_time"], reverse=True)[:3]
            insights.append(
                "🐌 Slowest endpoints: " +
                ", ".join(f"{t['name']} (avg={t['avg_response_time']}ms, P99={t['p99_response_time']}ms)"
                          for t in slowest)
            )

            # Error hotspots
            error_txs = [t for t in non_total if t.get("error_pct", 0) > 0]
            if error_txs:
                insights.append(
                    "🔴 Error hotspots: " +
                    ", ".join(f"{t['name']} ({t['error_pct']}%)" for t in error_txs[:5])
                )

        return insights

    def get_html_report_url(self, report_dir: Optional[str]) -> Optional[str]:
        """获取HTML报告路径"""
        if not report_dir:
            return None
        html_dir = os.path.join(report_dir, "html")
        index_path = os.path.join(html_dir, "index.html")
        if os.path.exists(index_path):
            return index_path
        return None

    def read_log(self, log_path: str, tail_lines: int = 100) -> str:
        """读取日志文件尾部"""
        if not os.path.exists(log_path):
            return ""
        
        if not 1 <= tail_lines <= 5000:
            raise ValueError('tail_lines must be within [1, 5000]')
        with open(log_path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            text = stream.read(65536).decode('utf-8', errors='replace')
        return ''.join(text.splitlines(keepends=True)[-tail_lines:])


_RT_BANDS = (("0-100ms", 100), ("100-300ms", 300), ("300-500ms", 500), ("500ms-1s", 1000),
             ("1s-3s", 3000), ("3s-5s", 5000), (">5s", None))
_BUCKET_SECONDS = (5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600)
_MAX_SEGMENTS = 60
_TRANSACTION_MESSAGE = "Number of samples in transaction"
# The steady window must be long enough to stand for the run; a step test only touches its peak briefly.
_MIN_STEADY_SHARE = 0.3
_MIN_STEADY_SAMPLES = 30
_SLOWEST_SAMPLES = 8


def _latency_stats(parser, histogram: Counter, total: int, seconds: float) -> dict:
    """Latency and rate of a group of samples; None rather than 0 when the group is empty."""
    count = sum(histogram.values())
    if not count:
        return {"count": 0, "avg_response_time": None, "max_response_time": None, "p50_response_time": None,
                "p90_response_time": None, "p95_response_time": None, "p99_response_time": None, "throughput": 0.0}
    p50, p90, p95, p99 = parser._histogram_percentiles(histogram, count)
    return {"count": count, "avg_response_time": round(total / count, 2), "max_response_time": max(histogram),
            "p50_response_time": round(p50, 2), "p90_response_time": round(p90, 2),
            "p95_response_time": round(p95, 2), "p99_response_time": round(p99, 2),
            "throughput": round(count / seconds, 2)}


class _Accumulator:
    """Streaming JTL aggregation shared by the CSV and XML readers.

    Nothing is kept per sample, so memory follows the run's length in seconds and its distinct
    response times, not its number of requests (per-sample arrays used to cost 29 bytes a request:
    gigabytes for a long soak test). Response times are integer milliseconds, so percentiles come
    exactly from histograms.

    When the run started, how long it lasted and when it was at peak concurrency are only known once
    the whole file has been read, so the timeline and the steady window are built by a second read:
    ``wants_second_pass``, then ``add_again`` for every row.
    """

    def __init__(self):
        self.total = self.errors = self.received = self.sent = 0
        self.elapsed_sum = self.failed_sum = 0
        self.histogram = Counter()
        self.failed_histogram = Counter()  # Successes are the difference; failures are usually few.
        self.transactions = None  # Parent rows of Transaction Controllers, kept out of the totals.
        self.bands = dict.fromkeys((name for name, _ in _RT_BANDS), 0)
        self.labels = {}
        self.error_details = defaultdict(lambda: {"count": 0, "messages": [], "labels": Counter()})
        # Samples with a timestamp: what the timeline and the diagnostics are built from.
        self.timed = 0
        self.first = self.last_end = None
        self.width = 0  # Seconds per timeline segment, chosen from the run's duration.
        self.buckets = {}  # segment -> count, sum, max, errors, connect, latency, threads, wait
        self.timed_elapsed = self.connect_sum = self.latency_sum = self.wait_sum = self.new_connections = 0
        self.peak = 0
        self.peak_start = self.peak_end = None  # First and last request sent at peak concurrency.
        self.slowest = []  # Min-heap of the _SLOWEST_SAMPLES slowest requests.
        self.steady_window = None
        self.steady_groups = None

    @property
    def has_samples(self) -> bool:
        return bool(self.total or self.transactions)

    def add(self, *, elapsed, label, success, code, message, received, sent, ts,
            failure="", connect=0, latency=0, threads=0):
        if message.startswith(_TRANSACTION_MESSAGE):
            # The parent repeats its children, so counting both would double every total.
            if self.transactions is None:
                self.transactions = _Accumulator()
            self.transactions.add(elapsed=elapsed, label=label, success=success, code=code, message="",
                                  received=received, sent=sent, ts=ts, failure=failure or message,
                                  connect=connect, latency=latency, threads=threads)
            return
        self.total += 1
        self.elapsed_sum += elapsed
        self.histogram[elapsed] += 1
        for name, limit in _RT_BANDS:
            if limit is None or elapsed < limit:
                self.bands[name] += 1
                break
        stats = self.labels.get(label)
        if stats is None:
            stats = self.labels[label] = {"count": 0, "errors": 0, "sum": 0, "received": 0, "histogram": Counter(),
                                          "failed_sum": 0, "failed_histogram": Counter()}
        stats["count"] += 1
        stats["sum"] += elapsed
        stats["received"] += received
        stats["histogram"][elapsed] += 1
        if not success:
            self.failed_sum += elapsed
            self.failed_histogram[elapsed] += 1
            stats["failed_sum"] += elapsed
            stats["failed_histogram"][elapsed] += 1
            self.errors += 1
            stats["errors"] += 1
            # An assertion fails a 200 OK response; the reason is only in failureMessage.
            reason = failure or message
            key = f"{code}: {reason}" if code else reason
            if not key or key == ": ":
                key = "Unknown Error"
            detail = self.error_details[key]
            detail["count"] += 1
            detail["labels"][label] += 1
            if len(detail["messages"]) < 3:
                detail["messages"].append(reason or "No message")
        self.received += received
        self.sent += sent
        if ts > 0:
            self._add_timed(ts, elapsed, success, connect, latency, threads, label)

    def _add_timed(self, ts, elapsed, success, connect, latency, threads, label):
        if self.first is None:
            self.first, self.last_end = ts, ts + max(elapsed, 0)
        else:
            self.first = min(self.first, ts)
            self.last_end = max(self.last_end, ts + max(elapsed, 0))
        wait = max(latency - connect, 0)
        self.timed_elapsed += elapsed
        self.connect_sum += connect
        self.latency_sum += latency
        self.wait_sum += wait
        if connect > 0:
            self.new_connections += 1
        if threads > self.peak:
            self.peak, self.peak_start, self.peak_end = threads, ts, ts
        elif threads == self.peak and threads:
            self.peak_start, self.peak_end = min(self.peak_start, ts), max(self.peak_end, ts)
        # Slowest first; of equally slow requests the earliest, then the one read first.
        entry = (elapsed, -ts, -self.timed, connect, wait, label, threads, success)
        self.timed += 1
        if len(self.slowest) < _SLOWEST_SAMPLES:
            heapq.heappush(self.slowest, entry)
        elif entry > self.slowest[0]:
            heapq.heapreplace(self.slowest, entry)

    def _duration(self) -> float:
        return max((self.last_end - self.first) / 1000.0, 0.001) if self.timed else 1.0

    def _timeline(self):
        """Use the earliest start and latest end, independent of row order."""
        if not self.timed:
            return 1.0, []
        duration, width = self._duration(), self.width
        segments = []
        for index in sorted(self.buckets):
            count, total, peak, errors, connect, latency, threads, wait = self.buckets[index]
            start = index * width
            window = max(min(float(width), duration - start), 0.001)  # Tail buckets use their observed duration
            segments.append(TimelineSegment(
                time_range=f"{start}-{start + width}s", avg_response_time=round(total / count, 2),
                max_response_time=peak, throughput=round(count / window, 2),
                error_rate=round(errors / count * 100, 2), request_count=count,
                active_threads=threads or None, avg_connect_time=round(connect / count, 2),
                avg_latency=round(latency / count, 2), avg_wait_time=round(wait / count, 2)))
        return duration, segments

    def wants_second_pass(self) -> bool:
        """Whether the file must be read again; call once every row has been added."""
        wanted = self.transactions is not None and self.transactions.wants_second_pass()
        if not self.timed:
            return wanted
        duration = self._duration()
        # Short runs need short segments: a 60 s test in 30 s buckets hides every event in it.
        self.width = next((w for w in _BUCKET_SECONDS if duration / w <= _MAX_SEGMENTS), _BUCKET_SECONDS[-1])
        if self.peak:
            seconds = max((self.peak_end - self.peak_start) / 1000.0, 0.001)
            if seconds >= duration * _MIN_STEADY_SHARE:
                self.steady_window = (self.peak_start, self.peak_end, seconds)
                # Every request in the window, and only those on a connection already open.
                self.steady_groups = {reused: {"overall": [Counter(), 0], "labels": {}, "samples": 0, "failures": 0}
                                      for reused in (True, False)}
        return True

    def add_again(self, *, elapsed, label, success, message, ts, connect=0, latency=0, threads=0, **_):
        if message.startswith(_TRANSACTION_MESSAGE):
            if self.transactions is not None:
                self.transactions.add_again(elapsed=elapsed, label=label, success=success, message="",
                                            ts=ts, connect=connect, latency=latency, threads=threads)
            return
        if ts <= 0 or not self.width:
            return
        index = (ts - self.first) // (self.width * 1000)
        bucket = self.buckets.get(index)
        if bucket is None:
            bucket = self.buckets[index] = [0, 0, 0, 0, 0, 0, 0, 0]
        bucket[0] += 1
        bucket[1] += elapsed
        bucket[2] = max(bucket[2], elapsed)
        bucket[3] += 0 if success else 1
        bucket[4] += connect
        bucket[5] += latency
        bucket[6] = max(bucket[6], threads)
        bucket[7] += max(latency - connect, 0)
        if self.steady_window is None or not self.steady_window[0] <= ts <= self.steady_window[1]:
            return
        for reused_only, group in self.steady_groups.items():
            if reused_only and connect:
                continue
            group["samples"] += 1
            if not success:
                group["failures"] += 1
                continue
            for target in (group["overall"], group["labels"].setdefault(label, [Counter(), 0])):
                target[0][elapsed] += 1
                target[1] += elapsed

    def _steady(self, parser) -> tuple[Optional[dict], Optional[dict]]:
        """The window at peak concurrency, and what the run measured inside it."""
        if self.steady_window is None:
            return None, None
        start, end, seconds = self.steady_window
        # A new connection costs far more than the request it carries; the first request of a
        # late-starting user would otherwise set the steady P99. Without keep-alive none are reused.
        group = self.steady_groups[True]
        if group["samples"] < _MIN_STEADY_SAMPLES:
            group = self.steady_groups[False]
        if group["samples"] < _MIN_STEADY_SAMPLES:
            return None, None
        window = {"start_s": round((start - self.first) / 1000.0, 1), "end_s": round((end - self.first) / 1000.0, 1),
                  "seconds": round(seconds, 1)}
        stats = _latency_stats(parser, group["overall"][0], group["overall"][1], seconds)
        stats["error_rate"] = round(group["failures"] / group["samples"] * 100, 2)
        stats["labels"] = {name: _latency_stats(parser, values[0], values[1], seconds)
                           for name, values in group["labels"].items()}
        return window, stats

    def _diagnostics(self, parser, duration: float) -> Optional[dict]:
        if not self.timed:
            return None
        count = self.timed
        window, steady = self._steady(parser)
        return {
            "load": {"peak_threads": self.peak or None, "duration_seconds": round(duration, 1),
                     "steady_window": window},
            "steady": steady,
            "timing": {"avg_connect_time": round(self.connect_sum / count, 2),
                       "avg_latency": round(self.latency_sum / count, 2),
                       "avg_wait_time": round(self.wait_sum / count, 2),
                       "avg_download_time": round((self.timed_elapsed - self.latency_sum) / count, 2),
                       "new_connections": self.new_connections,
                       "avg_connect_when_new": (round(self.connect_sum / self.new_connections, 2)
                                                if self.new_connections else None)},
            "slowest": [{"offset_s": round((-negative_ts - self.first) / 1000.0, 1), "elapsed": elapsed,
                         "connect": connect, "wait": wait, "label": label, "threads": threads,
                         "success": bool(success)}
                        for elapsed, negative_ts, _, connect, wait, label, threads, success
                        in sorted(self.slowest, reverse=True)],
        }

    def summary(self, parser) -> DetailedTestSummary:
        if not self.total:
            return self.transactions.summary(parser)  # The plan only recorded parent samples.
        duration, segments = self._timeline()
        per_label = {}
        for label, stats in self.labels.items():
            p90, p95, p99 = parser._histogram_percentiles(stats["histogram"], stats["count"], (0.9, 0.95, 0.99))
            per_label[label] = {
                "count": stats["count"],
                "avg_response_time": round(stats["sum"] / stats["count"], 2),
                "min_response_time": min(stats["histogram"]),
                "max_response_time": max(stats["histogram"]),
                "p90_response_time": round(p90, 2),
                "p95_response_time": round(p95, 2),
                "p99_response_time": round(p99, 2),
                "error_rate": round(stats["errors"] / stats["count"] * 100, 2),
                "throughput": round(stats["count"] / duration, 2),
                "bytes_per_sec": round(stats["received"] / duration, 2),
                "success": _latency_stats(parser, stats["histogram"] - stats["failed_histogram"],
                                          stats["sum"] - stats["failed_sum"], duration),
            }
        if self.transactions is not None:
            for label, stats in (self.transactions.summary(parser).per_label or {}).items():
                per_label.setdefault(label, {**stats, "is_transaction": True})
        p50, p90, p95, p99 = (round(v, 2) for v in parser._histogram_percentiles(self.histogram, self.total))
        errors = [ErrorDetail(error_type=key[:100], count=data["count"],
                              percentage=round(data["count"] / self.total * 100, 2),
                              sample_messages=data["messages"][:3],
                              labels=dict(data["labels"].most_common(5)))
                  for key, data in sorted(self.error_details.items(), key=lambda x: x[1]["count"], reverse=True)[:10]]
        return DetailedTestSummary(
            total_requests=self.total,
            avg_response_time=round(self.elapsed_sum / self.total, 2),
            min_response_time=min(self.histogram),
            max_response_time=max(self.histogram),
            p95_response_time=p95,
            p99_response_time=p99,
            error_rate=round(self.errors / self.total * 100, 2),
            throughput=round(self.total / duration, 2),
            received_bytes_per_sec=round(self.received / duration, 2),
            sent_bytes_per_sec=round(self.sent / duration, 2),
            percentiles={"p50": p50, "p90": p90, "p95": p95, "p99": p99},
            error_details=errors,
            timeline_segments=segments,
            performance_trend=parser._analyze_trend(segments),
            response_time_distribution={k: round(v / self.total * 100, 2) for k, v in self.bands.items() if v > 0},
            per_label=per_label or None,
            success=_latency_stats(parser, self.histogram - self.failed_histogram,
                                   self.elapsed_sum - self.failed_sum, duration),
            diagnostics=self._diagnostics(parser, duration),
        )
