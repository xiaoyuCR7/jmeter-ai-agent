"""Agent tools for JMeter test execution and report analysis."""

import os
import json
import asyncio
import logging
from typing import Optional

from app.core.config import settings
from app.services.execution_lifecycle import execution_lifecycle
from app.services.jmeter.environment import ProductionRunNeedsConfirmation
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


async def _with_script_approval(run, tool_name: str, jmx_path: str, *args, **kwargs):
    """Start a run; a plan with scripts or outside file access needs a second, explicit user approval.

    Approving the tool call alone does not show what the plan does, so the user is asked
    again with the list. The approval covers exactly the bytes that were listed; the model
    can never grant it itself.
    """
    from app.services.confirmations import approved_details, confirmations
    for _ in range(3):  # Scripts, then a production environment: each asked once.
        try:
            return await run(jmx_path, *args, **kwargs)
        except ScriptApprovalRequired as error:
            shown = approved_details.get() or {}
            # The call's own confirmation listed these scripts for these exact bytes: that was the approval.
            if shown.get('plan_sha256') != error.sha256:
                decision = await confirmations.ask(tool_name, {'jmx_path': jmx_path},
                                                   {'runs_scripts_on_this_machine': error.findings})
                if decision != 'approved':
                    raise ValueError(f'{error} {_UNAPPROVED[decision]}') from error
            kwargs['approved_sha256'] = error.sha256
        except ProductionRunNeedsConfirmation as error:
            decision = await confirmations.ask(tool_name, {'jmx_path': jmx_path}, {
                'production_environment': error.environment, 'base_url': error.base_url, 'threads': error.threads})
            if decision != 'approved':
                raise ValueError(f'{error} {_UNAPPROVED[decision]}') from error
            kwargs['confirm_production'] = True
    return await run(jmx_path, *args, **kwargs)


_UNAPPROVED = {
    'denied': 'The user denied it; do not retry automatically.',
    'unanswered': 'Nobody answered in the JMeter Agent window in time; ask the user to open it, then try again.',
}


async def run_preview(args: dict) -> Optional[dict]:
    """The scripts a plan would run on this machine, listed in the run's own confirmation.

    Approving the call then approves those exact bytes, instead of a second dialog the user did not expect.
    """
    from app.core.config import safe_path, settings
    from app.services.jmeter.script_risk import content_hash, scan
    if settings.JMETER_EXECUTION_MODE != 'local':
        return None
    try:
        path = safe_path(str(args.get('jmx_path') or ''), allowed_dirs=[settings.TEST_PLANS_DIR])
        with open(path, 'rb') as stream:
            content = stream.read()
    except (OSError, ValueError):
        return None
    findings = scan(content, os.path.dirname(path))
    return {'runs_scripts_on_this_machine': findings, 'plan_sha256': content_hash(content)} if findings else None


def resource_hooks(skip_resource_check: bool, resource_info: dict, *, wait: bool = True):
    """before_execute and postprocess callbacks: record the machine's load and, with ``wait``,
    hold or stop the run until it is quiet."""
    from app.models.schemas import TestExecutionStatus

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
        if not wait:
            return
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

    return before_execute, postprocess


async def _start(tool_name: str, jmx_path: str, execution_id: str, **kwargs) -> dict:
    """Hand a run to the lifecycle and return; it outlives the request that started it."""
    try:
        execution = await _with_script_approval(execution_lifecycle.submit, tool_name, jmx_path, execution_id, **kwargs)
    except (ValueError, FileNotFoundError, FileExistsError) as error:
        return {"error": str(error), "execution_id": execution_id}
    return {"execution_id": execution.id, "test_plan_id": execution.test_plan_id, "status": execution.status.value,
            "next": "The run continues in the background. Call get_execution_status with this execution_id."}


async def start_test(jmx_path: str, execution_id: str = "", skip_resource_check: bool = False) -> dict:
    """Start a JMeter test plan in the background, after a check of the machine's load."""
    before_execute, postprocess = resource_hooks(skip_resource_check, {})
    return await _start('execute_test', jmx_path, execution_id, before_execute=before_execute, postprocess=postprocess)


async def get_execution_status(execution_id: str) -> dict:
    """Current state of one run; terminal once status is completed, failed or cancelled.

    Args:
        execution_id: The execution ID returned when the run started.
    """
    from app.services.storage import store
    execution = store.get_execution(execution_id)
    if execution is None:
        return {"error": f"Execution not found: {execution_id}", "execution_id": execution_id}
    return {
        "execution_id": execution.id, "status": execution.status.value, "progress": execution.progress,
        "start_time": execution.start_time.isoformat() if execution.start_time else None,
        "end_time": execution.end_time.isoformat() if execution.end_time else None,
        "report_dir": execution.report_dir, "log_path": execution.log_path,
        "error_message": execution.error_message, "report_status": execution.report_status,
        "report_error": execution.report_error,
        "summary": execution.summary.model_dump() if execution.summary else None,
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



def _distributed_engines(remote_engines: str):
    """The engines to use, or the error to return when there are none."""
    from app.services.jmeter.distributed_executor import dist_executor
    engines = [e.strip() for e in remote_engines.split(",") if e.strip()] if remote_engines else None
    engine_status = dist_executor.get_engine_status()
    if not engines and not engine_status["has_engines"]:
        return None, {
            "error": "No remote engines configured for distributed testing. "
                     "Set JMETER_REMOTE_ENGINES in settings or pass remote_engines.",
            "engine_status": engine_status,
        }
    return engines or dist_executor.get_engines(), None


async def start_distributed(jmx_path: str, execution_id: str = "", remote_engines: str = "") -> dict:
    """Start a run across the remote engines in the background."""
    engines, error = _distributed_engines(remote_engines)
    return error or await _start('execute_distributed', jmx_path, execution_id, remote_engines=engines)


async def get_remote_engines() -> dict:
    """Get the list of configured remote JMeter engines."""
    from app.services.jmeter.distributed_executor import dist_executor
    return dist_executor.get_engine_status()

# A tool call is bounded far below a load test's length, so a run is started and then polled.
_BACKGROUND_NOTE = (" Returns as soon as the run has started; it continues in the background. "
                    "Poll get_execution_status with the returned execution_id.")

jmeter_tools = [
    Tool(
        name="execute_test",
        description="Start a JMeter test plan (.jmx file)." + _BACKGROUND_NOTE,
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Absolute path to the .jmx file"},
                "execution_id": {"type": "string", "description": "Optional execution ID"},
            },
            "required": ["jmx_path"],
        },
        handler=start_test,
        risk='execute',
        preview=run_preview,
    ),
    Tool(
        name="get_execution_status",
        description="Get one test run's status, progress and, once finished, its summary and report paths.",
        parameters={
            "type": "object",
            "properties": {
                "execution_id": {"type": "string", "description": "The execution ID"},
            },
            "required": ["execution_id"],
        },
        handler=get_execution_status,
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
        name="execute_distributed",
        description="Start a JMeter test across multiple remote engines for high-scale distributed load testing "
                    "(JMeter's -R master and workers)." + _BACKGROUND_NOTE,
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Path to the .jmx file"},
                "execution_id": {"type": "string", "description": "Optional execution ID"},
                "remote_engines": {"type": "string", "description": "Comma-separated host:port list, e.g. 'slave1:1099,slave2:1099'. Uses config if empty."},
            },
            "required": ["jmx_path"],
        },
        handler=start_distributed,
        risk='execute',
        preview=run_preview,
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
