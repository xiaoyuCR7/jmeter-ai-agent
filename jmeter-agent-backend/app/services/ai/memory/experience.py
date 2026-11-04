"""Lessons people confirmed: known issues, defect reviews and request standards, kept per project.

Nothing here is written by a model. A person saves a lesson from a report; later reports of the
same project see it, newest and most cited first, until someone retires it.
"""
import hashlib
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.core.config import settings
from app.services.ai.memory.storage import memory_db_path, MIGRATED_SUFFIX

logger = logging.getLogger(__name__)

KINDS = {'known_issue': '已知问题', 'defect_retrospective': '缺陷复盘', 'module_standard': '接口标准'}
REVIEW_AFTER_DAYS = 90  # Older lessons are still shown, marked for a second look.
MAX_IN_PROMPT = 5
LEGACY_DIR = 'rag_lancedb'  # Where lessons lived before they moved into the app's database.


@dataclass
class Lesson:
    id: str
    doc_type: str  # One of KINDS.
    module: str    # The project.
    title: str
    content: str
    keywords: list[str] = field(default_factory=list)  # The requests it is about; none means the whole project.
    source_execution_id: Optional[str] = None
    source_plan_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


_COLUMNS = ('id', 'doc_type', 'module', 'title', 'content', 'keywords', 'source_execution_id', 'source_plan_id',
            'metadata', 'created_at', 'updated_at')


