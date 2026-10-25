"""Agent tools for JMX test plan file management."""

import os
import json
from typing import Optional

from app.core.config import settings
from app.services.ai.agent.tool_registry import Tool
from app.services.jmeter.jmx_generator import JMXGenerator
from app.services.jmeter.jmx_parser import parse_jmx, strip_source
from app.services.jmeter.jmx_builder import build_jmx
from app.models.schemas import (
    APIEndpoint, TestConfig, ExtractionRule, VariableBinding, ExtractionType
)


async def create_jmx(
    test_name: str,
    endpoints: list[dict],
    base_url: str,
    test_type: str = "baseline",
    concurrent_users: int = 10,
    duration_seconds: int = 300,
    ramp_up_seconds: int = 30,
    project_id: Optional[str] = None,
) -> dict:
    """Generate a JMeter .jmx test plan from endpoint definitions.

    Args:
        test_name: Name for the test plan.
        endpoints: List of endpoint dicts with keys: name, method, path, headers (optional), body (optional), query_params (optional).
        base_url: Base URL of the target system (e.g. https://httpbin.org).
        test_type: Test type: baseline, concurrency, ramp_up, stress, stability, spike, custom.
        concurrent_users: Number of concurrent users.
        duration_seconds: Test duration in seconds.
        ramp_up_seconds: Ramp-up time in seconds.
        project_id: Optional project ID for organizing files.
    """
    # Build APIEndpoint objects
    from app.services.execution_lifecycle import validate_id
    project_id = project_id or 'default'
    validate_id(project_id)
    eps = []
    for ep_data in endpoints:
        # Parse extraction rules
        extractions = []
        for ext in ep_data.get("extractions", []) or []:
            extractions.append(ExtractionRule(
                ref_name=ext.get("ref_name", ""),
                extract_type=ext.get("extract_type", "json_path"),
                expression=ext.get("expression", ""),
                default_value=ext.get("default_value", "NOT_FOUND"),
                match_number=ext.get("match_number", 1),
                template=ext.get("template"),
                target_field=ext.get("target_field"),
            ))

        # Parse variable bindings
        bindings = []
        for b in ep_data.get("bindings", []) or []:
            bindings.append(VariableBinding(
                variable_name=b.get("variable_name", ""),
                target_location=b.get("target_location", "header"),
                target_key=b.get("target_key", ""),
                template=b.get("template"),
            ))

        eps.append(APIEndpoint(
            name=ep_data.get("name", "Unnamed"),
            method=ep_data.get("method", "GET"),
            path=ep_data.get("path", "/"),
            description=ep_data.get("description"),
            headers=ep_data.get("headers", {}),
            query_params=ep_data.get("query_params", {}),
            body=ep_data.get("body"),
            content_type=ep_data.get("content_type", "application/json"),
            expected_status=ep_data.get("expected_status", 200),
            extractions=extractions,
            bindings=bindings,
            depends_on=ep_data.get("depends_on", []),
            order_hint=ep_data.get("order_hint"),
        ))

    config = TestConfig(
        test_type=test_type,
        concurrent_users=concurrent_users,
        duration_seconds=duration_seconds,
        ramp_up_seconds=ramp_up_seconds,
    )

    generator = JMXGenerator()
    jmx_content = generator.generate_basic_jmx(
        endpoints=eps,
        test_config=config,
        base_url=base_url,
        test_name=test_name,
    )

    import uuid
    plan_id = uuid.uuid4().hex
    jmx_path = generator.save_jmx(jmx_content, name=test_name, project_id=project_id,
                                  plan_id=plan_id)
    from app.services.execution_lifecycle import execution_lifecycle
    plan = await execution_lifecycle.register_path(
        jmx_path, project_id=project_id or 'default', name=test_name,
        test_config=config, endpoints_count=len(eps))

    return {
        "jmx_path": jmx_path,
        "test_plan_id": plan.id,
        "test_name": test_name,
        "endpoints_count": len(eps),
        "test_type": test_type,
        "concurrent_users": concurrent_users,
        "duration_seconds": duration_seconds,
        "base_url": base_url,
        "message": f"JMX test plan created at {jmx_path}",
    }


