"""Fact Ledger — deterministic, ground-truth metrics computed from raw JTL.

Design principles:
- Facts are NOT produced by LLM. They are computed deterministically from JTL files.
- Every numeric claim in an LLM analysis must be re-derivable from the FactSheet.
- JTL files are immutable events; FactSheet is a projection that can always be rebuilt.
"""

import os
import json
import logging
import hashlib
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import HTTPException

from app.core.config import settings
from app.models.schemas import FactSheet, DetailedTestSummary
from app.services.jmeter.report_parser import ReportParser
from app.services.ai.memory.read_errors import ledger_read_boundary

logger = logging.getLogger(__name__)


class FactLedger:
    """Stores and retrieves deterministic FactSheets per execution."""

    SCHEMA_VERSION = "1.0"

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = os.path.join(settings.BASE_DIR, "facts.db")
        self.db_path = db_path
        self._db_conn = None  # Persistent connection — initialized once, reused
        # Lazy lock creation: creating asyncio.Lock at import time (no running loop)
        # on Windows can cause hangs with aiosqlite. The lock is created on first use.
        self._db_lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._db_lock is None:
            self._db_lock = asyncio.Lock()
        return self._db_lock

    async def _get_db(self):
        """Return persistent aiosqlite connection. Creates on first call, reuses thereafter."""
        if self._db_conn is not None:
            return self._db_conn
        import aiosqlite
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
        except (Exception, asyncio.CancelledError):
            try:
                await db.close()
            except Exception:
                logger.warning('Failed to close uninitialized fact connection')
            raise
        self._db_conn = db
        return db

    async def close(self):
        """Close the persistent database connection (safe to call multiple times)."""
        if self._db_conn is not None:
            try:
                await self._db_conn.close()
            except Exception:
                pass
            self._db_conn = None

    async def _ensure_table(self):
        async with self._get_lock():
            db = await self._get_db()
            await db.execute("""
                CREATE TABLE IF NOT EXISTS fact_sheets (
                    execution_id TEXT PRIMARY KEY,
                    module TEXT,
                    version TEXT,
                    test_type TEXT,
                    jtl_path TEXT,
                    jtl_hash TEXT,
                    schema_version TEXT NOT NULL,
                    computed_at TEXT NOT NULL,
                    facts_json TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_facts_module_version
                ON fact_sheets(module, version)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_facts_computed_at
                ON fact_sheets(computed_at)
            """)
            await db.commit()

    @staticmethod
    def compute_from_jtl(
        jtl_path: str,
        execution_id: str,
        module: Optional[str] = None,
        version: Optional[str] = None,
        test_type: Optional[str] = None,
    ) -> FactSheet:
        """Compute a FactSheet deterministically from a JTL file.

        This is the only sanctioned way to produce facts.
        """
        if not os.path.exists(jtl_path):
            raise FileNotFoundError(f"JTL file not found: {jtl_path}")

        parser = ReportParser()
        digest = FactLedger._jtl_hash(jtl_path)
        summary = parser.parse_jtl(jtl_path)
        fact = FactLedger.compute_from_summary(
            summary=summary,
            execution_id=execution_id,
            jtl_path=jtl_path,
            module=module,
            version=version,
            test_type=test_type,
        )
        if fact.jtl_sha256 != digest:
            raise ValueError('JTL changed during fact computation')
        return fact

    @staticmethod
    def compute_from_summary(
        summary: DetailedTestSummary,
        execution_id: str,
        jtl_path: Optional[str] = None,
        module: Optional[str] = None,
        version: Optional[str] = None,
        test_type: Optional[str] = None,
    ) -> FactSheet:
        """Compute a FactSheet from an already-parsed DetailedTestSummary."""
        return FactSheet(
            execution_id=execution_id,
            module=module or "unknown",
            version=version or "unknown",
            test_type=test_type or "unknown",
            jtl_path=jtl_path,
            jtl_sha256=FactLedger._jtl_hash(jtl_path) if jtl_path else '',
            schema_version=FactLedger.SCHEMA_VERSION,
            computed_at=datetime.now(),
            total_requests=summary.total_requests,
            avg_response_time=summary.avg_response_time,
            min_response_time=summary.min_response_time,
            max_response_time=summary.max_response_time,
            error_rate=summary.error_rate,
            throughput=summary.throughput,
            received_bytes_per_sec=summary.received_bytes_per_sec,
            sent_bytes_per_sec=summary.sent_bytes_per_sec,
            percentiles=summary.percentiles,
            per_label=summary.per_label,
            timeline_segments=summary.timeline_segments,
            error_details=summary.error_details,
            performance_trend=summary.performance_trend,
            raw_summary=summary,
        )

    @staticmethod
    def _jtl_hash(jtl_path: str) -> str:
        """Compute a cheap content hash for the JTL file."""
        if not os.path.exists(jtl_path):
            return ""
        h = hashlib.sha256()
        with open(jtl_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    async def save(self, fact: FactSheet) -> bool:
        """Persist a FactSheet."""
        # Freeze both indexed fields and nested JSON before the first suspension.
        values = {name: getattr(fact, name) for name in (
            'execution_id', 'module', 'version', 'test_type', 'jtl_path', 'schema_version')}
        values['computed_at'] = fact.computed_at.isoformat()
        values['facts_json'] = fact.model_dump_json()
        expected_hash = fact.jtl_sha256
        await self._ensure_table()

        jtl_path = values['jtl_path']
        jtl_hash = await asyncio.to_thread(self._jtl_hash, jtl_path) if jtl_path else ""
        if jtl_path and (not jtl_hash or (expected_hash and expected_hash != jtl_hash)):
            logger.error('Refusing fact save: source JTL changed or is unavailable')
            return False
        values['jtl_hash'] = jtl_hash

        async with self._get_lock():
            db = await self._get_db()
            try:
                await db.execute(
                    """INSERT OR REPLACE INTO fact_sheets
                       (execution_id, module, version, test_type, jtl_path, jtl_hash,
                        schema_version, computed_at, facts_json)
                       VALUES (:execution_id, :module, :version, :test_type, :jtl_path,
                               :jtl_hash, :schema_version, :computed_at, :facts_json)""",
                    values,
                )
                await db.commit()
                return True
            except HTTPException:
                raise
            except (Exception, asyncio.CancelledError) as e:
                logger.error('Failed to save fact sheet for %s: %s', values['execution_id'], e)
                try:
                    await db.rollback()
                except Exception:
                    await self.close()
                if isinstance(e, asyncio.CancelledError):
                    raise
                return False

    async def load(self, execution_id: str, *, strict: bool = False) -> Optional[FactSheet]:
        """Load a persisted FactSheet."""
        with ledger_read_boundary(strict, 'fact', 'load'):
            await self._ensure_table()

            async with self._get_lock():
                db = await self._get_db()
                try:
                    cursor = await db.execute(
                        "SELECT facts_json, jtl_hash, execution_id FROM fact_sheets WHERE execution_id = ?",
                        (execution_id,),
                    )
                    row = await cursor.fetchone()
                    if not row:
                        return None
                    return await self._verified_row(row)
                except HTTPException:
                    raise
                except Exception as e:
                    if strict:
                        raise
                    logger.error(f"Failed to load fact sheet for {execution_id}: {e}")
                    return None

    async def get_facts_for_module_version(
        self,
        module: str,
        version: Optional[str] = None,
        limit: int = 20,
        context_scope: Optional[str] = None,
        *, strict: bool = False,
    ) -> list[FactSheet]:
        """Retrieve historical facts for a module, optionally filtered by version."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError('limit must be a non-negative integer')
        if limit == 0:
            return []
        with ledger_read_boundary(strict, 'fact', 'history'):
            await self._ensure_table()

            async with self._get_lock():
                db = await self._get_db()
                try:
                    query = 'SELECT facts_json, jtl_hash, execution_id FROM fact_sheets WHERE module = ?'
                    params = [module]
                    if version:
                        query += ' AND version = ?'
                        params.append(version)
                    if context_scope is not None:
                        query += " AND COALESCE(CASE WHEN json_valid(facts_json) THEN json_extract(facts_json, '$.context_scope') END, '') = ?"
                        params.append(context_scope)
                    query += ' ORDER BY computed_at DESC, execution_id DESC'
                    verified = []
                    # The limit applies to usable facts, not corrupt/stale candidates.
                    # Iterate in batches instead of materializing the entire history.
                    async with db.execute(query, params) as cursor:
                        async for row in cursor:
                            fact = await self._verified_row(row)
                            if fact is not None:
                                verified.append(fact)
                                if len(verified) == limit:
                                    break
                    return verified
                except HTTPException:
                    raise
                except Exception as e:
                    if strict:
                        raise
                    logger.error(f"Failed to list facts for {module}/{version}: {e}")
                    return []

    async def _verified_row(self, row) -> Optional[FactSheet]:
        try:
            fact = FactSheet.model_validate_json(row[0])
            if fact.execution_id != row[2]:
                logger.warning('Skipping fact row: execution identity mismatch')
                return None
            if fact.jtl_path and row[1]:
                digest = await asyncio.to_thread(self._jtl_hash, fact.jtl_path)
                # Legacy rows stored a 16-character SHA256 prefix; retain compatibility.
                if not digest or not digest.startswith(row[1]):
                    logger.warning('Skipping fact %s: JTL hash mismatch or file unavailable', fact.execution_id)
                    return None
            return fact
        except (ValueError, TypeError, OSError) as error:
            logger.warning('Skipping unreadable fact row: %s', type(error).__name__)
            return None

    async def get_project_facts(self, project_id: str, limit: int = 50) -> list[FactSheet]:
        """Newest runs of every plan in a project whose source JTL is still intact."""
        if not project_id or limit <= 0:
            return []
        await self._ensure_table()
        query = ("SELECT facts_json, jtl_hash, execution_id FROM fact_sheets "
                 "WHERE json_valid(facts_json) AND json_extract(facts_json, '$.project_id') = ? "
                 "ORDER BY computed_at DESC, execution_id DESC")
        async with self._get_lock():
            db = await self._get_db()
            found = []
            async with db.execute(query, (project_id,)) as cursor:
                async for row in cursor:
                    fact = await self._verified_row(row)
                    if fact is not None:
                        found.append(fact)
                        if len(found) == limit:
                            break
            return found

    async def count_project_facts(self, project_id: str) -> int:
        await self._ensure_table()
        async with self._get_lock():
            db = await self._get_db()
            cursor = await db.execute(
                "SELECT COUNT(*) FROM fact_sheets WHERE json_valid(facts_json) "
                "AND json_extract(facts_json, '$.project_id') = ?", (project_id,))
            return (await cursor.fetchone())[0]

    async def forget_project(self, project_id: str, execution_ids: list[str]) -> int:
        """A deleted project's runs, including ones recorded before facts carried their project."""
        await self._ensure_table()
        async with self._get_lock():
            db = await self._get_db()
            cursor = await db.execute(
                "DELETE FROM fact_sheets WHERE (json_valid(facts_json) AND json_extract(facts_json, '$.project_id') = ?)"
                " OR execution_id IN (SELECT value FROM json_each(?))", (project_id, json.dumps(execution_ids)))
            await db.commit()
            return cursor.rowcount

    async def annotate(self, execution_id: str, *, only_missing: bool = False, **fields) -> bool:
        """Add context recorded after the fact (project, environment, load); the measurements stay as they were.

        ``only_missing`` leaves a fact that already has its context alone. True when it wrote something.
        """
        await self._ensure_table()
        async with self._get_lock():
            db = await self._get_db()
            cursor = await db.execute('SELECT facts_json FROM fact_sheets WHERE execution_id = ?', (execution_id,))
            row = await cursor.fetchone()
            if not row:
                return False
            data = json.loads(row[0])
            if only_missing:
                fields = {key: value for key, value in fields.items() if not data.get(key)}
            if all(data.get(key) == value for key, value in fields.items()):
                return False
            data.update(fields)
            await db.execute('UPDATE fact_sheets SET facts_json = ? WHERE execution_id = ?',
                             (json.dumps(data, ensure_ascii=False, default=str), execution_id))
            await db.commit()
            return True

    async def get_latest_fact(self, module: str, version: Optional[str] = None) -> Optional[FactSheet]:
        """Get the most recent fact for a module/version."""
        facts = await self.get_facts_for_module_version(module, version, limit=1)
        return facts[0] if facts else None

    async def rebuild_fact(self, execution_id: str) -> Optional[FactSheet]:
        """Rebuild a FactSheet from the original JTL file.

        This is the event-sourcing recovery path: if a fact is suspected to be
        wrong, recompute it from the immutable JTL event.
        """
        old = await self.load(execution_id)
        if not old or not old.jtl_path or not os.path.exists(old.jtl_path):
            logger.warning(f"Cannot rebuild fact {execution_id}: JTL missing")
            return None

        new_fact = await asyncio.to_thread(self.compute_from_jtl,
            jtl_path=old.jtl_path,
            execution_id=execution_id,
            module=old.module,
            version=old.version,
            test_type=old.test_type,
        )
        new_fact.context_scope = old.context_scope
        if not await self.save(new_fact):
            return None
        return new_fact


# Module singleton
fact_ledger = FactLedger()
