"""TestScheduler — resource-aware test execution orchestrator.

Ensures performance tests don't run when the host machine is under high load.
Between tests, enforces a configurable cooldown period to let the system
stabilize before the next test begins.

Two usage modes:

1. Direct API (programmatic):
       scheduler = TestScheduler(executor)
       result = await scheduler.run_one(jmx_path)

2. Agent-driven (AI decides):
       scheduler = TestScheduler(executor)
       decision = scheduler.check_readiness()  # gives snapshot + recommendation
       # agent reads decision, can wait, cancel, or proceed
       result = await scheduler.run_one(jmx_path)

3. Batch mode (run multiple tests sequentially):
       scheduler = TestScheduler(executor)
       results = await scheduler.run_batch([
           {"jmx_path": "path/to/baseline.jmx", "label": "baseline"},
           {"jmx_path": "path/to/concurrency.jmx", "label": "concurrency"},
       ])
"""

import os
import uuid
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import Enum

from app.core.config import settings
from app.services.jmeter.resource_monitor import (
    ResourceMonitor,
    ResourceSnapshot,
    get_resource_monitor,
)
from app.services.jmeter.executor import JMeterExecutor, get_executor
from app.models.schemas import TestExecutionInfo

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


@dataclass
class BatchItem:
    """A single test in a batch run."""
    jmx_path: str
    label: str = ""
    execution_id: str = ""


@dataclass
class BatchResult:
    """Result of a batch test run."""
    label: str
    jmx_path: str
    execution_id: str
    success: bool
    result: Optional[TestExecutionInfo] = None
    error: Optional[str] = None
    pre_snapshot: Optional[ResourceSnapshot] = None
    post_snapshot: Optional[ResourceSnapshot] = None


