"""Tool definition and registry — function-calling bridge to LLM tools API."""

import time
import copy
import json
from jsonschema import Draft202012Validator
from typing import Any, Literal, Optional, Callable, Awaitable
from dataclasses import dataclass, field

from app.core.config import settings

from app.services.ai.agent.types import ToolResult, ToolHandler, ErrorCategory, classify_error


# Room for confirmations and report parsing around a run.
LONG_RUN_MARGIN_SECONDS = 900
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
    long_running: bool = False  # may wait for a whole test run

    def __post_init__(self):
        self.parameters = copy.deepcopy(self.parameters)
        self.parameters.setdefault('type', 'object')
        self.parameters.setdefault('additionalProperties', False)

    @property
    def require_confirmation(self) -> bool:
        return self.risk in ('write', 'execute')

    @property
    def timeout_seconds(self) -> float:
        if not self.long_running:
            return settings.AGENT_TOOL_TIMEOUT_SECONDS
        return settings.JMETER_TIMEOUT + settings.RESOURCE_MAX_WAIT_SECONDS + LONG_RUN_MARGIN_SECONDS

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

    def to_anthropic_schema(self) -> dict:
        """Convert to Anthropic tool-use format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
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

    def get_anthropic_schemas(self) -> list[dict]:
        """Get all tool schemas in Anthropic format."""
        return [t.to_anthropic_schema() for t in self._tools.values()]

    def get_tools_description(self) -> str:
        """Compact tool summary for the planner prompt.

        The planner only needs to pick a tool and rough args — the ReAct
        executor re-sends full JSON schemas via native function calling and
        finalizes the arguments. So we render name + one-line description +
        param list (name:type, *=required, enums inlined) instead of dumping
        full indented JSON schemas (~10x fewer tokens).
        """
        lines = []
        for tool in self._tools.values():
            props = tool.parameters.get("properties", {}) or {}
            required = set(tool.parameters.get("required", []) or [])
            parts = []
            for pname, pschema in props.items():
                ptype = pschema.get("type", "any")
                enum = pschema.get("enum")
                if enum:
                    ptype = "|".join(str(e) for e in enum)
                mark = "*" if pname in required else ""
                parts.append(f"{pname}{mark}:{ptype}")
            param_str = ", ".join(parts) if parts else "none"
            short_desc = tool.description.split(". ")[0].split("\n")[0]
            lines.append(f"- {tool.name}: {short_desc}. params({param_str})")
        return "\n".join(lines)

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
            from app.services.confirmations import confirmation_source, confirmations
            confirmation_source.set(self.source)  # also covers approvals a handler asks for itself
            if tool.require_confirmation:
                decision = await confirmations.ask(name, args)
                if decision != 'approved':
                    return ToolResult(success=False, tool_name=name, error=DECLINED[decision],
                                      error_category=ErrorCategory.CANCELLED)
            result = await tool.handler(**args)
            duration = (time.time() - start) * 1000

            # Explicit business failures must not become successful observations.
            if isinstance(result, dict) and (
                result.get("error") or result.get("success") is False
                or result.get("valid") is False
                or result.get("status") in ("failed", "cancelled")
            ):
                error_msg = str(result.get("error") or result.get("error_message")
                                or f"Tool '{name}' reported an unsuccessful result")
                return ToolResult(
                    success=False,
                    data=result,
                    error=error_msg,
                    tool_name=name,
                    duration_ms=duration,
                    error_category=(ErrorCategory.CANCELLED if result.get("status") == "cancelled"
                                    else self._classify_error(error_msg)),
                )

            return ToolResult(
                success=True,
                data=result,
                tool_name=name,
                duration_ms=duration,
            )
        except TypeError as e:
            duration = (time.time() - start) * 1000
            return ToolResult(
                success=False,
                error=f"Tool '{name}' argument error: {str(e)}",
                tool_name=name,
                duration_ms=duration,
                error_category=ErrorCategory.INPUT_ERROR,
            )
        except Exception as e:
            duration = (time.time() - start) * 1000
            msg = str(e)
            return ToolResult(
                success=False,
                error=f"Tool '{name}' error: {msg}",
                tool_name=name,
                duration_ms=duration,
                error_category=self._classify_error(msg),
            )

    @staticmethod
    def _classify_error(error_message: str) -> ErrorCategory:
        """Delegate to shared classify_error in types.py."""
        return classify_error(error_message)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# Singleton registry for built-in tools
registry = ToolRegistry()
