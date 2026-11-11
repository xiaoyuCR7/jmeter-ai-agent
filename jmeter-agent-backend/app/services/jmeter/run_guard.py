"""Stops a run that has clearly gone wrong, instead of letting it hammer a broken system for an hour.

While JMeter writes its CSV result, the guard reads the new lines every few seconds and looks at
the most recent window. It stops the run, and says why, on any of three fixed rules, each twice in a
row and after a grace period:

- nearly every request fails (RUN_GUARD_ERROR_RATE);
- this machine is about to run out of memory (RUN_GUARD_MEMORY_PERCENT): JMeter would be swapped or
  killed, and the numbers are the machine's own long before that;
- responses take many times longer than at the start of the run (RUN_GUARD_LATENCY_FACTOR, off by
  default: a stress test is meant to slow the target down).

The rules are thresholds, not a model's judgement, so the same run always gets the same decision.
"""
import asyncio
import csv
import io
import logging
import os
from collections import deque

import psutil
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.core.config import settings
from app.core.text import tr

logger = logging.getLogger(__name__)

CHECK_SECONDS = 5
TRIPS_TO_STOP = 2  # Consecutive bad windows, so one burst of errors does not end a run.


@dataclass
class Window:
    """Samples of the last ``seconds``, as (timestamp ms, success, elapsed ms)."""
    seconds: float
    samples: deque = field(default_factory=deque)
    first_ms: Optional[int] = None
    baseline_ms: Optional[float] = None  # Average response time of the first full window after the grace period.

    def add(self, stamp: int, success: bool, elapsed: int = 0) -> None:
        if self.first_ms is None:
            self.first_ms = stamp
        self.samples.append((stamp, success, elapsed))

    def average_elapsed(self) -> float:
        return sum(sample[2] for sample in self.samples) / len(self.samples) if self.samples else 0.0

    def trim(self, now_ms: int) -> None:
        while self.samples and self.samples[0][0] < now_ms - self.seconds * 1000:
            self.samples.popleft()

    def error_rate(self) -> float:
        return 100.0 * sum(1 for sample in self.samples if not sample[1]) / len(self.samples) if self.samples else 0.0


class JtlTail:
    """Reads the rows a CSV JTL gained since the last read; a partial last line waits for the next."""

    def __init__(self, path: str):
        self.path, self.offset, self.header, self.rest = path, 0, None, ''

    def read(self) -> list[dict]:
        try:
            with open(self.path, encoding='utf-8', errors='replace', newline='') as stream:
                stream.seek(self.offset)
                chunk = stream.read()
                self.offset = stream.tell()
        except OSError:
            return []
        text = self.rest + chunk
        lines = text.split('\n')
        self.rest = lines.pop()  # Unfinished, or '' after a complete line.
        if self.header is None and lines:
            self.header = next(csv.reader([lines.pop(0)]))
        if not self.header or not lines:
            return []
        if 'timeStamp' not in self.header or 'success' not in self.header:
            return []  # Not a CSV result (an XML JTL); the guard has nothing to read.
        return list(csv.DictReader(io.StringIO('\n'.join(lines)), fieldnames=self.header, restval=''))


def judge(window: Window, now_ms: int) -> Optional[str]:
    """Why the run should stop now, or None."""
    if window.first_ms is None or now_ms - window.first_ms < settings.RUN_GUARD_GRACE_SECONDS * 1000:
        return None
    if len(window.samples) < settings.RUN_GUARD_MIN_SAMPLES:
        return None
    rate = window.error_rate()
    if rate >= settings.RUN_GUARD_ERROR_RATE:
        return tr(f'运行守护停止了这次运行：最近 {window.seconds:g} 秒 {len(window.samples)} 个请求中 {rate:.1f}% 失败'
                  f'（阈值 {settings.RUN_GUARD_ERROR_RATE:g}%）。先用试跑检查接口和环境，再重新运行。',
                  f'The run guard stopped this run: {rate:.1f}% of the {len(window.samples)} requests in the last '
                  f'{window.seconds:g} s failed (limit {settings.RUN_GUARD_ERROR_RATE:g}%). Check the requests and '
                  'the environment with a trial run, then run again.')
    return judge_latency(window, now_ms)