async def parse_jmx_tree(jmx_path: str) -> dict:
    """Parse a JMX file and return its tree structure.

    Args:
        jmx_path: Path to the .jmx file.
    """
    try:
        from app.core.config import safe_path
        jmx_path = safe_path(jmx_path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(jmx_path):
        return {"error": f"JMX file not found: {jmx_path}"}

    try:
        tree = strip_source(parse_jmx(jmx_path))
        return {"tree": tree}
    except Exception as e:
        return {"error": str(e)}


async def get_jmx_content(jmx_path: str) -> dict:
    """Read the raw XML content of a JMX file.

    Args:
        jmx_path: Path to the .jmx file.
    """
    try:
        from app.core.config import safe_path
        jmx_path = safe_path(jmx_path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(jmx_path):
        return {"error": f"JMX file not found: {jmx_path}"}

    with open(jmx_path, "r", encoding="utf-8") as f:
        content = f.read()

    return {
        "jmx_path": jmx_path,
        "content": content,
        "size_bytes": len(content),
    }


async def list_test_plans() -> dict:
    """List all saved JMX test plans."""
    if not os.path.exists(settings.TEST_PLANS_DIR):
        return {"test_plans": []}

    plans = []
    for item in os.listdir(settings.TEST_PLANS_DIR):
        item_path = os.path.join(settings.TEST_PLANS_DIR, item)
        if os.path.isdir(item_path):
            # New structure: test_plans/<project_id>/<file>.jmx
            for f in os.listdir(item_path):
                if f.endswith(".jmx"):
                    plans.append({
                        "name": f.replace(".jmx", ""),
                        "project_id": item,
                        "path": os.path.join(item_path, f),
                    })
        elif item.endswith(".jmx"):
            plans.append({
                "name": item.replace(".jmx", ""),
                "project_id": None,
                "path": item_path,
            })

    return {"test_plans": plans}


async def validate_jmx(jmx_path: str) -> dict:
    """Validate a JMX file by parsing it and checking structure.

    Args:
        jmx_path: Path to the .jmx file to validate.
    """
    try:
        from app.core.config import safe_path
        jmx_path = safe_path(jmx_path)
    except ValueError as e:
        return {"valid": False, "error": str(e)}

    if not os.path.exists(jmx_path):
        return {"valid": False, "error": f"File not found: {jmx_path}"}

    try:
        tree = parse_jmx(jmx_path)
        # Check for basic structure
        issues = []
        if not isinstance(tree, dict):
            issues.append("Root element is not a valid test plan object")

        # Check for ThreadGroup
        has_thread_group = False
        has_http_sampler = False

        def check_node(node):
            nonlocal has_thread_group, has_http_sampler
            if isinstance(node, dict):
                tc = node.get("testclass", "")
                if "ThreadGroup" in tc:
                    has_thread_group = True
                if "HTTPSampler" in tc:
                    has_http_sampler = True
                for child in node.get("children", []):
                    check_node(child)

        check_node(tree)

        if not has_thread_group:
            issues.append("Missing ThreadGroup element")
        if not has_http_sampler:
            issues.append("Missing HTTP Sampler (no HTTP requests defined)")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "has_thread_group": has_thread_group,
            "has_http_sampler": has_http_sampler,
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}


async def update_jmx(
    jmx_path: str,
    test_type: str = None,
    concurrent_users: int = None,
    duration_seconds: int = None,
    ramp_up_seconds: int = None,
    think_time_ms: int = None,
) -> dict:
    """Update parameters on an existing JMX file by parsing, modifying, and rebuilding.

    Args:
        jmx_path: Path to the .jmx file.
        test_type: New test type.
        concurrent_users: New concurrent user count.
        duration_seconds: New duration in seconds.
        ramp_up_seconds: New ramp-up time.
        think_time_ms: New think time.
    """
    try:
        from app.core.config import safe_path
        jmx_path = safe_path(jmx_path, allowed_dirs=[settings.TEST_PLANS_DIR])
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(jmx_path):
        return {"error": f"JMX file not found: {jmx_path}"}

    try:
        tree = parse_jmx(jmx_path)

        def _walk(node):
            """Yield all nodes in the JMX tree at any depth."""
            if isinstance(node, dict):
                yield node
                for c in node.get("children", []) or []:
                    yield from _walk(c)

        # ThreadGroups are nested under TestPlan — search recursively,
        # not just the top level.
        thread_groups = [
            n for n in _walk(tree) if "ThreadGroup" in n.get("testclass", "")
        ]
        if not thread_groups and any(
            v is not None for v in (concurrent_users, duration_seconds, ramp_up_seconds)
        ):
            return {"error": "No ThreadGroup found in JMX file"}

        applied: dict = {}
        skipped: list = []

        for tg in thread_groups:
            props = tg.get("properties", {})
            if concurrent_users is not None:
                if "ThreadGroup.num_threads" in props:
                    props["ThreadGroup.num_threads"] = str(concurrent_users)
                    applied["concurrent_users"] = concurrent_users
                else:
                    skipped.append("concurrent_users")
            if ramp_up_seconds is not None:
                if "ThreadGroup.ramp_time" in props:
                    props["ThreadGroup.ramp_time"] = str(ramp_up_seconds)
                    applied["ramp_up_seconds"] = ramp_up_seconds
                else:
                    skipped.append("ramp_up_seconds")
            if duration_seconds is not None:
                if "ThreadGroup.duration" in props:
                    props["ThreadGroup.duration"] = str(duration_seconds)
                    applied["duration_seconds"] = duration_seconds
                else:
                    skipped.append("duration_seconds")
                if "ThreadGroup.scheduler" in props:
                    props["ThreadGroup.scheduler"] = duration_seconds > 0

        # Think time lives in timer elements (ConstantTimer / *RandomTimer all
        # store the delay under the "ConstantTimer.delay" property in JMX).
        if think_time_ms is not None:
            timer_found = False
            for node in _walk(tree):
                if "Timer" in node.get("testclass", ""):
                    tprops = node.get("properties", {})
                    if "ConstantTimer.delay" in tprops:
                        tprops["ConstantTimer.delay"] = str(think_time_ms)
                        timer_found = True
            if timer_found:
                applied["think_time_ms"] = think_time_ms
            else:
                skipped.append("think_time_ms")

        # Rebuild XML
        new_xml = build_jmx(tree)
        os.makedirs(os.path.dirname(jmx_path), exist_ok=True)
        with open(jmx_path, "w", encoding="utf-8") as f:
            f.write(new_xml)

        result = {
            "jmx_path": jmx_path,
            "updated": True,
            "applied": applied,
            "skipped": sorted(set(skipped)),
        }
        if test_type is not None:
            # test_type changes the whole load profile (thread group type,
            # timers, durations) — it cannot be patched onto an existing plan.
            result["note"] = (
                f"test_type='{test_type}' was NOT applied: changing test type "
                "requires generating a new plan with create_jmx."
            )
        return result
    except Exception as e:
        return {"error": str(e)}


async def clone_jmx(jmx_path: str, new_name: str = "") -> dict:
    """Duplicate a test plan with a new name.

    Args:
        jmx_path: Path to the source .jmx file.
        new_name: Name for the cloned test plan.
    """
    try:
        from app.core.config import safe_path
        jmx_path = safe_path(jmx_path)
    except ValueError as e:
        return {"error": str(e)}

    if not os.path.exists(jmx_path):
        return {"error": f"JMX file not found: {jmx_path}"}

    import uuid
    if not new_name:
        new_name = f"clone_{os.path.basename(jmx_path)}"

    # Determine target path
    project_dir = os.path.dirname(jmx_path)
    new_path = os.path.join(project_dir, f"{new_name}.jmx")

    from pathlib import Path
    from app.core.config import safe_path
    try:
        new_path = safe_path(new_path, allowed_dirs=[settings.TEST_PLANS_DIR])
        new_path = safe_path(new_path, allowed_dirs=[project_dir])
        if os.path.exists(new_path):
            new_path = safe_path(os.path.join(project_dir, f"{new_name}_{str(uuid.uuid4())[:6]}.jmx"),
                                 allowed_dirs=[project_dir])
        source_content = Path(jmx_path).read_bytes()
        with open(new_path, "xb") as dst:
            dst.write(source_content)
    except (ValueError, FileExistsError) as error:
        return {"error": str(error)}
    from app.services.execution_lifecycle import execution_lifecycle
    source_plan = await execution_lifecycle.register_path(jmx_path)
    plan = await execution_lifecycle.register_path(
        new_path, project_id=source_plan.project_id, name=new_name,
        test_config=source_plan.test_config, endpoints_count=source_plan.endpoints_count)

    return {
        "source_path": jmx_path,
        "cloned_path": new_path,
        "test_plan_id": plan.id,
        "message": f"Test plan cloned to {new_path}",
    }


async def import_jmx(source_path: str, project_id: str = "default",
                     target_name: str = "") -> dict:
    """Import an externally created JMX file into the project's test_plans directory.

    Args:
        source_path: Absolute path to the .jmx file on disk.
        project_id: Target project ID.
        target_name: Name for the imported test plan.
    """
    from app.core.config import safe_path
    from app.services.execution_lifecycle import execution_lifecycle, validate_id
    try:
        validate_id(project_id)
        source_path = safe_path(source_path)
        name = target_name or os.path.splitext(os.path.basename(source_path))[0]
        target_dir = os.path.join(settings.TEST_PLANS_DIR, project_id)
        target_path = safe_path(os.path.join(target_dir, f"{name}.jmx"), allowed_dirs=[settings.TEST_PLANS_DIR])
        target_path = safe_path(target_path, allowed_dirs=[target_dir])
    except ValueError as error:
        return {"error": str(error)}

    if not os.path.exists(source_path):
        return {"error": f"Source file not found: {source_path}"}

    if not source_path.endswith(".jmx"):
        return {"error": "File must be a .jmx file"}

    try:
        # Validate the source before creating a destination; existing files are
        # never overwritten or deleted on validation failure.
        parse_jmx(source_path)
    except Exception as error:
        return {"error": f"Invalid JMX import: {error}"}
    from pathlib import Path
    try:
        source_content = Path(source_path).read_bytes()
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "xb") as destination:
            destination.write(source_content)
    except (ValueError, FileExistsError) as error:
        return {"error": str(error)}
    plan = await execution_lifecycle.register_path(target_path, project_id=project_id, name=name)

    return {
        "source_path": source_path,
        "imported_path": target_path,
        "test_plan_id": plan.id,
        "project_id": project_id,
        "message": f"JMX file imported to {target_path}",
    }


