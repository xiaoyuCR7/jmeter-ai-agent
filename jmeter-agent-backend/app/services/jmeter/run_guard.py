"""Stops a run that has clearly gone wrong, instead of letting it hammer a broken system for an hour.

While JMeter writes its CSV result, the guard reads the new lines every few seconds and looks at
the most recent window. When nearly every request fails, twice in a row and after a grace period,
it stops the run and says why. The rule is a fixed threshold, not a model's judgement, so the
same run always gets the same decision. RUN_GUARD_* settings tune or switch it off.
"""
import asyncio
import csv
import io
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

CHECK_SECONDS = 5
TRIPS_TO_STOP = 2  # Consecutive bad windows, so one burst of errors does not end a run.


@dataclass
class Window:
    """Samples of the last ``seconds``, as (timestamp ms, success)."""
    seconds: float
    samples: deque = field(default_factory=deque)
    first_ms: Optional[int] = None

    def add(self, stamp: int, success: bool) -> None:
        if self.first_ms is None:
            self.first_ms = stamp
        self.samples.append((stamp, success))

    def trim(self, now_ms: int) -> None:
        while self.samples and self.samples[0][0] < now_ms - self.seconds * 1000:
            self.samples.popleft()

    def error_rate(self) -> float:
        return 100.0 * sum(1 for _, ok in self.samples if not ok) / len(self.samples) if self.samples else 0.0


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
        return (f'运行守护停止了这次运行：最近 {window.seconds:g} 秒 {len(window.samples)} 个请求中 {rate:.1f}% 失败'
                f'（阈值 {settings.RUN_GUARD_ERROR_RATE:g}%）。先用试跑检查接口和环境，再重新运行。')
    return None


async def watch(jtl_path: str, stop: Callable[[str], Awaitable[None]], *, check_seconds: float = CHECK_SECONDS) -> None:
    """Read ``jtl_path`` until cancelled; call ``stop(reason)`` once if the run should end."""
    if not settings.RUN_GUARD_ENABLED:
        return
    tail, window, trips = JtlTail(jtl_path), Window(settings.RUN_GUARD_WINDOW_SECONDS), 0
    while True:
        await asyncio.sleep(check_seconds)
        if not os.path.exists(jtl_path):
            continue
        rows = await asyncio.to_thread(tail.read)
        for row in rows:
            try:
                window.add(int(row['timeStamp']), str(row.get('success', '')).lower() == 'true')
            except (KeyError, TypeError, ValueError):
                continue
        if not window.samples:
            continue
        now = window.samples[-1][0]
        window.trim(now)
        reason = judge(window, now)
        trips = trips + 1 if reason else 0
        if reason and trips >= TRIPS_TO_STOP:
            logger.warning('Run guard stopping %s: %s', jtl_path, reason)
            await stop(reason)
            return
