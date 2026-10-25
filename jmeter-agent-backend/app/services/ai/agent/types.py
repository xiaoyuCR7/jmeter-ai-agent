"""Shared types for the agent framework."""

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable
from enum import Enum
from datetime import datetime


class AgentState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    REFLECTING = "reflecting"
    WAITING = "waiting"  # waiting for user input
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCategory(str, Enum):
    """Classifies tool errors so the planner can decide: retry, fix input, skip, or abort."""
    RETRYABLE = "retryable"        # transient: timeout, rate-limit, connection refused
    INPUT_ERROR = "input_error"    # bad args: file not found, invalid format, missing field
    SYSTEM_ERROR = "system_error"  # env issue: JMeter not installed, disk full, permission denied
    TOOL_ERROR = "tool_error"      # bug in the tool itself: unexpected exception shape
    CANCELLED = "cancelled"        # user cancelled mid-execution


@dataclass
class AgentEvent:
    """Event emitted during agent execution (for SSE streaming)."""
    type: str  # "step_start", "step_complete", "tool_call", "tool_result", "reflect", "error", "complete"
    message: str
    data: Optional[dict] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ToolResult:
    """Result from a tool execution."""
    success: bool
    data: Any = None
    error: Optional[str] = None
    tool_name: str = ""
    duration_ms: float = 0.0
    error_category: Optional[ErrorCategory] = None  # classified error for smart replanning

    def to_message(self) -> str:
        """Human-readable tool result for conversation memory."""
        if self.success:
            data_str = str(self.data)
            if len(data_str) > 1200:
                data_str = data_str[:1200] + "...(truncated)"
            return f"Tool '{self.tool_name}' succeeded. Result:\n{data_str}"
        return f"Tool '{self.tool_name}' failed: {self.error}"

    def to_json(self) -> str:
        """Compact JSON for ReAct loop feedback. Data truncated to avoid context pollution."""
        payload = {
            "tool": self.tool_name,
            "success": self.success,
            "error": self.error,
            "error_category": self.error_category.value if self.error_category else None,
        }
        if self.data is not None:
            payload["data"] = _compact_tool_data(self.data)
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


def _compact_tool_data(value, budget=1800, depth=0):
    """Bound observations while preserving structured IDs/status before bulky logs."""
    import math
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value if len(value) <= budget else value[:max(0, budget - 16)] + '...[truncated]'
    if depth > 5:
        return '[truncated]'
    if isinstance(value, dict):
        priority = ('id', 'status', 'success', 'error', 'project_id', 'test_plan_id', 'execution_id',
                    'jmx_path', 'report_dir', 'log_path', 'summary')
        keys = sorted(value, key=lambda key: (str(key) not in priority, str(key) not in ('name', 'path')))
        result = {}
        remaining = budget
        for key in keys:
            if remaining < len(str(key)) + 40:
                result['_truncated'] = True
                break
            # Priority keys are already first, so a value may use what is left. A fixed per-key cap
            # cut a 4-item requirements draft to 3 items, and the model then made up the 4th.
            item = _compact_tool_data(value[key], remaining - len(str(key)) - 10, depth + 1)
            result[str(key)] = item
            remaining -= len(json.dumps({str(key): item}, ensure_ascii=False, default=str))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        remaining = budget
        for item in value[:10]:
            if remaining < 40:
                break
            compact = _compact_tool_data(item, min(500, remaining), depth + 1)
            result.append(compact)
            remaining -= len(json.dumps(compact, ensure_ascii=False, default=str))
        if len(result) < len(value):
            result.append('[truncated]')
        return result
    return _compact_tool_data(str(value), budget, depth)


@dataclass
class PlanStep:
    """Single step in a task plan."""
    step_id: int
    description: str
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    expected_outcome: str = ""
    success: bool = False
    result: Optional[ToolResult] = None
    retries: int = 0
    max_retries: int = 2


@dataclass
class AgentResult:
    """Final result from an agent run."""
    success: bool
    task: str
    receipt: Optional[dict] = field(default=None, kw_only=True)
    plan: list[PlanStep] = field(default_factory=list)
    output: str = ""
    events: list[AgentEvent] = field(default_factory=list)
    iterations: int = 0
    error: Optional[str] = None
    error_traceback: Optional[str] = None
    duration_seconds: float = 0.0
    analysis: Optional[dict] = None


# Type alias for async tool handler
ToolHandler = Callable[..., Awaitable[Any]]


# ── Shared error classification ───────────────────────────────
import re as _re  # used by classify_error below

_RETRYABLE_PATTERNS = [
    r'timeout', r'timed\s*out', r'connection.*refused', r'connect.*refused',
    r'rate.*limit', r'too\s+many\s+requests', r'429', r'503', r'502', r'504',
    r'temporarily\s+unavailable', r'retry', r'busy', r'overloaded',
]
_INPUT_ERROR_PATTERNS = [
    r'not\s+found', r'does\s+not\s+exist', r'no\s+such\s+file', r'file\s+not\s+found',
    r'invalid', r'bad\s+request', r'malformed', r'400', r'validation\s+error',
    r'missing', r'required', r'cannot\s+be\s+empty', r'not\s+configured',
    r'unknown\s+tool', r'Tool\s+.*\s+not\s+found',
]
_SYSTEM_ERROR_PATTERNS = [
    r'permission\s+denied', r'access\s+denied', r'forbidden', r'403',
    r'disk\s+full', r'out\s+of\s+memory', r'cannot\s+allocate',
    r'not\s+installed', r'executable\s+not\s+found', r'command\s+not\s+found',
    r'jmeter.*not\s+found', r'no\s+space', r'read.only',
]


def classify_error(error_message: str) -> ErrorCategory:
    """Classify an error message into a category for smart replanning.

    Used by both tool_registry and BaseAgent to consistently categorize
    tool failures so the planner can make intelligent decisions.
    """
    if not error_message:
        return ErrorCategory.TOOL_ERROR
    msg_lower = error_message.lower()
    # Explicit fatal conditions must not be downgraded by generic words such
    # as "retry" or "not found" in the same message.
    for pat in _SYSTEM_ERROR_PATTERNS:
        if _re.search(pat, msg_lower):
            return ErrorCategory.SYSTEM_ERROR
    for pat in _RETRYABLE_PATTERNS:
        if _re.search(pat, msg_lower):
            return ErrorCategory.RETRYABLE
    for pat in _INPUT_ERROR_PATTERNS:
        if _re.search(pat, msg_lower):
            return ErrorCategory.INPUT_ERROR
    return ErrorCategory.TOOL_ERROR