# Tool definitions
jmx_tools = [
    Tool(
        name="create_jmx",
        description="Generate a JMeter .jmx test plan file from API endpoint definitions with dependency-aware ordering and variable extraction support. Creates a fully functional test plan with thread groups, HTTP samplers, assertions, extractors, and listeners.",
        parameters={
            "type": "object",
            "properties": {
                "test_name": {"type": "string", "description": "Name for the test plan"},
                "endpoints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Endpoint name"},
                            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"], "description": "HTTP method"},
                            "path": {"type": "string", "description": "URL path, e.g. /api/users"},
                            "headers": {"type": "object", "description": "Request headers"},
                            "query_params": {"type": "object", "description": "Query parameters"},
                            "body": {"type": "object", "description": "Request body (JSON)"},
                            "content_type": {"type": "string", "description": "Content-Type header"},
                            "depends_on": {
                                "type": "array", "items": {"type": "string"},
                                "description": "Names of upstream endpoints this one depends on (e.g., ['Login'])"
                            },
                            "order_hint": {"type": "integer", "description": "Execution order hint (lower = earlier)"},
                            "extractions": {
                                "type": "array", "items": {
                                    "type": "object",
                                    "properties": {
                                        "ref_name": {"type": "string", "description": "Variable name, e.g. auth_token"},
                                        "extract_type": {"type": "string", "enum": ["json_path", "regex", "boundary", "xpath", "header"], "description": "Extraction method"},
                                        "expression": {"type": "string", "description": "Extraction expression. JSON path: $.data.token. Regex: \"token\":\"([^\"]+)\". Boundary: left=access_token\"|\"right=\"\""},
                                        "default_value": {"type": "string", "description": "Default if extraction fails"},
                                        "match_number": {"type": "integer", "description": "Which match to use (0=random, 1=first)"},
                                        "template": {"type": "string", "description": "Template for regex groups, e.g. $1$"},
                                        "target_field": {"type": "string", "description": "Target field for header extractions (Set-Cookie, Authorization, etc.)"},
                                    },
                                    "required": ["ref_name", "extract_type", "expression"],
                                },
                                "description": "Variables to extract from this endpoint's response"
                            },
                            "bindings": {
                                "type": "array", "items": {
                                    "type": "object",
                                    "properties": {
                                        "variable_name": {"type": "string", "description": "Variable to inject (must match an extraction ref_name from upstream)"},
                                        "target_location": {"type": "string", "enum": ["header", "path", "query", "body", "url"], "description": "Where to inject the variable"},
                                        "target_key": {"type": "string", "description": "Key name: header name, query param name, body field name, or URL path placeholder"},
                                        "template": {"type": "string", "description": "Custom template. Default: ${variable_name}. Example: 'Bearer ${auth_token}'"},
                                    },
                                    "required": ["variable_name", "target_location"],
                                },
                                "description": "Variable bindings to inject into this request from upstream extractions"
                            },
                        },
                        "required": ["name", "method", "path"],
                    },
                    "description": "List of API endpoints to test, with optional dependency and extraction metadata",
                },
                "base_url": {"type": "string", "description": "Base URL of the target system"},
                "test_type": {"type": "string", "enum": ["baseline", "concurrency", "ramp_up", "stress", "stability", "spike", "custom"], "description": "Type of performance test"},
                "concurrent_users": {"type": "integer", "description": "Number of concurrent users (default 10)"},
                "duration_seconds": {"type": "integer", "description": "Test duration in seconds (default 300)"},
                "ramp_up_seconds": {"type": "integer", "description": "Ramp-up time in seconds (default 30)"},
                "project_id": {"type": "string", "description": "Optional project ID for organizing files"},
            },
            "required": ["test_name", "endpoints", "base_url"],
        },
        handler=create_jmx,
    ),
    Tool(
        name="parse_jmx_tree",
        description="Parse a JMX file and return its tree structure (test plan → thread groups → HTTP samplers → assertions).",
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Path to the .jmx file"},
            },
            "required": ["jmx_path"],
        },
        handler=parse_jmx_tree,
    ),
    Tool(
        name="get_jmx_content",
        description="Read the raw XML content of a JMX file.",
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Path to the .jmx file"},
            },
            "required": ["jmx_path"],
        },
        handler=get_jmx_content,
    ),
    Tool(
        name="list_test_plans",
        description="List all saved JMX test plans on disk.",
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=list_test_plans,
    ),
    Tool(
        name="validate_jmx",
        description="Validate a JMX file structure — checks for required elements (ThreadGroup, HTTP Sampler) and reports issues.",
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Path to the .jmx file to validate"},
            },
            "required": ["jmx_path"],
        },
        handler=validate_jmx,
    ),
    Tool(
        name="update_jmx",
        description="Update test parameters (concurrent users, duration, ramp-up, think time) on an existing JMX file.",
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Path to the .jmx file to update"},
                "test_type": {"type": "string", "enum": ["baseline", "concurrency", "ramp_up", "stress", "stability", "spike", "custom"], "description": "NOT applied by this tool — changing test type requires create_jmx on a new plan"},
                "concurrent_users": {"type": "integer", "description": "New concurrent user count"},
                "duration_seconds": {"type": "integer", "description": "New duration in seconds"},
                "ramp_up_seconds": {"type": "integer", "description": "New ramp-up time in seconds"},
                "think_time_ms": {"type": "integer", "description": "New think time in milliseconds"},
            },
            "required": ["jmx_path"],
        },
        handler=update_jmx,
    ),
    Tool(
        name="clone_jmx",
        description="Duplicate an existing test plan with a new name.",
        parameters={
            "type": "object",
            "properties": {
                "jmx_path": {"type": "string", "description": "Path to the source .jmx file"},
                "new_name": {"type": "string", "description": "Name for the cloned test plan"},
            },
            "required": ["jmx_path"],
        },
        handler=clone_jmx,
    ),
    Tool(
        name="import_jmx",
        description="Import an externally created JMX file into the project's test plans directory. The file is validated before import.",
        parameters={
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Absolute path to the .jmx file on disk"},
                "project_id": {"type": "string", "description": "Target project ID (default: 'default')"},
                "target_name": {"type": "string", "description": "Name for the imported test plan"},
            },
            "required": ["source_path"],
        },
        handler=import_jmx,
    ),
]
