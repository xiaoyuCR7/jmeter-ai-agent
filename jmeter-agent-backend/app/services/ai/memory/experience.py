"""Lessons people confirmed: known issues, defect reviews and request standards, kept per project.

Nothing here is written by a model. A person saves a lesson from a report; later reports of the
same project see it, newest and most cited first, until someone retires it.
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from app.services.ai.rag.store import RAGDocument, rag_store

logger = logging.getLogger(__name__)

KINDS = {'known_issue': '已知问题', 'defect_retrospective': '缺陷复盘', 'module_standard': '接口标准'}
REVIEW_AFTER_DAYS = 90  # Older lessons are still shown, marked for a second look.
MAX_IN_PROMPT = 5


def _normalized(title: str) -> str:
    return ' '.join(title.lower().split())


def _active(doc: RAGDocument) -> bool:
    return doc.metadata.get('status', 'active') == 'active'


def needs_review(doc: RAGDocument, now: Optional[datetime] = None) -> bool:
    # reviewed_at, not updated_at: counting a citation rewrites the row but is not a review.
    try:
        reviewed = datetime.fromisoformat(doc.metadata.get('reviewed_at') or doc.created_at)
    except (TypeError, ValueError):
        return True
    return (now or datetime.now()) - reviewed > timedelta(days=REVIEW_AFTER_DAYS)


def view(doc: RAGDocument) -> dict:
    """What the API returns for a lesson."""
    return {'id': doc.id, 'kind': doc.doc_type, 'title': doc.title, 'content': doc.content,
            'labels': doc.keywords, 'project_id': doc.module, 'source_execution_id': doc.source_execution_id,
            'status': doc.metadata.get('status', 'active'), 'cited_count': doc.metadata.get('cited_count', 0),
            'last_cited_at': doc.metadata.get('last_cited_at'), 'needs_review': needs_review(doc),
            'retired_reason': doc.metadata.get('retired_reason'), 'reviewed_at': doc.metadata.get('reviewed_at'),
            'created_at': doc.created_at, 'updated_at': doc.updated_at}


def save(project_id: str, kind: str, title: str, content: str, labels: list[str],
         source_execution_id: Optional[str] = None, source_plan_id: Optional[str] = None) -> RAGDocument:
    """Save a lesson. The same title of the same kind in a project updates it instead of adding a copy."""
    if kind not in KINDS:
        raise ValueError(f'Unknown kind: {kind}')
    title, content = title.strip(), content.strip()
    if not title:
        raise ValueError('A lesson needs a title')
    doc_id = hashlib.sha256(json.dumps([project_id, kind, _normalized(title)], ensure_ascii=False).encode()).hexdigest()
    labels = sorted({label.strip() for label in labels if label and label.strip()})
    now = datetime.now().isoformat()
    existing = rag_store.get(doc_id)
    if existing:
        metadata = {**existing.metadata, 'status': 'active', 'reviewed_at': now}
        metadata.pop('retired_reason', None)
        return rag_store.update(doc_id, title=title, content=content, keywords=labels, metadata=metadata)
    doc = RAGDocument(id=doc_id, doc_type=kind, module=project_id, version='n/a', title=title, content=content,
                      keywords=labels, source_execution_id=source_execution_id, source_plan_id=source_plan_id,
                      metadata={'status': 'active', 'cited_count': 0, 'created_by': 'user', 'reviewed_at': now})
    rag_store.add(doc)
    return rag_store.get(doc_id)


def for_project(project_id: str, *, include_retired: bool = False, limit: int = 200) -> list[RAGDocument]:
    found = [doc for kind in KINDS for doc in rag_store.hybrid_search(module=project_id, doc_type=kind, limit=limit)]
    found = [doc for doc in found if include_retired or _active(doc)]
    return sorted(found, key=lambda doc: doc.updated_at, reverse=True)


def relevant(project_id: str, labels: set[str]) -> list[RAGDocument]:
    """Lessons for the requests this run has, then project-wide ones; most cited and newest first."""
    def rank(doc):
        about_run = bool(set(doc.keywords) & labels)
        return (about_run, doc.metadata.get('cited_count', 0), doc.created_at)
    usable = [doc for doc in for_project(project_id) if not doc.keywords or set(doc.keywords) & labels]
    return sorted(usable, key=rank, reverse=True)[:MAX_IN_PROMPT]


def cite(docs: list[RAGDocument]) -> None:
    """Count each time a lesson reaches a report, so the overview shows which ones are used."""
    now = datetime.now().isoformat()
    for doc in docs:
        metadata = {**doc.metadata, 'cited_count': doc.metadata.get('cited_count', 0) + 1, 'last_cited_at': now}
        try:
            rag_store.update(doc.id, metadata=metadata)
        except Exception as error:
            logger.warning('Could not count a citation of %s: %s', doc.id, error)


def set_status(doc_id: str, status: str, reason: str = '') -> Optional[RAGDocument]:
    """Retire a lesson that turned out wrong or outdated, or bring it back. Retired ones stay for the record."""
    doc = rag_store.get(doc_id)
    if doc is None or doc.doc_type not in KINDS:
        return None
    if status not in ('active', 'retired'):
        raise ValueError(f'Unknown status: {status}')
    metadata = {**doc.metadata, 'status': status}
    if status == 'retired':
        metadata['retired_reason'] = reason.strip()
    else:
        metadata.pop('retired_reason', None)
        metadata['reviewed_at'] = datetime.now().isoformat()  # Bringing it back is a review.
    return rag_store.update(doc_id, metadata=metadata)


def forget_project(project_id: str) -> int:
    """Lessons of a deleted project, retired ones included."""
    docs = for_project(project_id, include_retired=True)
    for doc in docs:
        rag_store.delete(doc.id)
    return len(docs)


def format_for_prompt(docs: list[RAGDocument]) -> str:
    if not docs:
        return ''
    lines = ['\n\n## 团队确认过的经验（人工保存，只作线索，不是本次测试的事实）']
    for doc in docs:
        scope = '、'.join(doc.keywords) if doc.keywords else '整个项目'
        review = '（超过 90 天未更新，可能已过时）' if needs_review(doc) else ''
        lines.append(f'- [{KINDS[doc.doc_type]}｜{scope}] {doc.title}{review}：{doc.content[:600]}')
    return '\n'.join(lines)
