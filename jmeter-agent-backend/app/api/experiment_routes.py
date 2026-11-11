"""Runs that answer a question: verification runs, capacity searches, and the CI gate of a run."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.shared import _approval_needed
from app.models.schemas import TestExecutionInfo
from app.services import experiments
from app.services.jmeter.environment import ProductionRunNeedsConfirmation
from app.services.jmeter.script_risk import ScriptApprovalRequired

router = APIRouter(tags=['experiments'])


class CapacitySearchRequest(BaseModel):
    start_threads: int = Field(..., ge=1, le=100_000)
    max_threads: int = Field(..., ge=1, le=100_000)
    factor: float = Field(1.5, ge=1.1, le=4)
    max_error_rate: float = Field(1.0, ge=0, le=100, description='错误率超过这个百分比就停止')
    confirm_stop: bool = Field(True, description='在停止的负载上复测一次，排除偶然波动')
    refine_steps: int = Field(2, ge=0, le=experiments.MAX_REFINE_STEPS,
                              description='在最后通过和首次不通过的负载之间再试几步')


def _refusal(error: Exception) -> HTTPException:
    if isinstance(error, (ProductionRunNeedsConfirmation, ScriptApprovalRequired)):
        return _approval_needed(error)
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    return HTTPException(status_code=409, detail=str(error))


@router.post('/hypotheses/{hypothesis_id}/verification-run', response_model=TestExecutionInfo)
async def verification_run(hypothesis_id: str, approved_sha256: Optional[str] = Query(None),
                           confirm_production: bool = Query(False)):
    """按假设的预测重跑它的计划（预测里写了线程数就用那个线程数）；跑完后程序自动判定这条假设"""
    try:
        return await experiments.start_verification(hypothesis_id, approved_sha256=approved_sha256,
                                                     confirm_production=confirm_production)
    except (ProductionRunNeedsConfirmation, ScriptApprovalRequired, LookupError, ValueError,
            RuntimeError, FileExistsError) as error:
        raise _refusal(error) from error


@router.post('/test-plans/{plan_id}/capacity-searches', response_model=experiments.CapacitySearch)
async def start_capacity_search(plan_id: str, request: CapacitySearchRequest,
                                approved_sha256: Optional[str] = Query(None), confirm_production: bool = Query(False)):
    """逐级加压，在第一个不达标、错误率超限或吞吐量不再增长的负载处停下；先复测确认，再在通过和不通过之间细化"""
    try:
        return await experiments.start_capacity_search(
            plan_id, start_threads=request.start_threads, max_threads=request.max_threads, factor=request.factor,
            max_error_rate=request.max_error_rate, confirm_stop=request.confirm_stop,
            refine_steps=request.refine_steps, approved_sha256=approved_sha256,
            confirm_production=confirm_production)
    except (ProductionRunNeedsConfirmation, ScriptApprovalRequired, LookupError, ValueError,
            RuntimeError, FileExistsError) as error:
        raise _refusal(error) from error


@router.get('/test-plans/{plan_id}/capacity-searches', response_model=list[experiments.CapacitySearch])
async def list_capacity_searches(plan_id: str):
    return experiments.list_searches(plan_id)


@router.get('/capacity-searches/{search_id}', response_model=experiments.CapacitySearch)
async def get_capacity_search(search_id: str):
    try:
        search = experiments.load_search(search_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if search is None:
        raise HTTPException(status_code=404, detail='Capacity search not found')
    return search


@router.post('/capacity-searches/{search_id}/cancel')
async def cancel_capacity_search(search_id: str):
    if not await experiments.cancel_search(search_id):
        raise HTTPException(status_code=409, detail='This search is not running')
    return {'cancelled': True}


@router.get('/executions/{execution_id}/gate')
async def execution_gate(execution_id: str):
    """CI 门禁：pass / fail / error；运行或报告还没结束时是 pending，轮询到它变化为止"""
    try:
        return await experiments.gate(execution_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
