"""What the agent remembers, where people can see and correct it: hypotheses, lessons and an overview."""
import asyncio
from collections import Counter
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.models.schemas import HypothesisStatus
from app.services.ai.memory import experience, fact_ledger, hypothesis_ledger, prediction
from app.services.storage import store

router = APIRouter(tags=["memory"])

MAX_HYPOTHESES_ON_REPORT = 20


class ExperienceRequest(BaseModel):
    kind: Literal['known_issue', 'defect_retrospective', 'module_standard']
    title: str = Field(..., min_length=1, max_length=120)
    content: str = Field(..., min_length=20, max_length=4000, description="至少 20 个字，写清现象、原因或标准")
    labels: list[str] = Field(default_factory=list, max_length=20, description="涉及的接口；留空表示整个项目")


class ExperienceStatusRequest(BaseModel):
    status: Literal['active', 'retired']
    reason: str = Field('', max_length=500)


class HypothesisDecision(BaseModel):
    status: Literal['confirmed', 'refuted', 'deprecated']
    note: str = Field('', max_length=1000)


def hypothesis_view(hyp) -> dict:
    expected = hyp.prediction or prediction.from_text(hyp.testable_prediction)
    return {'id': hyp.hypothesis_id, 'statement': hyp.statement, 'category': hyp.category.value,
            'status': hyp.status.value, 'prediction': prediction.describe(expected) or hyp.testable_prediction,
            'checkable': prediction.checkable(expected), 'plan_id': hyp.module, 'source_execution_id': hyp.source_execution_id,
            'verified_by': hyp.verified_by_execution_id, 'refuted_by': hyp.refuted_by_execution_id,
            'notes': hyp.verification_notes, 'created_at': hyp.created_at, 'updated_at': hyp.updated_at}


def _execution(execution_id: str):
    execution = store.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    return execution


def _labels(execution) -> set[str]:
    return set((execution.summary.per_label or {}) if execution.summary else {})


@router.get("/executions/{execution_id}/memory")
async def run_memory(execution_id: str):
    """Hypotheses and lessons that bear on this run, as the report page shows them."""
    execution = _execution(execution_id)
    project_id, labels = execution.project_id, _labels(execution)
    hypotheses = [hyp for hyp in await hypothesis_ledger.list_for_project(project_id, limit=200)
                  if hyp.module == execution.test_plan_id or (hyp.prediction or {}).get('label') in labels]
    lessons = await asyncio.to_thread(experience.relevant, project_id, labels)
    return {'labels': sorted(labels), 'project_id': project_id,
            'hypotheses': [hypothesis_view(hyp) for hyp in hypotheses[:MAX_HYPOTHESES_ON_REPORT]],
            'experiences': [experience.view(doc) for doc in lessons]}


@router.post("/executions/{execution_id}/experiences")
async def save_experience(execution_id: str, request: ExperienceRequest):
    """Save a lesson from this run's report; later reports of the project will see it."""
    execution = _execution(execution_id)
    unknown = set(request.labels) - _labels(execution)
    if unknown:
        raise HTTPException(status_code=400, detail=f"This run has no request named {sorted(unknown)[0]}")
    try:
        doc = await asyncio.to_thread(
            experience.save, execution.project_id, request.kind, request.title, request.content, request.labels,
            execution_id, execution.test_plan_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return experience.view(doc)


@router.get("/projects/{project_id}/experiences")
async def list_experiences(project_id: str, include_retired: bool = Query(True)):
    docs = await asyncio.to_thread(experience.for_project, project_id, include_retired=include_retired)
    return [experience.view(doc) for doc in docs]


@router.patch("/experiences/{doc_id}")
async def set_experience_status(doc_id: str, request: ExperienceStatusRequest):
    """Retire a lesson that turned out wrong or outdated; retired ones stay out of reports."""
    doc = await asyncio.to_thread(experience.set_status, doc_id, request.status, request.reason)
    if doc is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return experience.view(doc)


@router.patch("/hypotheses/{hypothesis_id}")
async def decide_hypothesis(hypothesis_id: str, request: HypothesisDecision):
    """A person settles a hypothesis no program can check, or one they know more about."""
    hyp = await hypothesis_ledger.get(hypothesis_id)
    if hyp is None:
        raise HTTPException(status_code=404, detail="Hypothesis not found")
    status = HypothesisStatus(request.status)
    note = '人工判定' + (f'：{request.note.strip()}' if request.note.strip() else '')
    if not await hypothesis_ledger.update_status(hypothesis_id, status, notes=note):
        raise HTTPException(status_code=500, detail="The decision was not saved")
    return hypothesis_view(await hypothesis_ledger.get(hypothesis_id))


@router.get("/memory/overview")
async def memory_overview(project_id: str = Query(..., min_length=1)):
    """Whether memory is growing into something used, or only piling up."""
    hypotheses = await hypothesis_ledger.list_for_project(project_id, limit=5000)
    lessons = await asyncio.to_thread(experience.for_project, project_id, include_retired=True)
    active = [doc for doc in lessons if doc.metadata.get('status', 'active') == 'active']
    kinds = {kind: {'active': sum(doc.doc_type == kind for doc in active),
                    'retired': sum(doc.doc_type == kind for doc in lessons) - sum(doc.doc_type == kind for doc in active)}
             for kind in experience.KINDS}
    refuted = [hyp for hyp in hypotheses if hyp.status == HypothesisStatus.REFUTED][:3]
    cited = sorted(active, key=lambda doc: doc.metadata.get('cited_count', 0), reverse=True)[:3]
    return {'project_id': project_id, 'runs': await fact_ledger.count_project_facts(project_id),
            'hypotheses': dict(Counter(hyp.status.value for hyp in hypotheses)),
            'checkable_open': sum(1 for hyp in hypotheses if hyp.status == HypothesisStatus.UNTESTED
                                  and prediction.checkable(hyp.prediction or prediction.from_text(hyp.testable_prediction))),
            'experiences': kinds, 'needs_review': sum(experience.needs_review(doc) for doc in active),
            'most_cited': [experience.view(doc) for doc in cited if doc.metadata.get('cited_count')],
            'recently_refuted': [hypothesis_view(hyp) for hyp in refuted]}
