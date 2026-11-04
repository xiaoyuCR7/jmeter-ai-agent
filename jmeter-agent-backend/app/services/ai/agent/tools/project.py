"""Agent tools for project and environment management."""

import os
import uuid
import json
from datetime import datetime
from typing import Optional

from app.core.config import settings
from app.services.ai.agent.tool_registry import Tool
from app.services.storage import store


def _as_strings(values: Optional[dict]) -> dict:
    """JMeter headers and variables are strings; models often send 1000 or false."""
    return {str(k): v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in (values or {}).items()}


async def create_project(name: str, description: str = "", base_url: str = "", headers: dict = None, variables: dict = None) -> dict:
    """Create a new test project.

    Args:
        name: Project name.
        description: Project description.
        base_url: Base URL for the project's API.
        headers: Global headers for all requests.
        variables: Project-level variables.
    """
    project_id = str(uuid.uuid4())[:8]
    now = datetime.now()

    from app.models.schemas import ProjectInfo, ProjectEnvironment

    env = ProjectEnvironment(
        base_url=base_url,
        headers=_as_strings(headers),
        variables=_as_strings(variables),
    )

    project = ProjectInfo(
        id=project_id,
        name=name,
        description=description,
        environment=env,
        created_at=now,
        updated_at=now,
    )

    # A new ID is create-only: collisions and policy failures must not publish
    # cache entries or silently turn an existing project into a new project.
    from app.services.db_store import db_store as _db
    await _db.create_project({
        "id": project_id,
        "name": name,
        "description": description,
        "base_url": base_url,
        "environment_data": env.model_dump(),
        "created_at": now,
    })

    # Create project directory
    project_dir = os.path.join(settings.TEST_PLANS_DIR, project_id)
    store.set_project(project_id, project)
    os.makedirs(project_dir, exist_ok=True)

    return {
        "project_id": project_id,
        "name": name,
        "base_url": base_url,
        "test_plans_dir": project_dir,
        "message": f"Project '{name}' created with ID {project_id}",
    }


async def list_projects() -> dict:
    """List the projects in the app, with how many test plans each has."""
    counts: dict[str, int] = {}
    for plan in store.test_plans.values():
        counts[plan.project_id] = counts.get(plan.project_id, 0) + 1
    return {"projects": [{"id": pid, "name": project.name, "description": project.description,
                          "test_plan_count": counts.get(pid, 0)} for pid, project in store.projects.items()]}


async def get_project_info(project_id: str) -> dict:
    """Get a project's environment and its test plans.

    Args:
        project_id: Project ID.
    """
    project = store.get_project(project_id)
    if project is None:
        return {"error": f"Project not found: {project_id}"}
    return {
        "id": project_id, "name": project.name, "description": project.description,
        "environment": project.environment.model_dump() if project.environment else {},
        "test_plans": [{"test_plan_id": plan.id, "name": plan.name, "jmx_path": plan.jmx_path}
                       for plan in store.test_plans.values() if plan.project_id == project_id],
    }


async def update_project_env(
    project_id: str,
    base_url: str = None,
    headers: dict = None,
    variables: dict = None,
) -> dict:
    """Update a project's environment configuration.

    Args:
        project_id: Project ID.
        base_url: New base URL.
        headers: New global headers.
        variables: New project variables.
    """
    if project_id not in store.projects:
        return {"error": f"Project not found: {project_id}"}

    proj = store.projects[project_id].model_copy(deep=True)
    if hasattr(proj, 'environment') and proj.environment:
        if base_url is not None:
            proj.environment.base_url = base_url
        if headers is not None:
            proj.environment.headers = _as_strings(headers)
        if variables is not None:
            proj.environment.variables = _as_strings(variables)
        proj.updated_at = datetime.now()
        from app.services.db_store import db_store as _db
        env_data = proj.environment.model_dump()
        persisted = await _db.update_project(project_id, {
            "base_url": env_data.get("base_url", ""),
            "environment_data": env_data,
            "storage_version": proj.storage_version,
        })
        if persisted is None:
            raise LookupError(f'Project not registered: {project_id}')
        proj.storage_version = persisted.get('storage_version', proj.storage_version)
        store.set_project(project_id, proj)

    return {
        "project_id": project_id,
        "base_url": base_url,
        "message": f"Environment updated for project {project_id}",
    }


# Tool definitions
project_tools = [
    Tool(
        name="create_project",
        description="Create a new test project with environment configuration.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "description": {"type": "string", "description": "Project description"},
                "base_url": {"type": "string", "description": "Base URL of the target API"},
                "headers": {"type": "object", "description": "Global request headers"},
                "variables": {"type": "object", "description": "Project-level variables"},
            },
            "required": ["name"],
        },
        handler=create_project,
        risk='draft',
    ),
    Tool(
        name="list_projects",
        description="List all test projects.",
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=list_projects,
    ),
    Tool(
        name="get_project_info",
        description="Get detailed info about a specific project.",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
        handler=get_project_info,
    ),
    Tool(
        name="update_project_env",
        description="Update a project's environment (base URL, headers, variables).",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "base_url": {"type": "string", "description": "New base URL"},
                "headers": {"type": "object", "description": "New global headers"},
                "variables": {"type": "object", "description": "New project variables"},
            },
            "required": ["project_id"],
        },
        handler=update_project_env,
        risk='write',
    ),
]
