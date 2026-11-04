"""Agent tools for the test machine's resources, run comparison, OpenAPI import and anomaly detection."""

import asyncio
from app.core.config import settings, safe_path
from app.services.jmeter.resource_monitor import get_resource_monitor
from app.services.ai.agent.tool_registry import Tool


_monitor = get_resource_monitor()


async def check_system_resources() -> dict:
    """Check current CPU and memory usage of the test machine.

    Use this BEFORE executing any performance test to ensure the
    host machine is not already under high load, which would skew
    test results.

    Returns current CPU%, memory%, disk%, and a health assessment.
    """
    snap = await asyncio.to_thread(_monitor.snapshot)
    diagnosis = _monitor.diagnose(snap)
    return {
        **snap.to_dict(),
        "diagnosis": diagnosis,
        "thresholds": {
            "cpu_percent": settings.RESOURCE_CPU_THRESHOLD,
            "memory_percent": settings.RESOURCE_MEMORY_THRESHOLD,
        },
        "recommendation": (
            "System healthy — safe to run tests."
            if snap.is_healthy
            else f"System under load — consider waiting before starting tests. {diagnosis}"
        ),
    }


# ── SLA and Regression ──────────────────────────────────────────

async def compare_runs(
    baseline_jtl: str, target_jtl: str,
    baseline_name: str = "baseline", target_name: str = "current",
    regression_threshold_pct: float = 20.0,
) -> dict:
    """Compare two test runs and detect performance regressions.

    Args:
        baseline_jtl: Path to the baseline JTL result file.
        target_jtl: Path to the current JTL result file.
        baseline_name: Label for the baseline run.
        target_name: Label for the current run.
        regression_threshold_pct: Percentage change to flag as regression (default 20%).
    """
    from app.services.jmeter.report_parser import ReportParser
    from app.services.jmeter.regression_engine import RegressionEngine

    parser = ReportParser()
    engine = RegressionEngine(regression_threshold_pct=regression_threshold_pct)

    try:
        baseline_jtl = safe_path(baseline_jtl, [settings.REPORTS_DIR])
        target_jtl = safe_path(target_jtl, [settings.REPORTS_DIR])
        baseline = await asyncio.to_thread(parser.parse_jtl, baseline_jtl)
        target = await asyncio.to_thread(parser.parse_jtl, target_jtl)
    except Exception as e:
        return {"error": f"Failed to parse JTL files: {str(e)}"}

    report = engine.compare(baseline, target, baseline_name, target_name)
    return report.to_dict()



async def import_openapi_spec(url: str) -> dict:
    """Import an OpenAPI/Swagger specification as a new project with a checked baseline plan."""
    from app.services.openapi_importer import OpenAPIImporter
    result = await OpenAPIImporter().import_from_url(url)
    return result.model_dump()


# ── Anomaly Detection ──────────────────────────────────────

async def detect_anomalies(execution_id: str) -> dict:
    """Run ML anomaly detection on a test execution.

    Uses statistical methods (Z-score, IQR) to find performance spikes,
    throughput drops, and response time degradation patterns.

    Args:
        execution_id: The execution ID to analyze.
    """
    import os
    from app.core.config import settings
    from app.services.execution_lifecycle import validate_id
    validate_id(execution_id)
    jtl_path = safe_path(os.path.join(settings.REPORTS_DIR, execution_id, "result.jtl"), [settings.REPORTS_DIR])
    if not os.path.exists(jtl_path):
        return {"error": f"JTL not found for execution {execution_id}"}
    from app.services.jmeter.report_parser import ReportParser
    parser = ReportParser()
    summary = await asyncio.to_thread(parser.parse_jtl, jtl_path)
    from app.services.ml_analysis import ml_detector
    report = ml_detector.analyze_timeline(summary.timeline_segments)
    return report.model_dump()


resource_tools = [
    Tool(
        name="check_system_resources",
        description=(
            "Check current CPU, memory, and disk usage of the test machine. "
            "MUST call this BEFORE executing any performance test to ensure "
            "the host is not under load. Returns health assessment and recommendation."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=check_system_resources,
    ),
    Tool(
        name="compare_runs",
        description="Compare two test runs (baseline vs current) and detect performance regressions. Returns per-metric deltas, regression flags, and overall verdict.",
        parameters={
            "type": "object",
            "properties": {
                "baseline_jtl": {"type": "string", "description": "Path to the baseline JTL file"},
                "target_jtl": {"type": "string", "description": "Path to the current JTL file to compare"},
                "baseline_name": {"type": "string", "description": "Label for baseline (e.g. 'v1.0')"},
                "target_name": {"type": "string", "description": "Label for current (e.g. 'v1.1')"},
                "regression_threshold_pct": {"type": "number", "description": "Pct change to flag as regression (default 20)"},
            },
            "required": ["baseline_jtl", "target_jtl"],
        },
        handler=compare_runs,
    ),
# ── OpenAPI Import ─────────────────────────────────────────

    Tool(
        name="import_openapi_spec",
        description="Import supported OpenAPI/Swagger HTTP operations from a URL as a new project with a baseline "
                    "plan, checked and loaded by JMeter before it is saved. Supply real authentication tokens "
                    "separately; unsupported correlation is not inferred. Nothing runs; start a run afterwards.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to the OpenAPI JSON/YAML spec"},
            },
            "required": ["url"],
        },
        handler=import_openapi_spec,
        # It makes this machine fetch any URL the caller names, local services included: the user sees it first.
        risk='write',
    ),
    Tool(
        name="detect_anomalies",
        description="Run statistical anomaly detection on a test execution. Finds performance spikes, throughput drops, and degradation patterns using Z-score and IQR methods.",
        parameters={
            "type": "object",
            "properties": {
                "execution_id": {"type": "string", "description": "The execution ID to analyze"},
            },
            "required": ["execution_id"],
        },
        handler=detect_anomalies,
    ),
]
