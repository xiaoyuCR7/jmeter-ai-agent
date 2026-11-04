"""Runs: starting them, their reports and analyses, and questions about a finished run."""
import logging
import os
import json
import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from app.core.config import settings
from app.models.schemas import (
    TestExecutionInfo, TestReport, TestExecutionStatus, RunQuestion, ReanalyzeRequest,
)
from app.services.ai import usage as ai_usage
from app.services.ai.adapters.factory import AIAdapterFactory
from app.services.jmeter.script_risk import ScriptApprovalRequired
from app.services.jmeter.environment import ProductionRunNeedsConfirmation
from app.services.jmeter.sla_engine import SLAEngine, SLAConfig
from app.services.ai.agent.tools.jmeter import resource_hooks
from app.services.ai.agents import AnalysisAgent
from app.services.ai.agents.analysis_agent import AnalysisExecutionError
from app.services.storage import store
from app.services.execution_lifecycle import execution_lifecycle, persist_execution
from app.services import run_diagnostics, server_metrics
from app.services import post_run
from app.services.post_run import save_analysis_markdown
from app.api.shared import _approval_needed, _report_parser, ensure_default_project

logger = logging.getLogger(__name__)
router = APIRouter()


MAX_FOLLOW_UP_CONTEXT = 3  # Earlier answers a follow-up question can build on.
_question_locks: dict[str, asyncio.Lock] = {}  # One answer saved at a time per run.


