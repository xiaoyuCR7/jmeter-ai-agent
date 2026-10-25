"""Project performance requirements: AI extraction from documents, review, storage."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import requirements as requirement_service
from app.services import server_metrics
from app.services.storage import store

router = APIRouter(tags=["requirements"])


class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=200_000)
    source_name: str = Field('', max_length=200)
    model: Optional[str] = None


def _require_project(project_id: str):
    if project_id not in store.projects:
        raise HTTPException(404, "Project not found")


@router.get("/projects/{project_id}/requirements")
async def get_requirements(project_id: str):
    _require_project(project_id)
    stored = await requirement_service.get_requirements(project_id)
    return stored.model_dump() if stored else None


@router.put("/projects/{project_id}/requirements")
async def save_requirements(project_id: str, body: requirement_service.PerformanceRequirements):
    _require_project(project_id)
    return (await requirement_service.save_requirements(project_id, body)).model_dump()


@router.delete("/projects/{project_id}/requirements")
async def delete_requirements(project_id: str):
    _require_project(project_id)
    return {"deleted": await requirement_service.delete_requirements(project_id)}


@router.post("/projects/{project_id}/requirements/extract")
async def extract_requirements(project_id: str, body: ExtractRequest):
    """Return a draft for review; nothing is saved until the user confirms it with PUT."""
    _require_project(project_id)
    from app.services.ai.adapters.factory import AIAdapterFactory
    try:
        adapter = AIAdapterFactory.create(body.model)
        draft = await requirement_service.extract_requirements(body.text, adapter, body.source_name)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(502, f"Requirement extraction failed: {type(error).__name__}") from error
    return draft.model_dump()


@router.get("/monitoring/presets")
async def monitoring_presets():
    return {name: [q.model_dump() for q in queries] for name, queries in server_metrics.PRESETS.items()}


@router.get("/projects/{project_id}/monitoring")
async def get_monitoring(project_id: str):
    _require_project(project_id)
    config = await server_metrics.get_config(project_id)
    return config.model_dump() if config else None


@router.put("/projects/{project_id}/monitoring")
async def save_monitoring(project_id: str, body: server_metrics.MonitoringConfig):
    _require_project(project_id)
    return (await server_metrics.save_config(project_id, body)).model_dump()


@router.delete("/projects/{project_id}/monitoring")
async def delete_monitoring(project_id: str):
    _require_project(project_id)
    return {"deleted": await server_metrics.delete_config(project_id)}


@router.post("/projects/{project_id}/monitoring/test")
async def test_monitoring(project_id: str, body: server_metrics.MonitoringConfig):
    """Check the URL answers, then run each query over the last five minutes, before the user saves."""
    _require_project(project_id)
    from datetime import datetime, timedelta
    result = await server_metrics.probe(body.prometheus_url)
    result["queries"] = []
    if result["reachable"] and body.queries:
        end = datetime.now() - timedelta(seconds=60)
        raw = await server_metrics.collect(body, end - timedelta(minutes=3), end)
        result["queries"] = [{"name": m["name"], "series": len(m["series"]), "error": m.get("error")}
                             for m in raw["metrics"]]
    return result


@router.post("/executions/{execution_id}/server-metrics/refresh")
async def refresh_server_metrics(execution_id: str):
    """Discard the cached snapshot and query Prometheus again for this run."""
    import os
    execution = store.executions.get(execution_id)
    if execution is None:
        raise HTTPException(404, "Execution not found")
    if execution.report_dir:
        try:
            os.remove(os.path.join(execution.report_dir, server_metrics.CACHE_FILE))
        except FileNotFoundError:
            pass
    return await execution_evidence(execution_id)


@router.get("/executions/{execution_id}/evidence")
async def execution_evidence(execution_id: str):
    execution = store.executions.get(execution_id)
    if execution is None:
        raise HTTPException(404, "Execution not found")
    if not execution.summary or not execution.summary.total_requests:
        raise HTTPException(409, "Execution has no usable parsed report")
    from app.services.evidence import build_evidence
    return (await build_evidence(execution, execution.summary)).model_dump()