class LessonStore:
    """The lessons table. Calls block; async callers run them in a thread."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or memory_db_path()
        self._lock = threading.Lock()
        self._ready = False
        self._adopt = db_path is None  # Only the app's own database takes over the old LanceDB lessons.

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            if not self._ready:
                with self._lock:
                    if not self._ready:
                        self._create(db)
                        self._ready = True
            yield db
            db.commit()
        finally:
            db.close()

    def _create(self, db):
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("""CREATE TABLE IF NOT EXISTS lessons (
            id TEXT PRIMARY KEY, doc_type TEXT NOT NULL, module TEXT NOT NULL, title TEXT NOT NULL,
            content TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '[]', source_execution_id TEXT,
            source_plan_id TEXT, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        db.execute('CREATE INDEX IF NOT EXISTS idx_lessons_module ON lessons(module)')
        if self._adopt:
            for lesson in _legacy_lessons():
                db.execute(f"INSERT OR IGNORE INTO lessons ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
                           _row(lesson))

    def get(self, lesson_id: str) -> Optional[Lesson]:
        with self._db() as db:
            row = db.execute(f"SELECT {', '.join(_COLUMNS)} FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        return _lesson(row) if row else None

    def add(self, lesson: Lesson) -> str:
        with self._db() as db:
            db.execute(f"INSERT INTO lessons ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
                       _row(lesson))
        return lesson.id

    def update(self, lesson_id: str, **fields) -> Optional[Lesson]:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        for key, value in fields.items():
            if key not in ('id', 'created_at', 'source_execution_id', 'source_plan_id') and hasattr(lesson, key):
                setattr(lesson, key, value)
        lesson.updated_at = datetime.now().isoformat()
        with self._db() as db:
            db.execute(f"UPDATE lessons SET {', '.join(f'{c} = ?' for c in _COLUMNS[1:])} WHERE id = ?",
                       (*_row(lesson)[1:], lesson.id))
        return lesson

    def delete(self, lesson_id: str) -> None:
        with self._db() as db:
            db.execute('DELETE FROM lessons WHERE id = ?', (lesson_id,))

    def for_project(self, project_id: str) -> list[Lesson]:
        with self._db() as db:
            rows = db.execute(f"SELECT {', '.join(_COLUMNS)} FROM lessons WHERE module = ?", (project_id,)).fetchall()
        return [_lesson(row) for row in rows]


def _row(lesson: Lesson) -> tuple:
    return (lesson.id, lesson.doc_type, lesson.module, lesson.title, lesson.content,
            json.dumps(lesson.keywords, ensure_ascii=False), lesson.source_execution_id, lesson.source_plan_id,
            json.dumps(lesson.metadata, ensure_ascii=False), lesson.created_at, lesson.updated_at)


def _lesson(row) -> Lesson:
    values = dict(zip(_COLUMNS, row))
    values['keywords'], values['metadata'] = json.loads(values['keywords'] or '[]'), json.loads(values['metadata'] or '{}')
    return Lesson(**values)


def _legacy_lessons() -> list[Lesson]:
    """Lessons from the LanceDB folder of earlier versions; the folder is renamed once they are read."""
    folder = os.path.join(settings.BASE_DIR, LEGACY_DIR)
    if not os.path.isdir(folder):
        return []
    try:
        import lancedb
        rows = lancedb.connect(folder).open_table('rag_documents').to_arrow().to_pylist()
    except ImportError:
        logger.warning('Saved lessons in %s were not moved: they need lancedb to read. '
                       'Install it once (pip install lancedb) and restart to move them.', folder)
        return []
    except Exception as error:
        logger.warning('Saved lessons in %s could not be read: %s', folder, error)
        return []
    lessons = []
    for row in rows:
        if row.get('doc_type') not in KINDS:
            continue  # Report archives of older versions; reports now read history from the facts.
        try:
            lessons.append(Lesson(
                id=row['id'], doc_type=row['doc_type'], module=row['module'], title=row['title'],
                content=row['content'], keywords=json.loads(row.get('keywords') or '[]'),
                source_execution_id=row.get('source_execution_id') or None,
                source_plan_id=row.get('source_plan_id') or None, metadata=json.loads(row.get('metadata') or '{}'),
                created_at=row.get('created_at') or datetime.now().isoformat(),
                updated_at=row.get('updated_at') or datetime.now().isoformat()))
        except (KeyError, TypeError, ValueError) as error:
            logger.warning('Skipped an unreadable saved lesson: %s', error)
    try:
        os.replace(folder, folder + MIGRATED_SUFFIX)
    except OSError as error:
        logger.warning('Could not rename %s after moving its lessons: %s', folder, error)
    logger.info('Moved %d saved lessons from %s', len(lessons), folder)
    return lessons


lesson_store = LessonStore()


def _normalized(title: str) -> str:
    return ' '.join(title.lower().split())


def _active(doc: Lesson) -> bool:
    return doc.metadata.get('status', 'active') == 'active'


def needs_review(doc: Lesson, now: Optional[datetime] = None) -> bool:
    # reviewed_at, not updated_at: counting a citation rewrites the row but is not a review.
    try:
        reviewed = datetime.fromisoformat(doc.metadata.get('reviewed_at') or doc.created_at)
    except (TypeError, ValueError):
        return True
    return (now or datetime.now()) - reviewed > timedelta(days=REVIEW_AFTER_DAYS)


def view(doc: Lesson) -> dict:
    """What the API returns for a lesson."""
    return {'id': doc.id, 'kind': doc.doc_type, 'title': doc.title, 'content': doc.content,
            'labels': doc.keywords, 'project_id': doc.module, 'source_execution_id': doc.source_execution_id,
            'status': doc.metadata.get('status', 'active'), 'cited_count': doc.metadata.get('cited_count', 0),
            'last_cited_at': doc.metadata.get('last_cited_at'), 'needs_review': needs_review(doc),
            'retired_reason': doc.metadata.get('retired_reason'), 'reviewed_at': doc.metadata.get('reviewed_at'),
            'created_at': doc.created_at, 'updated_at': doc.updated_at}


def save(project_id: str, kind: str, title: str, content: str, labels: list[str],
         source_execution_id: Optional[str] = None, source_plan_id: Optional[str] = None) -> Lesson:
    """Save a lesson. The same title of the same kind in a project updates it instead of adding a copy."""
    if kind not in KINDS:
        raise ValueError(f'Unknown kind: {kind}')
    title, content = title.strip(), content.strip()
    if not title:
        raise ValueError('A lesson needs a title')
    doc_id = hashlib.sha256(json.dumps([project_id, kind, _normalized(title)], ensure_ascii=False).encode()).hexdigest()
    labels = sorted({label.strip() for label in labels if label and label.strip()})
    now = datetime.now().isoformat()
    existing = lesson_store.get(doc_id)
    if existing:
        metadata = {**existing.metadata, 'status': 'active', 'reviewed_at': now}
        metadata.pop('retired_reason', None)
        return lesson_store.update(doc_id, title=title, content=content, keywords=labels, metadata=metadata)
    doc = Lesson(id=doc_id, doc_type=kind, module=project_id, title=title, content=content,
                 keywords=labels, source_execution_id=source_execution_id, source_plan_id=source_plan_id,
                 metadata={'status': 'active', 'cited_count': 0, 'created_by': 'user', 'reviewed_at': now})
    lesson_store.add(doc)
    return lesson_store.get(doc_id)


def for_project(project_id: str, *, include_retired: bool = False, limit: int = 200) -> list[Lesson]:
    found = [doc for doc in lesson_store.for_project(project_id)
             if doc.doc_type in KINDS and (include_retired or _active(doc))]
    return sorted(found, key=lambda doc: doc.updated_at, reverse=True)[:limit]


def relevant(project_id: str, labels: set[str]) -> list[Lesson]:
    """Lessons for the requests this run has, then project-wide ones; most cited and newest first."""
    def rank(doc):
        about_run = bool(set(doc.keywords) & labels)
        return (about_run, doc.metadata.get('cited_count', 0), doc.created_at)
    usable = [doc for doc in for_project(project_id) if not doc.keywords or set(doc.keywords) & labels]
    return sorted(usable, key=rank, reverse=True)[:MAX_IN_PROMPT]


def cite(docs: list[Lesson]) -> None:
    """Count each time a lesson reaches a report, so the overview shows which ones are used."""
    now = datetime.now().isoformat()
    for doc in docs:
        metadata = {**doc.metadata, 'cited_count': doc.metadata.get('cited_count', 0) + 1, 'last_cited_at': now}
        try:
            lesson_store.update(doc.id, metadata=metadata)
        except Exception as error:
            logger.warning('Could not count a citation of %s: %s', doc.id, error)


def set_status(doc_id: str, status: str, reason: str = '') -> Optional[Lesson]:
    """Retire a lesson that turned out wrong or outdated, or bring it back. Retired ones stay for the record."""
    doc = lesson_store.get(doc_id)
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
    return lesson_store.update(doc_id, metadata=metadata)


def forget_project(project_id: str) -> int:
    """Lessons of a deleted project, retired ones included."""
    docs = for_project(project_id, include_retired=True)
    for doc in docs:
        lesson_store.delete(doc.id)
    return len(docs)


def format_for_prompt(docs: list[Lesson]) -> str:
    if not docs:
        return ''
    lines = ['\n\n## 团队确认过的经验（人工保存，只作线索，不是本次测试的事实）']
    for doc in docs:
        scope = '、'.join(doc.keywords) if doc.keywords else '整个项目'
        review = '（超过 90 天未更新，可能已过时）' if needs_review(doc) else ''
        lines.append(f'- [{KINDS[doc.doc_type]}｜{scope}] {doc.title}{review}：{doc.content[:600]}')
    return '\n'.join(lines)