# ===== Test Execution =====
@router.post("/test-plans/{plan_id}/execute", response_model=TestExecutionInfo)
async def execute_test_plan(
    plan_id: str,
    background_tasks: BackgroundTasks,
    ai_model: Optional[str] = Query(None, description="AI model for analysis"),
    approved_sha256: Optional[str] = Query(None, description="The plan content the user approved to run on this machine"),
    confirm_production: bool = Query(False, description="The user confirmed running against a production environment"),
):
    """执行测试计划"""
    if plan_id not in store.test_plans:
        raise HTTPException(status_code=404, detail="Test plan not found")
    
    plan = store.test_plans[plan_id]
    # The user chose to run it now, so a busy machine is recorded for the report, not waited on.
    before_execute, postprocess = resource_hooks(False, {}, wait=False)
    try:
        return await execution_lifecycle.submit(
            plan.jmx_path, test_plan_id=plan_id, approved_sha256=approved_sha256,
            confirm_production=confirm_production,
            before_execute=before_execute, postprocess=postprocess, ai_model=ai_model)
    except (ProductionRunNeedsConfirmation, ScriptApprovalRequired) as error:
        raise _approval_needed(error) from error
    except (ValueError, FileExistsError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/executions", response_model=dict)
async def list_executions(
    project_id: Optional[str] = Query(None, description="按项目过滤"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """列出执行记录（新的在前），每行附带计划名、需求判定与 AI 分析状态，供执行列表直接展示。"""
    from app.services import requirements as requirements_service
    execs = [e for e in store.executions.values() if not project_id or e.project_id == project_id]
    execs.sort(key=lambda e: e.start_time or datetime.min, reverse=True)
    page = execs[offset:offset + limit]
    project_requirements = {}
    for pid in {e.project_id for e in page}:
        try:
            project_requirements[pid] = await requirements_service.get_requirements(pid)
        except Exception:
            logger.warning("Failed to load requirements for project %s", pid, exc_info=True)
            project_requirements[pid] = None
    rows = []
    for e in page:
        plan = store.test_plans.get(e.test_plan_id)
        project = store.projects.get(e.project_id)
        summary = e.summary
        verdict = None
        if summary and summary.total_requests:
            evaluation = requirements_service.evaluate(project_requirements.get(e.project_id), summary,
                                                       requirements_service.sampler_routes(e.jmx_path))
            verdict = {"verdict": evaluation.verdict, "passed": evaluation.passed,
                       "failed": evaluation.failed, "missing": evaluation.missing}
        rows.append({
            "id": e.id,
            "project_id": e.project_id,
            "project_name": project.name if project else "",
            "test_plan_id": e.test_plan_id,
            "test_plan_name": plan.name if plan else "",
            "status": e.status.value if hasattr(e.status, 'value') else str(e.status),
            "start_time": e.start_time.isoformat() if e.start_time else None,
            "end_time": e.end_time.isoformat() if e.end_time else None,
            "progress": e.progress,
            "error_message": e.error_message,
            "metrics": {
                "total_requests": summary.total_requests,
                "avg_response_time": summary.avg_response_time,
                "p95_response_time": summary.p95_response_time,
                "error_rate": summary.error_rate,
                "throughput": summary.throughput,
            } if summary else None,
            "requirements": verdict,
            "has_ai_analysis": bool(e.ai_analysis),
            "has_markdown": bool(e.report_dir and os.path.isfile(os.path.join(e.report_dir, "ai_analysis.md"))),
        })
    return {"executions": rows, "total": len(execs), "offset": offset, "limit": limit}


@router.get("/projects/{project_id}/executions", response_model=list)
async def list_project_executions(project_id: str):
    """列出指定项目下的执行记录"""
    await ensure_default_project()
    return [e for e in store.executions.values() if e.project_id == project_id]


@router.get("/executions/{execution_id}", response_model=TestExecutionInfo)
async def get_execution(execution_id: str):
    """获取执行状态"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    return store.executions[execution_id]


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str):
    """取消测试执行"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")

    success = await execution_lifecycle.cancel(execution_id)
    return {"success": success, "message": "" if success else "Execution already ended or could not be stopped"}


@router.get("/executions/{execution_id}/report", response_model=TestReport)
async def get_report(execution_id: str):
    """获取测试报告"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    execution = store.executions[execution_id]
    # Reports are available for completed, failed, and cancelled executions
    # so users can inspect partial results and logs.
    if execution.status not in (
        TestExecutionStatus.COMPLETED,
        TestExecutionStatus.FAILED,
        TestExecutionStatus.CANCELLED,
    ):
        raise HTTPException(status_code=400, detail="Test not completed yet")
    
    # Parse JTL file — fall back to cached summary on parse failure
    jtl_path = os.path.join(execution.report_dir, "result.jtl") if execution.report_dir else None
    summary = getattr(execution, "summary", None)
    if summary is None and jtl_path and os.path.exists(jtl_path):
        try:
            summary = await asyncio.to_thread(_report_parser.parse_jtl, jtl_path)
        except Exception as e:
            logger.warning("Report JTL re-parse failed for %s: %s", execution_id, e)
    if summary is None:
        summary = _report_parser._empty_summary()

    # Get HTML report path
    html_path = _report_parser.get_html_report_url(execution.report_dir)

    # SLA evaluation (best-effort)
    sla_result = None
    if summary and summary.total_requests > 0:
        try:
            sla_config = SLAConfig.standard_api_sla()
            sla_engine = SLAEngine()
            sla_result = asdict(sla_engine.evaluate(summary, sla_config))
        except Exception as e:
            logger.warning("SLA evaluation failed for %s: %s", execution_id, e)

    plan = store.test_plans.get(execution.test_plan_id)

    failure_diagnosis = None
    if (execution.status == TestExecutionStatus.FAILED or summary.total_requests == 0
            or (execution.status == TestExecutionStatus.CANCELLED and execution.error_message)):
        log_text = await asyncio.to_thread(_report_parser.read_log, execution.log_path, 200) if execution.log_path else ''
        failure_diagnosis = run_diagnostics.diagnose_failure(execution.error_message, log_text)

    evidence = None
    if summary.total_requests > 0:
        from app.services.evidence import build_evidence
        try:
            evidence = (await build_evidence(execution, summary)).model_dump()
        except Exception as e:
            logger.warning("Evidence assembly failed for %s: %s", execution_id, e)

    report = TestReport(
        execution_id=execution_id,
        project_id=execution.project_id or "default",
        test_plan_id=execution.test_plan_id,
        test_plan_name=plan.name if plan else "Unknown",
        start_time=execution.start_time,
        end_time=execution.end_time or datetime.now(),
        duration_seconds=(execution.end_time - execution.start_time).total_seconds() if execution.end_time and execution.start_time else 0,
        summary=summary,
        raw_data_path=jtl_path,
        html_report_path=html_path,
        ai_analysis=getattr(execution, 'ai_analysis', None),
        sla_result=sla_result,
        evidence=evidence,
        failure_diagnosis=failure_diagnosis,
        environment=server_metrics.run_environment(execution.report_dir) or None,
        status=execution.status,
        report_status=execution.report_status,
        report_error=execution.report_error,
        analysis_enabled=settings.AUTO_ANALYSIS_ENABLED,
    )

    return report


@router.post("/executions/{execution_id}/analysis", status_code=202)
async def reanalyze_execution(execution_id: str, request: Optional[ReanalyzeRequest] = None):
    """重新生成这次运行的 AI 分析；后台进行，报告页轮询 report_status"""
    try:
        execution = await post_run.start_reanalysis(execution_id, request.model if request else None)
    except LookupError:
        raise HTTPException(status_code=404, detail="Execution not found") from None
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {'report_status': execution.report_status}


async def _question_setup(execution_id: str, request: RunQuestion):
    """The evidence, the report context and the agent for a question; refuses before anything runs."""
    try:
        evidence = await AnalysisAgent.resolve_execution(execution_id)
    except AnalysisExecutionError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    execution = store.get_execution(execution_id)
    if execution is None or execution.ai_analysis is None:
        raise HTTPException(status_code=409, detail="This run has no analysis to ask about")
    if execution.report_status == 'running':
        raise HTTPException(status_code=409, detail="The analysis is being written; ask when it is done")
    earlier = execution.ai_analysis.follow_ups[-MAX_FOLLOW_UP_CONTEXT:]
    context = json.dumps({'report': execution.ai_analysis.model_dump(exclude={'follow_ups', 'fact_check', 'usage'}),
                          'earlier_questions': [{'question': item.get('question'), 'answer': item.get('answer')}
                                                for item in earlier]}, ensure_ascii=False, default=str)
    try:
        agent = AnalysisAgent(AIAdapterFactory.create(request.model))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return evidence, context, agent


async def _save_answer(execution_id: str, question: str, result, used: dict) -> dict:
    validation = (result.analysis or {}).get('validation') or {}
    answer = {'question': question, 'answer': result.output, 'asked_at': datetime.now().isoformat(),
              'mismatches': validation.get('mismatches') or [],
              'corrected_from': (result.analysis or {}).get('corrected_from') or 0, 'usage': used}
    async with _question_locks.setdefault(execution_id, asyncio.Lock()):
        # Read again: the run may have been deleted or re-analysed while the model was answering.
        current = store.get_execution(execution_id)
        if current is None or current.ai_analysis is None or current.report_status == 'running':
            raise HTTPException(status_code=409, detail="The run or its analysis changed while answering; ask again")
        execution = current.model_copy(deep=True)
        execution.ai_analysis.follow_ups.append(answer)
        await persist_execution(execution)
    save_analysis_markdown(execution.report_dir, execution_id, execution.ai_analysis)
    return answer


QUESTION_PHASES = {
    'preparation': ('正在准备这次运行的证据', "Gathering this run's evidence"),
    'postprocessing': ('正在核对回答里的数字', 'Checking the numbers in the answer'),
    'correction': ('有数字与实测不符，正在让模型改正', 'Some numbers did not match; asking the model to fix them'),
}
QUESTION_TOOLS = {
    'find_samples': ('查看单个样本', 'Looking at individual samples'),
    'read_run_log': ('查看 JMeter 日志', 'Reading the JMeter log'),
    'describe_plan': ('查看测试计划', 'Reading the test plan'),
    'detect_anomalies': ('查找时间线上的突变', 'Looking for spikes and drops'),
    'list_runs': ('查找早先的运行', 'Finding earlier runs'),
    'compare_with_run': ('和早先的运行对比', 'Comparing with an earlier run'),
}


@router.post("/executions/{execution_id}/questions/stream")
async def ask_about_execution_stream(execution_id: str, request: RunQuestion, lang: str = 'zh-CN'):
    """追问，并以 SSE 报告进度；最后一条是保存好的问答（step=answer）或错误（step=error）"""
    evidence, context, agent = await _question_setup(execution_id, request)
    en = lang == 'en'

    def progress(event):
        data = event.data or {}
        if event.type == 'analysis_phase' and data.get('phase') in QUESTION_PHASES:
            return QUESTION_PHASES[data['phase']][en]
        if event.type == 'tool_call':
            return QUESTION_TOOLS.get(data.get('tool'), ('查看更多数据', 'Looking further'))[en]
        if event.type == 'queued':
            return 'Other AI tasks are running; waiting for one to finish' if en else '其它 AI 任务正在运行，正在排队'
        return None

    async def events():
        from contextlib import aclosing
        with ai_usage.metered() as used:
            outcome = {}

            async def answer():
                outcome['result'] = await agent.run_analysis(request.question, evidence, context=context)
                return outcome['result']

            async with aclosing(agent.stream(answer)) as stream:
                async for event in stream:
                    message = progress(event)
                    if message:
                        yield f"data: {json.dumps({'step': 'progress', 'message': message}, ensure_ascii=False)}\n\n"
            result = outcome['result']
            if not result.success:
                payload = {'step': 'error', 'message': result.error or 'The model did not answer'}
            else:
                try:
                    payload = {'step': 'answer', 'answer': await _save_answer(execution_id, request.question, result, used)}
                except HTTPException as error:
                    payload = {'step': 'error', 'message': error.detail}
            yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/executions/{execution_id}/log")
async def get_execution_log(execution_id: str, lines: int = 100):
    """获取执行日志"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    execution = store.executions[execution_id]
    if not execution.log_path or not os.path.exists(execution.log_path):
        return {"log": "No log available"}
    
    log_content = _report_parser.read_log(execution.log_path, lines)
    return {"log": log_content}


@router.get("/analysis/reports/{execution_id}/download")
async def download_analysis_report(execution_id: str):
    """下载AI分析报告的Markdown文件"""
    if execution_id not in store.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    e = store.executions[execution_id]
    if not e.report_dir:
        raise HTTPException(status_code=404, detail="Report directory not found")

    md_path = os.path.join(e.report_dir, "ai_analysis.md")
    if not os.path.isfile(md_path):
        raise HTTPException(status_code=404, detail="AI analysis markdown not found")

    return FileResponse(
        md_path,
        media_type="text/markdown",
        filename=f"ai_analysis_{execution_id}.md"
    )
