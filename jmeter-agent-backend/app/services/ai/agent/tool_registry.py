"""Tool definition and registry — function-calling bridge to LLM tools API."""

import copy
import inspect
import time
import json
from jsonschema import Draft202012Validator
from typing import Any, Literal, Optional, Callable, Awaitable
from dataclasses import dataclass, field

from app.core.config import settings

from app.services.ai.agent.types import (
    ToolResult, ToolHandler, ErrorCategory, classify_exception, reported_category,
)


DECLINED = {
    'denied': "The user denied this call; do not retry automatically",
    'unanswered': ("Nobody answered the confirmation in the JMeter Agent window. "
                   "Ask the user to open it, then try again"),
}


@dataclass
class Tool:
    """A callable tool that agents can use."""
    name: str
    description: str
    parameters: dict  # JSON Schema for the parameters
    handler: ToolHandler
    # read: no lasting effect. draft: adds something new, overwrites nothing.
    # write and execute: change or run existing things, so the user is asked first.
    risk: Literal['read', 'draft', 'write', 'execute'] = 'read'

    def __post_init__(self):
        self.parameters = copy.deepcopy(self.parameters)
        self.parameters.setdefault('type', 'object')
        self.parameters.setdefault('additionalProperties', False)

    @property
    def require_confirmation(self) -> bool:
        return self.risk in ('write', 'execute')

    @property
    def timeout_seconds(self) -> float:
        # No tool waits for a whole run: runs start in the background and are polled.
        return settings.AGENT_TOOL_TIMEOUT_SECONDS

    def to_openai_schema(self) -> dict:
        """Convert to OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }


class ToolRegistry:
    """Registry of tools available to agents."""

    def __init__(self, source: str = 'agent'):
        self.source = source  # who is calling, shown to the user with each confirmation
        self._tools: dict[str, Tool] = {}
        self._validators = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        Draft202012Validator.check_schema(tool.parameters)
        self._validators[tool.name] = Draft202012Validator(tool.parameters)
        self._tools[tool.name] = tool

    def register_many(self, tools: list[Tool]) -> None:
        """Register multiple tools."""
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get_openai_schemas(self) -> list[dict]:
        """Get all tool schemas in OpenAI format."""
        return [t.to_openai_schema() for t in self._tools.values()]

    def validate_arguments(self, name: str, args: Any) -> Optional[str]:
        """Validate protocol input without running a handler or requesting approval."""
        if name not in self._validators:
            return f"Unknown tool: {name}"
        try:
            json.dumps(args, allow_nan=False)
        except (ValueError, TypeError):
            return "Tool arguments must be finite JSON values"
        error = next(self._validators[name].iter_errors(args), None)
        return f"Invalid tool arguments: {error.message}" if error is not None else None

    async def execute(self, name: str, args: dict) -> ToolResult:
        """Execute a tool by name with arguments. Auto-classifies errors.

        Handles three error paths:
        1. Handler raises exception → caught, classified, wrapped as ToolResult
        2. Handler returns dict with "error" key → detected as soft failure
        3. Handler returns normal data → ToolResult(success=True)
        """
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {name}",
                tool_name=name,
                error_category=ErrorCategory.INPUT_ERROR,
            )

        start = time.time()
        try:
            # Freeze the approved invocation, reject hidden arguments before side effects.
            error = self.validate_arguments(name, args)
            if error is not None:
                return ToolResult(success=False, tool_name=name, error=error,
                                  error_category=ErrorCategory.INPUT_ERROR)
            args = json.loads(json.dumps(args, allow_nan=False))
            # Checked before asking for approval. A TypeError the handler raises later is its own bug.
            try:
                inspect.signature(tool.handler).bind(**args)
            except TypeError as e:
                return ToolResult(success=False, tool_name=name, error=f"Tool '{name}' argument error: {e}",
                                  duration_ms=(time.time() - start) * 1000,
                                  error_category=ErrorCategory.INPUT_ERROR)
            from app.services.confirmations import confirmation_source, confirmations
            confirmation_source.set(self.source)  # also covers approvals a handler asks for itself
            if tool.require_confirmation:
                decision = await confirmations.ask(name, args)
                if decision != 'approved':
                    return ToolResult(success=False, tool_name=name, error=DECLINED[decision],
                                      error_category=ErrorCategory.CANCELLED)
            result = await tool.handler(**args)
            duration = (time.time() - start) * 1000

            # Explicit business failures must not become successful observations. A run's status is the
            # outcome of a tool that ran it; a read tool reporting a failed or cancelled run read it fine.
            if isinstance(result, dict) and (
                result.get("error") or result.get("success") is False
                or result.get("valid") is False
                or (tool.risk != 'read' and result.get("status") in ("failed", "cancelled"))
            ):
                error_msg = str(result.get("error") or result.get("error_message")
                                or f"Tool '{name}' reported an unsuccessful result")
                return ToolResult(
                    success=False,
                    data=result,
                    error=error_msg,
                    tool_name=name,
                    duration_ms=duration,
                    error_category=(ErrorCategory.CANCELLED
                                    if tool.risk != 'read' and result.get("status") == "cancelled"
                                    else reported_category(result, error_msg)),
                )

            return ToolResult(
                success=True,
                data=result,
                tool_name=name,
                duration_ms=duration,
            )
        except Exception as e:
            duration = (time.time() - start) * 1000
            msg = str(e)
            return ToolResult(
                success=False,
                error=f"Tool '{name}' error: {msg}",
                tool_name=name,
                duration_ms=duration,
                error_category=classify_exception(e),
            )

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

