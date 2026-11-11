"""Whether this machine is quiet enough to start a load test, and the wait until it is.

A test started on a busy load generator, or right after another test, measures the machine as much as
the target. The execution lifecycle asks here before unattended runs (agents, verification runs,
capacity steps); a run the user starts by hand is recorded as it is, not held.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.core.config import settings
from app.services.jmeter.resource_monitor import ResourceMonitor, ResourceSnapshot, get_resource_monitor

logger = logging.getLogger(__name__)


class SchedulerDecision(str, Enum):
    """What the scheduler recommends before starting a test."""
    PROCEED = "proceed"           # Resources OK, can run now
    WAIT = "wait"                 # Resources too high, should wait
    WARNING = "warning"           # Resources borderline, run with caution
    OVERLOADED = "overloaded"     # Resources critically high, do not run


@dataclass
class ReadinessCheck:
    """Result of a pre-test readiness check."""

    decision: SchedulerDecision
    snapshot: ResourceSnapshot
    message: str
    suggested_wait_seconds: float = 0.0
    cpu_trend: list[float] = field(default_factory=list)  # last few CPU readings


class TestScheduler:
    """Readiness of the load generator: thresholds, and a cooldown after the previous run."""

    __test__ = False  # Not a pytest class, despite the name.

    def __init__(
        self,
        monitor: Optional[ResourceMonitor] = None,
        cpu_threshold: Optional[float] = None,
        memory_threshold: Optional[float] = None,
        cooldown_seconds: Optional[float] = None,
        max_wait_seconds: Optional[float] = None,
    ):
        self.monitor = monitor or get_resource_monitor()
        self.cpu_threshold = cpu_threshold or settings.RESOURCE_CPU_THRESHOLD
        self.memory_threshold = memory_threshold or settings.RESOURCE_MEMORY_THRESHOLD
        self.cooldown_seconds = settings.RESOURCE_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
        self.max_wait_seconds = settings.RESOURCE_MAX_WAIT_SECONDS if max_wait_seconds is None else max_wait_seconds
        self._last_test_end: Optional[float] = None

    def note_run_finished(self) -> None:
        """A JMeter process just ended; the cooldown counts from now."""
        self._last_test_end = time.monotonic()

    def cooldown_remaining(self) -> float:
        if self._last_test_end is None:
            return 0.0
        return max(0.0, self.cooldown_seconds - (time.monotonic() - self._last_test_end))

    def check_readiness(self) -> ReadinessCheck:
        """Evaluate whether the system is ready to start a test.

        Returns a ReadinessCheck with a decision and explanation.
        """
        snap = self.monitor.snapshot()
        history = self.monitor.get_history(6)
        cpu_trend = [s.cpu_percent for s in history] if history else [snap.cpu_percent]
        cooldown_remaining = self.cooldown_remaining()

        issues = []
        cpu_issue = snap.cpu_percent > self.cpu_threshold
        mem_issue = snap.memory_percent > self.memory_threshold
        cooldown_issue = cooldown_remaining > 0

        cpu_severity = (
            "critical" if snap.cpu_percent > 95
            else "high" if snap.cpu_percent > self.cpu_threshold + 10
            else "borderline" if snap.cpu_percent > self.cpu_threshold - 5
            else "ok"
        )

        if cpu_severity == "critical":
            decision = SchedulerDecision.OVERLOADED
            issues.append(f"CPU critically high: {snap.cpu_percent:.1f}%")
        elif cpu_issue and mem_issue:
            decision = SchedulerDecision.WAIT
            issues.append(f"CPU {snap.cpu_percent:.1f}% > {self.cpu_threshold}%")
            issues.append(f"Memory {snap.memory_percent:.1f}% > {self.memory_threshold}%")
        elif cpu_issue:
            decision = SchedulerDecision.WAIT if cpu_severity == "high" else SchedulerDecision.WARNING
            issues.append(f"CPU {snap.cpu_percent:.1f}% > {self.cpu_threshold}%")
        elif mem_issue:
            decision = SchedulerDecision.WAIT
            issues.append(f"Memory {snap.memory_percent:.1f}% > {self.memory_threshold}%")
        elif cooldown_issue:
            decision = SchedulerDecision.WAIT
            issues.append(f"Cooldown: {cooldown_remaining:.0f}s remaining")
        elif cpu_severity == "borderline":
            decision = SchedulerDecision.WARNING
            issues.append(f"CPU borderline: {snap.cpu_percent:.1f}%")
        else:
            decision = SchedulerDecision.PROCEED

        if decision in (SchedulerDecision.WAIT, SchedulerDecision.OVERLOADED):
            suggested_wait = max(cooldown_remaining, 15.0)
            if cpu_issue:
                suggested_wait = max(suggested_wait, 30.0)
        elif decision == SchedulerDecision.WARNING:
            suggested_wait = max(cooldown_remaining, 5.0)
        else:
            suggested_wait = cooldown_remaining

        message = (
            "Ready to proceed."
            if decision == SchedulerDecision.PROCEED
            else f"Not ready: {'; '.join(issues)}"
        )

        return ReadinessCheck(
            decision=decision,
            snapshot=snap,
            message=message,
            suggested_wait_seconds=suggested_wait,
            cpu_trend=cpu_trend,
        )

    async def hold_until_ready(self) -> Optional[str]:
        """Wait out the cooldown and a busy machine; why the run must not start, or None once it may.

        A critically busy machine refuses at once. A busy one is waited on for ``max_wait_seconds``; a
        borderline one that stays borderline starts anyway, and the report is told how busy it was.
        """
        remaining = self.cooldown_remaining()
        if remaining > 0:
            logger.info('Waiting %.0fs for the load generator to settle after the previous run', remaining)
            await asyncio.sleep(remaining)
        readiness = await asyncio.to_thread(self.check_readiness)
        if readiness.decision == SchedulerDecision.OVERLOADED:
            return f"System overloaded: {readiness.message}"
        if readiness.decision in (SchedulerDecision.WAIT, SchedulerDecision.WARNING):
            safe = self.max_wait_seconds > 0 and await self.monitor.wait_until_safe(
                timeout=self.max_wait_seconds, check_interval=5.0)
            if not safe and readiness.decision == SchedulerDecision.WAIT:
                return f"Timed out waiting for resources to free up: {readiness.message}"
        return None


_scheduler: Optional[TestScheduler] = None


def get_scheduler() -> TestScheduler:
    """The process-wide scheduler: one cooldown clock for every way a run can start."""
    global _scheduler
    if _scheduler is None:
        _scheduler = TestScheduler()
    return _scheduler
