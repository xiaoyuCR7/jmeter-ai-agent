"""Agent tools for JMeter test execution and report analysis."""

import os
import json
import asyncio
import logging

from app.core.config import settings
from app.services.execution_lifecycle import execution_lifecycle
from app.services.jmeter.script_risk import ScriptApprovalRequired
from app.services.jmeter.report_parser import ReportParser
from app.services.jmeter.test_scheduler import TestScheduler, SchedulerDecision
from app.services.jmeter.resource_monitor import get_resource_monitor
from app.services.ai.agent.tool_registry import Tool

logger = logging.getLogger(__name__)

# Executor is obtained lazily via get_executor() at call sites so importing
# this module never probes the filesystem for the JMeter binary.
_report_parser = ReportParser()
_scheduler = TestScheduler()


async def execute_with_script_approval(tool_name: str, jmx_path: str, *args, **kwargs):
    """Run through the lifecycle; a plan with scripts needs a second, explicit user approval.

    Approving the tool call alone does not show what the plan does, so the user is asked
    again with the list. The approval covers exactly the bytes that were listed; the model
    can never grant it itself.
    """
    try:
        return await execution_lifecycle.execute(jmx_path, *args, **kwargs)
    except ScriptApprovalRequired as error:
        from app.services.confirmations import confirmations
        if not await confirmations.request(tool_name, {'jmx_path': jmx_path,
                                                       'runs_scripts_on_this_machine': error.findings}):
            raise ValueError(f'{error} The user did not approve it; do not retry automatically.') from error
        approved = error.sha256
    return await execution_lifecycle.execute(jmx_path, *args, approved_sha256=approved, **kwargs)


async def execute_test(jmx_path: str, execution_id: str = "",
                       skip_resource_check: bool = False) -> dict:
    """Execute a JMeter test plan with resource-aware scheduling.

    Before execution, checks CPU/memory usage. If the host machine is
    under high load, waits for resources to free up (up to configurable
    timeout). This ensures performance test results are not skewed by
    the test machine's own resource contention.

    Args:
        jmx_path: Absolute path to the .jmx file.
        execution_id: Optional execution ID (auto-generated if empty).
        skip_resource_check: If True, skip the resource check and run immediately.
    """
    from app.models.schemas import TestExecutionStatus
    resource_info = {}

    async def before_execute(execution):
        if skip_resource_check:
            return
        readiness = _scheduler.check_readiness()
        from dataclasses import asdict
        execution.resource_snapshot_pre = asdict(readiness.snapshot)
        resource_info.update(pre_test_cpu=readiness.snapshot.cpu_percent,
                             pre_test_memory=readiness.snapshot.memory_percent,
                             readiness_decision=readiness.decision.value,
                             readiness_message=readiness.message)
        if readiness.decision == SchedulerDecision.OVERLOADED:
            execution.status = TestExecutionStatus.CANCELLED
            execution.error_message = f"System overloaded: {readiness.message}"
            return
        if readiness.decision in (SchedulerDecision.WAIT, SchedulerDecision.WARNING):
            safe = await get_resource_monitor().wait_until_safe(
                timeout=settings.RESOURCE_MAX_WAIT_SECONDS, check_interval=5.0)
            resource_info['waited_for_resources'] = True
            if not safe and readiness.decision == SchedulerDecision.WAIT:
                execution.status = TestExecutionStatus.CANCELLED
                execution.error_message = 'Timed out waiting for resources to free up'

    async def postprocess(execution):
        from dataclasses import asdict
        snapshot = get_resource_monitor().snapshot()
        execution.resource_snapshot_post = asdict(snapshot)
        resource_info.update(post_test_cpu=snapshot.cpu_percent, post_test_memory=snapshot.memory_percent)

    try:
        result = await execute_with_script_approval(
            'execute_test', jmx_path, execution_id, before_execute=before_execute, postprocess=postprocess)
    except (ValueError, FileNotFoundError, FileExistsError) as error:
        return {"error": str(error), "execution_id": execution_id}
    return {
        "execution_id": result.id, "test_plan_id": result.test_plan_id,
        "success": result.status == TestExecutionStatus.COMPLETED,
        "status": result.status.value,
        "start_time": result.start_time.isoformat() if result.start_time else None,
        "end_time": result.end_time.isoformat() if result.end_time else None,
        "report_dir": result.report_dir, "log_path": result.log_path,
        "error_message": result.error_message,
        "report_status": result.report_status, "report_error": result.report_error,
        "resource_info": resource_info,
    }


