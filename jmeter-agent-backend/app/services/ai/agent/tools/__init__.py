"""Built-in agent tools for JMeter operations."""

from app.services.ai.agent.tool_registry import Tool
from app.services.ai.agent.tools.jmeter import jmeter_tools
from app.services.ai.agent.tools.jmx import jmx_tools
from app.services.ai.agent.tools.project import project_tools
from app.services.ai.agent.tools.resource import resource_tools
from app.services.ai.agent.tools.evidence import evidence_tools


def get_all_builtin_tools() -> list[Tool]:
    """Get all built-in tools as a flat list."""
    return jmeter_tools + jmx_tools + project_tools + resource_tools + evidence_tools


__all__ = [
    "get_all_builtin_tools",
    "jmeter_tools",
    "jmx_tools",
    "project_tools",
    "resource_tools",
    "evidence_tools",
]
