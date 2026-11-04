"""Distributed test execution — master-slave JMeter support.

Extends JMeterExecutor with remote engine coordination.
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional, Callable

from app.core.config import settings
from app.models.schemas import TestExecutionInfo, TestExecutionStatus
from app.services.jmeter.executor import JMeterExecutor, get_executor

logger = logging.getLogger(__name__)


class DistributedExecutor:
    """Coordinates distributed JMeter test execution across remote engines.

    Wraps JMeterExecutor and adds:
    - Remote engine configuration
    - Distributed command building (-R flag)
    - Engine health validation
    - Result merging advice
    """

    def __init__(self, executor: Optional[JMeterExecutor] = None):
        self._executor = executor

    @property
    def executor(self) -> JMeterExecutor:
        """Lazily resolved via the shared singleton so module-level
        instantiation never probes the filesystem for the JMeter binary."""
        if self._executor is None:
            self._executor = get_executor()
        return self._executor

    def get_engines(self) -> list[str]:
        """Return configured remote engines from settings."""
        raw = settings.JMETER_REMOTE_ENGINES
        if not raw:
            return []
        engines = [e.strip() for e in raw.split(",") if e.strip()]
        return engines[: settings.JMETER_DISTRIBUTED_MAX_ENGINES]

    async def execute(
        self,
        jmx_path: str,
        execution_id: str = "",
        remote_engines: list[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> TestExecutionInfo:
        """Execute a test across remote engines.

        Args:
            jmx_path: Path to the .jmx file.
            execution_id: Unique ID (auto-generated if empty).
            remote_engines: Override configured engines. Uses settings if None.
            progress_callback: Progress update callback.
        """
        import uuid
        if not execution_id:
            execution_id = uuid.uuid4().hex  # The same form as every other run id.

        engines = remote_engines or self.get_engines()
        if not engines:
            raise ValueError(
                "No remote engines configured. Set JMETER_REMOTE_ENGINES in config "
                "or pass remote_engines parameter."
            )

        return await self.executor.execute(jmx_path, execution_id,
                                           progress_callback=progress_callback,
                                           remote_engines=engines)

    def get_engine_status(self) -> dict:
        """Return configured engine information for agent consumption."""
        engines = self.get_engines()
        return {
            "total_engines": len(engines),
            "max_engines": settings.JMETER_DISTRIBUTED_MAX_ENGINES,
            "engines": engines,
            "has_engines": len(engines) > 0,
        }


# Module-level singleton
dist_executor = DistributedExecutor()
