"""The app's HTTP API, one router per area; main.py mounts this one under /api/v1."""
from fastapi import APIRouter

from app.api import (analysis_routes, baseline_routes, data_routes, execution_routes, experiment_routes,
                     mcp_routes, memory_routes, model_routes, plan_routes, project_routes, requirement_routes,
                     system_routes, workflow_routes)

router = APIRouter()
for area in (model_routes, plan_routes, execution_routes, experiment_routes, project_routes, workflow_routes,
             system_routes, baseline_routes, requirement_routes, analysis_routes, data_routes, mcp_routes,
             memory_routes):
    router.include_router(area.router)