class TestScheduler:
    """Resource-aware test scheduler.

    Before each test:
    1. Take a resource snapshot
    2. Evaluate against thresholds
    3. If unsafe, wait with exponential backoff (up to max_wait)
    4. Run the test
    5. Enforce cooldown before next test
    """

    def __init__(
        self,
        executor: Optional[JMeterExecutor] = None,
        monitor: Optional[ResourceMonitor] = None,
        cpu_threshold: Optional[float] = None,
        memory_threshold: Optional[float] = None,
        cooldown_seconds: Optional[float] = None,
        max_wait_seconds: Optional[float] = None,
        max_concurrent_tests: Optional[int] = None,
    ):
        self._executor = executor
        self.monitor = monitor or get_resource_monitor()
        self.cpu_threshold = cpu_threshold or settings.RESOURCE_CPU_THRESHOLD
        self.memory_threshold = memory_threshold or settings.RESOURCE_MEMORY_THRESHOLD
        self.cooldown_seconds = settings.RESOURCE_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
        self.max_wait_seconds = settings.RESOURCE_MAX_WAIT_SECONDS if max_wait_seconds is None else max_wait_seconds
        self.max_concurrent_tests = max_concurrent_tests or settings.RESOURCE_MAX_CONCURRENT_TESTS

        self._last_test_end: float = 0.0
        self._active_test_count: int = 0
        self._test_semaphore = asyncio.Semaphore(self.max_concurrent_tests)

    @property
    def executor(self) -> JMeterExecutor:
        """Resolve the shared executor only when execution is requested."""
        if self._executor is None:
            self._executor = get_executor()
        return self._executor

    # ── readiness check ──────────────────────────────────────

    def check_readiness(self) -> ReadinessCheck:
        """Evaluate whether the system is ready to start a test.

        Returns a ReadinessCheck with a decision and explanation.
        The AI agent can read this to decide whether to proceed, wait, or abort.
        """
        snap = self.monitor.snapshot()
        history = self.monitor.get_history(6)
        cpu_trend = [s.cpu_percent for s in history] if history else [snap.cpu_percent]

        # Check cooldown
        now = time.monotonic()
        time_since_last = now - self._last_test_end
        cooldown_remaining = max(0, self.cooldown_seconds - time_since_last)

        issues = []
        cpu_issue = snap.cpu_percent > self.cpu_threshold
        mem_issue = snap.memory_percent > self.memory_threshold
        cooldown_issue = cooldown_remaining > 0

        # Determine severity
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

        # Calculate suggested wait time
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

    # ── single test execution ─────────────────────────────────

    async def run_one(
        self,
        jmx_path: str,
        execution_id: str = "",
        label: str = "",
        auto_wait: bool = True,
    ) -> BatchResult:
        """Run a single test with resource checks.

        Args:
            jmx_path: Path to the .jmx file.
            execution_id: Optional execution ID (auto-generated).
            label: Human-readable label for this test.
            auto_wait: If True, automatically wait when resources are high.
                       If False, raise an exception or return early.
        """
        if not os.path.exists(jmx_path):
            return BatchResult(
                label=label, jmx_path=jmx_path, execution_id=execution_id,
                success=False, error=f"JMX file not found: {jmx_path}",
            )

        if not execution_id:
            execution_id = str(uuid.uuid4())[:8]

        from app.models.schemas import TestExecutionStatus
        from app.services.execution_lifecycle import execution_lifecycle
        pre_snap = None
        acquired = False

        async def before_execute(execution):
            nonlocal pre_snap, acquired
            readiness = self.check_readiness()
            pre_snap = readiness.snapshot
            execution.resource_snapshot_pre = pre_snap.to_dict()
            if readiness.decision == SchedulerDecision.OVERLOADED:
                execution.status = TestExecutionStatus.CANCELLED
                execution.error_message = f"System overloaded: {readiness.message}"
                return
            if readiness.decision in (SchedulerDecision.WAIT, SchedulerDecision.WARNING):
                if not auto_wait:
                    execution.status = TestExecutionStatus.CANCELLED
                    execution.error_message = f"Resources not ready (auto_wait=False): {readiness.message}"
                    return
                safe = await self.monitor.wait_until_safe(timeout=self.max_wait_seconds, check_interval=5.0)
                if not safe and readiness.decision == SchedulerDecision.WAIT:
                    execution.status = TestExecutionStatus.CANCELLED
                    execution.error_message = f"Timed out waiting for resources: {readiness.message}"
                    return
            await self._test_semaphore.acquire()
            acquired = True
            self._active_test_count += 1

        async def postprocess(execution):
            execution.resource_snapshot_post = self.monitor.snapshot().to_dict()

        exec_error = None
        exec_result = None
        try:
            exec_result = await execution_lifecycle.execute(
                jmx_path, execution_id, before_execute=before_execute, postprocess=postprocess,
                executor=self._executor)
            success = exec_result.status == TestExecutionStatus.COMPLETED
        except Exception as error:
            success = False
            exec_error = str(error)
            logger.exception("[%s] Test execution failed", execution_id)
        finally:
            if acquired:
                self._active_test_count -= 1
                self._test_semaphore.release()

        self._last_test_end = time.monotonic()
        post_snap = self.monitor.snapshot()

        # Compute error message
        if success:
            error_msg = None
        elif exec_result and exec_result.error_message:
            error_msg = exec_result.error_message
        elif exec_error:
            error_msg = exec_error
        else:
            error_msg = "Unknown error"

        batch_result = BatchResult(
            label=label,
            jmx_path=jmx_path,
            execution_id=execution_id,
            success=success,
            result=exec_result,
            error=error_msg,
            pre_snapshot=pre_snap,
            post_snapshot=post_snap,
        )

        return batch_result

    # ── batch execution ───────────────────────────────────────

    async def run_batch(
        self,
        tests: list[dict],
        stop_on_failure: bool = False,
        auto_wait: bool = True,
        progress_callback: Optional[Callable[[int, int, BatchResult], None]] = None,
    ) -> list[BatchResult]:
        """Run multiple tests sequentially with resource checks between each.

        Args:
            tests: List of dicts with 'jmx_path' and optional 'label', 'execution_id'.
            stop_on_failure: If True, stop the batch when a test fails.
            auto_wait: Passed through to run_one.
            progress_callback: Called with (completed, total, last_result) after each test.

        Returns:
            List of BatchResult, one per test in input order.
        """
        results: list[BatchResult] = []
        total = len(tests)

        # Start background monitoring
        await self.monitor.start_monitoring(interval=5.0)

        try:
            for i, test in enumerate(tests):
                result = await self.run_one(
                    jmx_path=test["jmx_path"],
                    execution_id=test.get("execution_id", ""),
                    label=test.get("label", f"test_{i + 1}"),
                    auto_wait=auto_wait,
                )
                results.append(result)

                if progress_callback:
                    progress_callback(i + 1, total, result)

                if not result.success and stop_on_failure:
                    logger.warning(
                        f"Batch stopped after failure at test {i + 1}/{total}: "
                        f"{result.label}"
                    )
                    # Mark remaining tests as skipped
                    for j in range(i + 1, total):
                        t = tests[j]
                        results.append(BatchResult(
                            label=t.get("label", f"test_{j + 1}"),
                            jmx_path=t["jmx_path"],
                            execution_id="",
                            success=False,
                            error="Skipped: previous test failed",
                        ))
                    break

                # Enforce cooldown between tests
                if i < total - 1:
                    await asyncio.sleep(self.cooldown_seconds)
        finally:
            await self.monitor.stop_monitoring()

        return results

    # ── convenience helpers ───────────────────────────────────

    def get_status_summary(self) -> dict:
        """Return a summary of the scheduler's current state for agent consumption."""
        snap = self.monitor.latest or self.monitor.snapshot()
        readiness = self.check_readiness()
        cooldown_remaining = max(
            0, self.cooldown_seconds - (time.monotonic() - self._last_test_end)
        )

        return {
            "current_resources": snap.to_dict(),
            "readiness_decision": readiness.decision.value,
            "readiness_message": readiness.message,
            "cooldown_remaining_seconds": round(cooldown_remaining, 1),
            "suggested_wait_seconds": readiness.suggested_wait_seconds,
            "thresholds": {
                "cpu_percent": self.cpu_threshold,
                "memory_percent": self.memory_threshold,
                "cooldown_seconds": self.cooldown_seconds,
            },
        }