async def parse_jtl_report(jtl_path: str) -> dict:
    """Parse a JMeter JTL result file and return detailed test summary.

    Args:
        jtl_path: Path to the .jtl file (e.g., reports/<execution_id>/result.jtl).
    """
    try:
        from app.core.config import safe_path
        jtl_path = safe_path(jtl_path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(jtl_path):
        return {"error": f"JTL file not found: {jtl_path}"}

    try:
        summary = await asyncio.to_thread(_report_parser.parse_jtl, jtl_path)
        return summary.model_dump()
    except Exception as e:
        return {"error": str(e)}


async def read_test_log(log_path: str, lines: int = 100) -> dict:
    """Read the tail of a JMeter test log file.

    Args:
        log_path: Path to the log file.
        lines: Number of lines to read from the end.
    """
    try:
        from app.core.config import safe_path
        log_path = safe_path(log_path)
    except ValueError as e:
        return {"error": str(e), "log": ""}

    if not os.path.exists(log_path):
        return {"error": f"Log file not found: {log_path}", "log": ""}

    content = _report_parser.read_log(log_path, lines)
    return {"log": content, "line_count": len(content.splitlines())}


async def get_html_report_path(report_dir: str) -> dict:
    """Get the HTML report path for a test execution.

    Args:
        report_dir: The report directory (e.g., reports/<execution_id>).
    """
    from app.core.config import safe_path
    try:
        report_dir = safe_path(report_dir, [settings.REPORTS_DIR])
        safe_path(os.path.join(report_dir, 'html', 'index.html'), [settings.REPORTS_DIR])
    except ValueError as error:
        return {"error": str(error), "html_report_path": None, "exists": False}
    html_path = _report_parser.get_html_report_url(report_dir)
    if html_path:
        return {"html_report_path": html_path, "exists": True}
    return {"html_report_path": None, "exists": False}


async def cancel_execution(execution_id: str) -> dict:
    """Cancel a running JMeter test execution.

    Args:
        execution_id: The execution ID to cancel.
    """
    success = await execution_lifecycle.cancel(execution_id)
    return {"cancelled": success, "execution_id": execution_id}


async def list_report_dir() -> dict:
    """List all test execution reports."""
    if not os.path.exists(settings.REPORTS_DIR):
        return {"reports": []}

    reports = []
    for item in os.listdir(settings.REPORTS_DIR):
        item_path = os.path.join(settings.REPORTS_DIR, item)
        if os.path.isdir(item_path):
            jtl_path = os.path.join(item_path, "result.jtl")
            has_jtl = os.path.exists(jtl_path)
            reports.append({
                "execution_id": item,
                "has_jtl": has_jtl,
                "modified": os.path.getmtime(item_path),
            })
    return {"reports": sorted(reports, key=lambda r: r["modified"], reverse=True)}



async def execute_distributed(jmx_path: str, execution_id: str = "",
                              remote_engines: str = "") -> dict:
    """Execute a JMeter test across multiple remote engines (distributed testing).

    Args:
        jmx_path: Path to the .jmx file.
        execution_id: Optional execution ID.
        remote_engines: Comma-separated "host:port" list. Uses config if empty.
    """
    import uuid
    if not execution_id:
        execution_id = str(uuid.uuid4())[:8]

    from app.services.jmeter.distributed_executor import dist_executor

    engines = None
    if remote_engines:
        engines = [e.strip() for e in remote_engines.split(",") if e.strip()]

    engine_status = dist_executor.get_engine_status()

    if not engines and not engine_status["has_engines"]:
        return {
            "error": "No remote engines configured for distributed testing. "
                     "Set JMETER_REMOTE_ENGINES in settings or pass remote_engines.",
            "engine_status": engine_status,
        }

    try:
        result = await execute_with_script_approval('execute_distributed', jmx_path, execution_id,
                                                     remote_engines=engines or dist_executor.get_engines())
        return {
            "execution_id": execution_id,
            "status": result.status.value,
            "start_time": result.start_time.isoformat() if result.start_time else None,
            "end_time": result.end_time.isoformat() if result.end_time else None,
            "report_dir": result.report_dir,
            "log_path": result.log_path,
            "error_message": result.error_message,
            "engines_used": engines or dist_executor.get_engines(),
            "total_engines": len(engines or dist_executor.get_engines()),
        }
    except Exception as e:
        return {"error": str(e), "execution_id": execution_id}


async def get_remote_engines() -> dict:
    """Get the list of configured remote JMeter engines."""
    from app.services.jmeter.distributed_executor import dist_executor
    return dist_executor.get_engine_status()

# Tool definitions
jmeter_tools = [
    Tool(
        name="execute_test",
        description="Execute a JMeter test plan (.jmx file) and return execution status.",
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Absolute path to the .jmx file"},
                "execution_id": {"type": "string", "description": "Optional execution ID"},
            },
            "required": ["jmx_path"],
        },
        handler=execute_test,
        risk='execute',
    ),
    Tool(
        name="parse_jtl_report",
        description="Parse a JMeter JTL result file and get detailed performance summary including response times, throughput, error rate, percentiles, and timeline analysis.",
        parameters={
            "type": "object",
            "properties": {
                "jtl_path": {"type": "string", "description": "Path to the .jtl file, e.g. reports/<execution_id>/result.jtl"},
            },
            "required": ["jtl_path"],
        },
        handler=parse_jtl_report,
    ),
    Tool(
        name="read_test_log",
        description="Read the last N lines of a JMeter test log file to check for errors.",
        parameters={
            "type": "object",
            "properties": {
                "log_path": {"type": "string", "description": "Path to the jmeter.log file"},
                "lines": {"type": "integer", "description": "Number of lines to read (default 100)"},
            },
            "required": ["log_path"],
        },
        handler=read_test_log,
    ),
    Tool(
        name="get_html_report_path",
        description="Check if an HTML report exists for a test execution and get its path.",
        parameters={
            "type": "object",
            "properties": {
                "report_dir": {"type": "string", "description": "Report directory path, e.g. reports/<execution_id>"},
            },
            "required": ["report_dir"],
        },
        handler=get_html_report_path,
    ),
    Tool(
        name="cancel_execution",
        description="Cancel a running JMeter test.",
        parameters={
            "type": "object",
            "properties": {
                "execution_id": {"type": "string", "description": "The execution ID to cancel"},
            },
            "required": ["execution_id"],
        },
        handler=cancel_execution,
        risk='write',
    ),
    Tool(
        name="list_report_dir",
        description="List all test execution reports and their status.",
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=list_report_dir,
    ),    Tool(
        name="execute_distributed",
        description="Execute a JMeter test across multiple remote engines for high-scale distributed load testing. Uses the -R flag to coordinate master-slave execution.",
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Path to the .jmx file"},
                "execution_id": {"type": "string", "description": "Optional execution ID"},
                "remote_engines": {"type": "string", "description": "Comma-separated host:port list, e.g. 'slave1:1099,slave2:1099'. Uses config if empty."},
            },
            "required": ["jmx_path"],
        },
        handler=execute_distributed,
        risk='execute',
    ),
    Tool(
        name="get_remote_engines",
        description="List configured remote JMeter engines available for distributed testing.",
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=get_remote_engines,
    ),
]
