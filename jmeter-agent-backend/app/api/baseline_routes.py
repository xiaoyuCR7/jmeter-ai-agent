"""Performance baseline management endpoints."""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from app.services import baseline_compare
from app.services.db_store import db_store

router = APIRouter(tags=["baselines"])


@router.post("/baselines")
async def create_baseline(
    project_id: str,
    name: str,
    test_plan_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    description: str = "",
    thresholds: Optional[dict] = None,
    metrics_snapshot: Optional[dict] = None,
):
    """Create a performance baseline (expected P99, error rate...) for regression checks."""
    return await baseline_compare.create_baseline(project_id, name, test_plan_id, execution_id, description,
                                                  thresholds, metrics_snapshot)


@router.post("/executions/{execution_id}/promote-baseline")
async def promote_execution_to_baseline(execution_id: str):
    """One click on a report: make this run the active baseline of its test plan."""
    return await baseline_compare.promote_execution(execution_id)


@router.get("/baselines")
async def list_baselines(
    project_id: Optional[str] = Query(None, description="Filter by project"),
):
    """List all performance baselines, optionally filtered by project."""
    return await db_store.list_baselines(project_id=project_id)


@router.get("/baselines/active")
async def get_active_baseline(
    project_id: str = Query(..., description="Project ID"),
    test_plan_id: Optional[str] = Query(None, description="Test plan ID"),
):
    """Get the currently active baseline for a project/test plan."""
    baseline = await db_store.get_active_baseline(project_id, test_plan_id)
    if not baseline:
        raise HTTPException(status_code=404, detail="No active baseline found")
    return baseline


@router.post("/baselines/{baseline_id}/activate")
async def activate_baseline(
    baseline_id: int,
):
    """Activate a baseline; other baselines in the same scope are deactivated."""
    baseline = await db_store.set_active_baseline(baseline_id)
    if not baseline:
        raise HTTPException(status_code=404, detail="Baseline not found")
    return baseline


@router.delete("/baselines/{baseline_id}")
async def delete_baseline(
    baseline_id: int,
):
    """Delete a performance baseline."""
    deleted = await db_store.delete_baseline(baseline_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Baseline not found")
    return {"message": "Baseline deleted"}
