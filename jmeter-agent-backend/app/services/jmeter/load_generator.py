"""How busy the machine running JMeter was while a run lasted.

A load generator out of CPU measures its own queueing: latencies rise and throughput flattens with the
server idle. Two readings, before and after, cannot show that, so every run is sampled throughout and
the report is told when the numbers cannot be trusted. The rule is fixed thresholds, not a judgement.
"""
import asyncio
import logging
import time
from typing import Optional

import psutil

logger = logging.getLogger(__name__)

SAMPLE_SECONDS = 5.0
SATURATED_CPU = 90.0      # % of all cores; JMeter's timers and I/O threads starve above this
SATURATED_MEMORY = 95.0   # %; the JVM is swapping or about to be killed
SATURATED_SHARE = 0.2     # of the run's samples at or above a limit
MIN_SAMPLES = 3           # fewer say nothing about a run


def read() -> dict:
    """CPU (averaged over a moment) and memory of this machine, now."""
    memory = psutil.virtual_memory()
    return {'cpu_percent': round(psutil.cpu_percent(interval=0.2), 1), 'memory_percent': round(memory.percent, 1),
            'memory_used_gb': round(memory.used / 1024 ** 3, 2), 'memory_total_gb': round(memory.total / 1024 ** 3, 2),
            'timestamp': time.time()}


def summarize(samples: list[dict], interval: float = SAMPLE_SECONDS) -> Optional[dict]:
    """Average, peak and how much of the run the machine spent at its limit; None without samples."""
    if not samples:
        return None
    cpu = [sample['cpu_percent'] for sample in samples]
    memory = [sample['memory_percent'] for sample in samples]
    busy = sum(1 for sample in samples
               if sample['cpu_percent'] >= SATURATED_CPU or sample['memory_percent'] >= SATURATED_MEMORY)
    share = busy / len(samples)
    return {
        'samples': len(samples), 'interval_seconds': interval,
        'cpu_avg': round(sum(cpu) / len(cpu), 1), 'cpu_peak': max(cpu),
        'memory_avg': round(sum(memory) / len(memory), 1), 'memory_peak': max(memory),
        'saturated_share': round(share, 3),
        'saturated': len(samples) >= MIN_SAMPLES and share >= SATURATED_SHARE,
    }


class Sampler:
    """Samples the machine from ``start`` to ``stop``; neither can fail a run."""

    def __init__(self, interval: float = SAMPLE_SECONDS):
        self.interval = interval
        self.samples: list[dict] = []
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> Optional[dict]:
        """The reading just before the run, which is not one of its samples."""
        try:
            before = await asyncio.to_thread(read)
        except Exception as error:
            logger.warning('Load generator not sampled: %s', error)
            return None
        self._task = asyncio.create_task(self._loop())
        return before

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                self.samples.append(await asyncio.to_thread(read))
            except Exception as error:
                logger.debug('Load generator sample skipped: %s', error)

    async def stop(self) -> Optional[dict]:
        """The last reading, with ``during`` summarising the run when it lasted long enough to sample."""
        if self._task is None:
            return None
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        try:
            after = self.samples[-1] if self.samples else await asyncio.to_thread(read)
        except Exception:
            return None
        during = summarize(self.samples, self.interval)
        return {**after, **({'during': during} if during else {})}
