"""Typed HTTP adapters for existing, local analysis services."""
import math

from fastapi import APIRouter, HTTPException

from app.services.storage import store
from app.services.execution_lifecycle import validate_id
from app.services.ml_analysis import ml_detector
router = APIRouter(tags=['analysis'])


def _execution(execution_id):
    try:
        validate_id(execution_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    execution = store.get_execution(execution_id)
    if execution is None:
        raise HTTPException(404, 'Execution not found')
    if not execution.summary or not execution.summary.total_requests:
        raise HTTPException(409, 'Execution has no usable parsed report')
    return execution


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


@router.post('/analysis/anomalies')
async def detect_anomalies(execution_id: str):
    execution = _execution(execution_id)
    if not execution.summary.timeline_segments:
        raise HTTPException(409, 'Execution has no timeline for anomaly analysis')
    return _json_safe(ml_detector.analyze_timeline(execution.summary.timeline_segments).model_dump())

