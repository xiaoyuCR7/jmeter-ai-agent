"""RAG document store — LanceDB-backed, searched by keyword and metadata.

Upgrades the original JSON-file store to LanceDB:
- Substring search over each document's text, so Chinese and English match alike
- Metadata filters (module, version, doc type)
- Automatic fallback: if LanceDB is unavailable, falls back to a JSON store

Storage rules (same as before):
- ONLY stores: raw reports, formal analysis conclusions, defect retrospectives,
  high-frequency issue experiences, module test standards.
- NEVER stores: casual chat, temporary fragments, one-off remarks.
"""

import os
import json
import uuid
import re
import logging
import copy
import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)
_FALLBACK_LOCKS: dict[str, threading.RLock] = {}

# Allowed document types (whitelist)
ALLOWED_DOC_TYPES = frozenset({
    "raw_report",
    "analysis_conclusion",
    "defect_retrospective",
    "known_issue",
    "module_standard",
    "hypothesis",  # structured, falsifiable interpretation
})

# Blocked content patterns
BLOCKED_PATTERNS = [
    r"^(你好|嗨|hello|hi)\b",
    r"^(谢谢|感谢|thanks|thank you)\b",
    r"^\?{1,}$",
    r"^.{0,20}$",
]


@dataclass
class RAGDocument:
    """A stored knowledge document in the RAG database."""
    id: str
    doc_type: str
    module: str
    version: str
    title: str
    content: str
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_execution_id: Optional[str] = None
    source_plan_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    context_scope: str = ''

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "doc_type": self.doc_type,
            "module": self.module,
            "version": self.version,
            "title": self.title,
            "content": self.content,
            "keywords": self.keywords,
            "tags": self.tags,
            "source_execution_id": self.source_execution_id,
            "source_plan_id": self.source_plan_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "context_scope": self.context_scope,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RAGDocument":
        return cls(
            id=data["id"],
            doc_type=data["doc_type"],
            module=data["module"],
            version=data.get("version", "unknown"),
            title=data["title"],
            content=data["content"],
            keywords=data.get("keywords", []),
            tags=data.get("tags", []),
            source_execution_id=data.get("source_execution_id"),
            source_plan_id=data.get("source_plan_id"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            context_scope=data.get('context_scope', ''),
        )

    @property
    def search_text(self) -> str:
        """Combined text for keyword search."""
        parts = [self.title, self.module, self.version,
                 " ".join(self.keywords), " ".join(self.tags), self.content]
        return " ".join(p for p in parts if p)


# ── LanceDB RAG Store ─────────────────────────────────────────

class LanceRAGStore:
    """LanceDB-backed RAG document store, searched by keyword and metadata.

    Auto-falls back to a JSON store if LanceDB is unavailable.
    """

    # Older tables have a vector column; rows keep a zero vector so they stay readable.
    VECTOR_DIM = 384

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = settings.RAG_LANCEDB_PATH or os.path.join(
                settings.BASE_DIR, "rag_lancedb"
            )

        self.db_path = db_path
        self._table = None
        self._db = None
        self._docs_cache: dict[str, RAGDocument] = {}  # fast lookup by id
        self._available = False
        self._fallback_path = os.path.join(self.db_path, 'fallback.json')
        self._fallback_lock = _FALLBACK_LOCKS.setdefault(os.path.realpath(self.db_path), threading.RLock())
        self._fallback_signature = None
        with self._write_lease():
            self._init_db()
        if os.path.exists(self._fallback_path):
            # Restore this configured store during construction, including
            # import before lifespan. Public JSON migration remains guarded.
            with self._write_lease():
                with self._fallback_lock:
                    documents = self._load_fallback()
                    if self.available:
                        # Import without overwriting independently updated Lance rows.
                        for doc in documents.values():
                            if self.get(doc.id) is None:
                                self.add(doc)
                        os.replace(self._fallback_path, self._fallback_path + '.migrated')
                    else:
                        self._docs_cache.update(documents)
                        self._fallback_signature = self._fallback_file_signature()

    @contextmanager
    def _write_lease(self):
        # Single process: the instance lock already serializes writers.
        yield

    def _fallback_file_signature(self):
        try:
            stat = os.stat(self._fallback_path)
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        except FileNotFoundError:
            return None

    def _load_fallback(self) -> dict[str, RAGDocument]:
        if not os.path.exists(self._fallback_path):
            return {}
        with open(self._fallback_path, encoding='utf-8') as source:
            return {item['id']: RAGDocument.from_dict(item) for item in json.load(source)['documents']}

    def _persist_fallback(self, doc: Optional[RAGDocument] = None, *, delete_id: Optional[str] = None):
        """Atomic single-process fallback; never publish cache changes before disk success."""
        with self._fallback_lock:
            documents = self._load_fallback()
            if doc is not None:
                documents[doc.id] = copy.deepcopy(doc)
            if delete_id is not None:
                documents.pop(delete_id, None)
            os.makedirs(self.db_path, exist_ok=True)
            temporary = self._fallback_path + '.' + uuid.uuid4().hex + '.tmp'
            try:
                with open(temporary, 'x', encoding='utf-8') as target:
                    json.dump({'documents': [d.to_dict() for d in documents.values()]}, target,
                              ensure_ascii=False, allow_nan=False)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self._fallback_path)
            finally:
                if os.path.exists(temporary):
                    os.remove(temporary)
            self._docs_cache.clear()
            self._docs_cache.update(documents)
            self._fallback_signature = self._fallback_file_signature()

    def _write_document(self, doc: RAGDocument):
        if self.available:
            row = self._doc_to_row(doc, [0.0] * self.VECTOR_DIM)
            (self._table.merge_insert('id').when_matched_update_all()
             .when_not_matched_insert_all().execute([row]))
        else:
            self._persist_fallback(doc)
        self._docs_cache[doc.id] = copy.deepcopy(doc)

    def _list_table_names(self) -> set[str]:
        """Use the current paginated API while retaining old client support."""
        if not hasattr(self._db, 'list_tables'):
            return set(self._db.table_names())

        names = set()
        page_token = None
        while True:
            response = self._db.list_tables(page_token=page_token)
            names.update(response.tables)
            page_token = response.page_token
            if not page_token:
                return names

    def _init_db(self):
        """Initialize LanceDB connection and table."""
        try:
            import lancedb
            os.makedirs(self.db_path, exist_ok=True)
            self._db = lancedb.connect(self.db_path)

            # Check if table exists
            existing = self._list_table_names()
            if "rag_documents" in existing:
                self._table = self._db.open_table("rag_documents")
                if 'context_scope' not in self._table.schema.names:
                    self._table.add_columns({'context_scope': "''"})
                logger.info(f"RAG LanceDB opened: {self.db_path} "
                           f"(table: rag_documents, rows: ~{self._table.count_rows()})")
            else:
                # Create empty table — will add data on first write
                import pyarrow as pa
                schema = pa.schema([
                    ("id", pa.string()),
                    ("doc_type", pa.string()),
                    ("module", pa.string()),
                    ("version", pa.string()),
                    ("title", pa.string()),
                    ("content", pa.string()),
                    ("keywords", pa.string()),      # JSON-encoded list
                    ("tags", pa.string()),           # JSON-encoded list
                    ("source_execution_id", pa.string()),
                    ("source_plan_id", pa.string()),
                    ("metadata", pa.string()),       # JSON-encoded dict
                    ("created_at", pa.string()),
                    ("updated_at", pa.string()),
                    ("context_scope", pa.string()),
                    ("search_text", pa.string()),    # combined text for FTS
                    ("vector", pa.list_(pa.float32(), self.VECTOR_DIM)),
                ])
                self._table = self._db.create_table("rag_documents", schema=schema)
                logger.info(f"RAG LanceDB created: {self.db_path}")

            self._available = True

        except ImportError:
            logger.warning(
                "lancedb not installed. RAG will use JSON fallback. "
                "Install with: pip install lancedb pyarrow"
            )
            self._available = False
        except Exception as e:
            logger.error(f"Failed to initialize LanceDB: {e}. RAG will use JSON fallback.")
            self._available = False

    @property
    def available(self) -> bool:
        return self._available and self._table is not None

    # ── CRUD ──────────────────────────────────────────────

    def _doc_to_row(self, doc: RAGDocument, vector: list) -> dict:
        """Serialize a document into a LanceDB row."""
        return {
            "id": doc.id,
            "doc_type": doc.doc_type,
            "module": doc.module,
            "version": doc.version,
            "title": doc.title,
            "content": doc.content,
            "keywords": json.dumps(doc.keywords, ensure_ascii=False),
            "tags": json.dumps(doc.tags, ensure_ascii=False),
            "source_execution_id": doc.source_execution_id or "",
            "source_plan_id": doc.source_plan_id or "",
            "metadata": json.dumps(doc.metadata, ensure_ascii=False),
            "created_at": doc.created_at,
            "updated_at": doc.updated_at,
            "context_scope": doc.context_scope,
            "search_text": doc.search_text,
            "vector": vector,
        }

    def add(self, doc: RAGDocument) -> str:
        with self._write_lease():
            return self._add_locked(doc)

    def _add_locked(self, doc: RAGDocument) -> str:
        """Add a document. Returns doc ID."""
        if doc.doc_type not in ALLOWED_DOC_TYPES:
            raise ValueError(f"Document type '{doc.doc_type}' not allowed.")
        if not self._validate_content(doc.content):
            raise ValueError("Document content rejected by quality filter.")
        if not doc.id:
            doc.id = str(uuid.uuid4())
        else:
            existing = self._get_raw(doc.id)
            if existing is not None:
                raise ValueError(f'Document {doc.id} already exists; use update')

        now = datetime.now().isoformat()
        if not doc.created_at:
            doc.created_at = now
        doc.updated_at = now

        self._write_document(doc)
        return doc.id

    @staticmethod
    def _safe_where(field: str, value: str) -> str:
        """Build a safe LanceDB where clause by escaping single quotes."""
        # LanceDB uses SQL-like syntax; single quotes must be doubled.
        safe_value = str(value).replace("'", "''")
        return f"{field} = '{safe_value}'"

    def get(self, doc_id: str) -> Optional[RAGDocument]:
        """Get a document by ID."""
        doc = self._get_raw(doc_id)
        return doc

    def _get_raw(self, doc_id: str) -> Optional[RAGDocument]:
        """Internal identity lookup, never treat a hidden ID as a new document."""
        if doc_id in self._docs_cache:
            return copy.deepcopy(self._docs_cache[doc_id])

        if self.available:
            try:
                result = self._table.search().where(self._safe_where('id', doc_id)).limit(1).to_list()
                if result:
                    doc = self._row_to_doc(result[0])
                    self._docs_cache[doc.id] = doc
                    return copy.deepcopy(doc)
            except Exception as e:
                logger.error(f"LanceDB get failed for {doc_id}: {e}")
                raise

        return copy.deepcopy(self._docs_cache.get(doc_id))

    def update(self, doc_id: str, **updates) -> Optional[RAGDocument]:
        with self._write_lease():
            return self._update_locked(doc_id, **updates)

    def _update_locked(self, doc_id: str, **updates) -> Optional[RAGDocument]:
        """Update a document. Returns updated doc or None."""
        doc = self._get_raw(doc_id)
        if not doc:
            return None
        if any(key in updates and updates[key] != getattr(doc, key)
               for key in ('source_execution_id', 'source_plan_id', 'context_scope')):
            raise ValueError('Document source identity cannot be changed')

        for key, value in updates.items():
            if key not in ('id', 'created_at') and hasattr(doc, key):
                setattr(doc, key, value)
        if doc.doc_type not in ALLOWED_DOC_TYPES or not self._validate_content(doc.content):
            raise ValueError('Document update rejected by quality filter')
        doc.updated_at = datetime.now().isoformat()
        self._write_document(doc)
        return doc

    def delete(self, doc_id: str) -> bool:
        with self._write_lease():
            return self._delete_locked(doc_id)

    def _delete_locked(self, doc_id: str) -> bool:
        """Delete a document."""
        if self.available:
            self._table.delete(self._safe_where("id", doc_id))
        else:
            self._persist_fallback(delete_id=doc_id)
        self._docs_cache.pop(doc_id, None)
        return True

    # ── Search ────────────────────────────────────────────

    def _visible(self, documents):
        return [copy.deepcopy(doc) for doc in documents]

    @staticmethod
    def _where(condition=None):
        return condition or '1 = 1'

    def search_by_keywords(self, keywords: list[str], limit: int = 20) -> list[RAGDocument]:
        """Documents containing any of the keywords."""
        return self._fallback_keyword_search(keywords, limit) if keywords else []

    def search_by_module(self, module: str, limit: int = 20) -> list[RAGDocument]:
        """Find all documents for a module."""
        if self.available:
            try:
                return self._latest_rows(limit, where=self._safe_where("module", module))
            except Exception as e:
                logger.error(f"LanceDB module search failed: {e}")

        # Fallback
        docs = [d for d in self._visible(self._docs_cache.values()) if module == d.module]
        return sorted(docs, key=lambda d: (d.created_at, d.id), reverse=True)[:limit]

    def search_by_type(self, doc_type: str, limit: int = 20) -> list[RAGDocument]:
        """Find all documents of a given type."""
        if self.available:
            try:
                return self._latest_rows(limit, where=self._safe_where("doc_type", doc_type))
            except Exception:
                pass

        docs = [d for d in self._visible(self._docs_cache.values()) if d.doc_type == doc_type]
        return sorted(docs, key=lambda d: (d.created_at, d.id), reverse=True)[:limit]

    def hybrid_search(
        self,
        keywords: list[str] = None,
        module: str = None,
        doc_type: str = None,
        version: str = None,
        query_text: str = None,
        limit: int = 20,
        context_scope: Optional[str] = None,
    ) -> list[RAGDocument]:
        """Newest documents matching the metadata filters and containing any keyword.

        Args:
            keywords: Documents must contain at least one of these.
            module: Filter by module.
            doc_type: Filter by document type.
            version: Filter by version.
            query_text: A single search term, used when no keywords are given.
            limit: Max results.
        """
        keywords = keywords or []
        filters = [self._safe_where(field, value) for field, value in (
            ("module", module), ("doc_type", doc_type), ("version", version)
        ) if value]
        if context_scope is not None:
            filters.append(self._safe_where('context_scope', context_scope))

        terms = keywords or ([query_text] if query_text else [])
        if self.available:
            try:
                clauses = list(filters)
                if terms:
                    clauses.append(self._keyword_where(terms))
                return self._latest_rows(limit, where=" AND ".join(clauses) or None)
            except Exception as e:
                logger.error(f"LanceDB filtered search failed: {e}")

        # Cache fallback is still constrained by the same query and metadata.
        candidates = self._visible(self._docs_cache.values())

        if module:
            candidates = [d for d in candidates if module == d.module]
        if doc_type:
            candidates = [d for d in candidates if d.doc_type == doc_type]
        if version:
            candidates = [d for d in candidates if version == d.version]
        if context_scope is not None:
            candidates = [d for d in candidates if context_scope == d.context_scope]
        if terms:
            candidates = [d for d in candidates if any(term.lower() in d.search_text.lower() for term in terms)]

        return sorted(candidates, key=lambda d: (d.created_at, d.id), reverse=True)[:limit]

    def full_text_search(self, query: str, limit: int = 20) -> list[RAGDocument]:
        """Full-text search across all document text."""
        return self.hybrid_search(query_text=query, limit=limit)

    def get_module_versions(self, module: str) -> list[str]:
        """Get all known versions for a module."""
        if self.available:
            rows = self._table.search().select(['version']).where(
                self._where(self._safe_where('module', module))).limit(None).to_list()
            return sorted({row['version'] for row in rows if row.get('version')}, reverse=True)
        return sorted({d.version for d in self._visible(self._docs_cache.values()) if d.module == module and d.version},
                      reverse=True)

    def get_latest_analysis(self, module: str) -> Optional[RAGDocument]:
        """Get the most recent analysis conclusion for a module."""
        docs = self.hybrid_search(module=module, doc_type="analysis_conclusion", limit=1)
        return docs[0] if docs else None

    def list_all(self, limit: int = 50, offset: int = 0) -> list[RAGDocument]:
        """List all documents with pagination. Returns docs sorted by created_at desc."""
        if self.available:
            try:
                return self._latest_rows(limit, offset)
            except Exception as e:
                logger.error(f"LanceDB list_all failed: {e}")
        docs = sorted(self._visible(self._docs_cache.values()), key=lambda d: d.created_at, reverse=True)
        return docs[offset:offset + limit]

    def _latest_rows(self, limit: int, offset: int = 0, where: str = None) -> list[RAGDocument]:
        query = self._table.search().select(['id', 'created_at'])
        query = query.where(self._where(where))
        index = query.limit(None).to_list()
        index.sort(key=lambda row: (row['created_at'], row['id']), reverse=True)
        selected = index[offset:offset + limit]
        if not selected:
            return []
        clause = ' OR '.join(self._safe_where('id', row['id']) for row in selected)
        rows = self._table.search().where(clause).limit(len(selected)).to_list()
        documents = {row['id']: self._row_to_doc(row) for row in rows}
        return self._visible(documents[row['id']] for row in selected if row['id'] in documents)

    def count(self) -> int:
        if self.available:
            try:
                result = self._table.count_rows(self._where())
                return result
            except Exception:
                pass
        return len(self._visible(self._docs_cache.values()))

    def get_stats(self) -> dict:
        """Get store statistics."""
        by_type = {}
        by_module = {}
        rows = (self._table.search().select(['doc_type', 'module']).where(self._where()).limit(None).to_list() if self.available
                else [{'doc_type': doc.doc_type, 'module': doc.module} for doc in self._visible(self._docs_cache.values())])
        for row in rows:
            by_type[row['doc_type']] = by_type.get(row['doc_type'], 0) + 1
            by_module[row['module']] = by_module.get(row['module'], 0) + 1

        return {
            "total_documents": self.count(),
            "backend": "lancedb" if self.available else "json_fallback",
            "db_path": self.db_path,
            "by_type": by_type,
            "by_module": by_module,
        }

    # ── Internal ──────────────────────────────────────────

    def _validate_content(self, content: str) -> bool:
        if not content or len(content.strip()) < 20:
            return False
        text = content.strip()
        for pattern in BLOCKED_PATTERNS:
            if re.match(pattern, text, re.IGNORECASE):
                return False
        return True

    def _row_to_doc(self, row: dict) -> RAGDocument:
        """Convert LanceDB row dict to RAGDocument."""
        return RAGDocument(
            id=row["id"],
            doc_type=row["doc_type"],
            module=row["module"],
            version=row.get("version", "unknown"),
            title=row["title"],
            content=row["content"],
            keywords=json.loads(row.get("keywords", "[]"))
                if isinstance(row.get("keywords"), str) else (row.get("keywords") or []),
            tags=json.loads(row.get("tags", "[]"))
                if isinstance(row.get("tags"), str) else (row.get("tags") or []),
            source_execution_id=row.get("source_execution_id") or None,
            source_plan_id=row.get("source_plan_id") or None,
            metadata=json.loads(row.get("metadata", "{}"))
                if isinstance(row.get("metadata"), str) else (row.get("metadata") or {}),
            created_at=row.get("created_at", datetime.now().isoformat()),
            updated_at=row.get("updated_at", datetime.now().isoformat()),
            context_scope=row.get('context_scope') or '',
        )

    @staticmethod
    def _keyword_where(keywords: list[str]) -> str:
        # contains treats %, _ and quotes as literal search data, not SQL wildcards.
        literals = [str(keyword).lower().replace("'", "''") for keyword in keywords if keyword]
        return "(" + " OR ".join(f"contains(lower(search_text), '{word}')" for word in literals) + ")"

    def _fallback_keyword_search(self, keywords: list[str], limit: int) -> list[RAGDocument]:
        """Substring search, ranked by how many keywords a document contains when LanceDB is unavailable."""
        if self.available and any(keywords):
            try:
                rows = self._table.search().where(self._where(self._keyword_where(keywords))).limit(limit).to_list()
                return self._visible(self._row_to_doc(row) for row in rows)
            except Exception as e:
                logger.warning(f"LanceDB substring search failed: {e}")
        results = []
        for doc in self._visible(self._docs_cache.values()):
            text = doc.search_text.lower()
            score = sum(1 for kw in keywords if kw.lower() in text)
            if score > 0:
                results.append((score, doc))
        results.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in results[:limit]]

    # ── Migration from old JSON store ─────────────────────

    def migrate_from_json(self, json_path: str) -> int:
        """Import documents from old JSON RAG store. Returns count of imported docs."""
        if not os.path.exists(json_path):
            logger.info(f"No JSON RAG store to migrate: {json_path}")
            return 0

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read JSON RAG store for migration: {e}")
            return 0

        docs = data.get("documents", [])
        imported = 0
        with self._write_lease():
            for doc_data in docs:
                try:
                    doc = RAGDocument.from_dict(doc_data)
                    self.add(doc)
                    imported += 1
                except Exception as e:
                    logger.warning(f"Migration: skipped document {doc_data.get('id', '?')}: {e}")

        logger.info(f"RAG migration complete: {imported}/{len(docs)} documents imported from {json_path}")
        return imported


# Module singleton
rag_store = LanceRAGStore()