def judge_latency(window: Window, now_ms: int) -> Optional[str]:
    """Responses many times slower than when the run began; None while the rule is off or has no baseline yet."""
    factor = settings.RUN_GUARD_LATENCY_FACTOR
    if factor <= 1:
        return None
    start = window.first_ms + (settings.RUN_GUARD_GRACE_SECONDS + window.seconds) * 1000
    if window.baseline_ms is None:
        # The first window that lies wholly after the grace period, so warm-up does not set the bar.
        if now_ms >= start and window.average_elapsed() > 0:
            window.baseline_ms = window.average_elapsed()
        return None
    average = window.average_elapsed()
    if average >= factor * window.baseline_ms:
        return tr(f'运行守护停止了这次运行：最近 {window.seconds:g} 秒的平均响应时间 {average:.0f} ms，'
                  f'是运行开始时（{window.baseline_ms:.0f} ms）的 {average / window.baseline_ms:.1f} 倍'
                  f'（阈值 {factor:g} 倍）。被测系统已经明显过载；降低负载后重新运行。',
                  f'The run guard stopped this run: the average response time of the last {window.seconds:g} s was '
                  f'{average:.0f} ms, {average / window.baseline_ms:.1f} times what it was when the run began '
                  f'({window.baseline_ms:.0f} ms; limit {factor:g} times). The target is clearly overloaded; '
                  'lower the load and run again.')
    return None


def judge_machine(memory_percent: float) -> Optional[str]:
    """This machine nearly out of memory; None while the rule is off."""
    limit = settings.RUN_GUARD_MEMORY_PERCENT
    if limit <= 0 or memory_percent < limit:
        return None
    return tr(f'运行守护停止了这次运行：压测机内存占用 {memory_percent:.0f}%（阈值 {limit:g}%），'
              'JMeter 即将被换出或终止，结果已不可信。调大内存、降低线程数或改用分布式执行后重新运行。',
              f'The run guard stopped this run: memory on the load generator was {memory_percent:.0f}% full '
              f'(limit {limit:g}%). JMeter was about to be swapped out or killed and its numbers could no longer '
              'be trusted. Add memory, use fewer threads or run distributed, then run again.')


async def watch(jtl_path: str, stop: Callable[[str], Awaitable[None]], *, live=None,
                check_seconds: float = CHECK_SECONDS) -> None:
    """Read ``jtl_path`` until cancelled; call ``stop(reason)`` once if the run should end.

    ``live`` (a LiveStats) gets every row too, for the live view; it is fed even with the guard switched off.
    """
    guarding = settings.RUN_GUARD_ENABLED
    if not guarding and live is None:
        return
    tail, window, trips = JtlTail(jtl_path), Window(settings.RUN_GUARD_WINDOW_SECONDS), 0
    while True:
        await asyncio.sleep(check_seconds)
        if not os.path.exists(jtl_path):
            continue
        rows = await asyncio.to_thread(tail.read)
        for row in rows:
            if live is not None:
                live.add_row(row)
            if not guarding:
                continue
            try:
                elapsed = row.get('elapsed') or 0
                window.add(int(row['timeStamp']), str(row.get('success', '')).lower() == 'true',
                           int(elapsed) if str(elapsed).isdigit() else 0)
            except (KeyError, TypeError, ValueError):
                continue
        if not guarding:
            continue
        reason = judge_machine(psutil.virtual_memory().percent)
        if reason is None and window.samples:
            now = window.samples[-1][0]
            window.trim(now)
            reason = judge(window, now)
        trips = trips + 1 if reason else 0
        if reason and trips >= TRIPS_TO_STOP:
            logger.warning('Run guard stopping %s: %s', jtl_path, reason)
            await stop(reason)
            return
