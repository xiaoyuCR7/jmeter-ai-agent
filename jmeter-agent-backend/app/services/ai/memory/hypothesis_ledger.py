"""Hypothesis Ledger — interpretations are stored as falsifiable hypotheses, not facts.

Each interpretation (bottleneck, root cause, recommendation) is a hypothesis with:
- source execution
- status: untested / confirmed / refuted / deprecated
- confidence
- supporting facts (metric field references)
- testable prediction + falsification design

Later runs of the same project check structured predictions and update status. A prediction
that held says only that the numbers moved as predicted; the explanation stays a hypothesis.
"""

import os
import uuid
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import HTTPException

from app.core.config import settings
from app.models.schemas import (
    Hypothesis, HypothesisStatus, HypothesisCategory, FactSheet,
)
from app.services.ai.memory import prediction as predictions
from app.services.ai.memory.fact_ledger import fact_ledger
from app.services.ai.memory.read_errors import ledger_read_boundary

logger = logging.getLogger(__name__)
REF_LENGTH = 8  # Characters of a hypothesis id a prompt shows and a report quotes back.


class HypothesisUpdateConflict(RuntimeError):
    """The hypothesis changed after the caller read it; no update was applied."""


class HypothesisLedger:
    """Stores and evaluates hypotheses."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = os.path.join(settings.BASE_DIR, "hypotheses.db")
        self.db_path = db_path
        self._db_conn = None  # Persistent connection
        # Lazy lock creation: creating asyncio.Lock at import time (no running loop)
        # on Windows can cause hangs with aiosqlite. The lock is created on first use.
        self._db_lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._db_lock is None:
            self._db_lock = asyncio.Lock()
        return self._db_lock

    async def _get_db(self):
        """Return persistent aiosqlite connection."""
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
                logger.warning('Failed to close uninitialized hypothesis connection')
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
                CREATE TABLE IF NOT EXISTS hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    source_execution_id TEXT NOT NULL,
                    module TEXT NOT NULL,
                    version TEXT,
                    test_type TEXT,
                    category TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    status TEXT NOT NULL DEFAULT 'untested',
                    supporting_facts TEXT NOT NULL DEFAULT '[]',
                    testable_prediction TEXT,
                    falsification_design TEXT,
                    verified_by_execution_id TEXT,
                    refuted_by_execution_id TEXT,
                    verification_notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT '',
                    prediction_json TEXT
                )
            """)
            # Tables from before projects and structured predictions get the columns at the end.
            columns = {row[1] async for row in await db.execute('PRAGMA table_info(hypotheses)')}
            if 'project_id' not in columns:
                await db.execute("ALTER TABLE hypotheses ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
            if 'prediction_json' not in columns:
                await db.execute('ALTER TABLE hypotheses ADD COLUMN prediction_json TEXT')
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_hyp_project
                ON hypotheses(project_id, status)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_hyp_module_version
                ON hypotheses(module, version)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_hyp_status
                ON hypotheses(status)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_hyp_source_exec
                ON hypotheses(source_execution_id)
            """)
            await db.commit()

    @staticmethod
    def _row_to_hypothesis(row) -> Hypothesis:
        return Hypothesis(
            hypothesis_id=row[0],
            source_execution_id=row[1],
            module=row[2],
            version=row[3] or None,
            test_type=row[4] or None,
            category=HypothesisCategory(row[5]),
            statement=row[6],
            confidence=row[7],
            status=HypothesisStatus(row[8]),
            supporting_facts=json.loads(row[9]) if row[9] else [],
            testable_prediction=row[10] or None,
            falsification_design=row[11] or None,
            verified_by_execution_id=row[12] or None,
            refuted_by_execution_id=row[13] or None,
            verification_notes=row[14] or None,
            created_at=datetime.fromisoformat(row[15]),
            updated_at=datetime.fromisoformat(row[16]),
            project_id=row[17] if len(row) > 17 else '',
            prediction=json.loads(row[18]) if len(row) > 18 and row[18] else None,
        )

    async def add(
        self,
        hypothesis: Hypothesis,
        *,
        if_absent: bool = False,
    ) -> str:
        """Add a hypothesis; optionally preserve an existing ID without replacing it."""
        if not hypothesis.hypothesis_id:
            hypothesis.hypothesis_id = str(uuid.uuid4())[:8]
        hypothesis_id = hypothesis.hypothesis_id
        now = datetime.now().isoformat()
        # Capture mutable input before initialization, lock, or commit can suspend.
        values = (
            hypothesis_id, hypothesis.source_execution_id, hypothesis.module,
            hypothesis.version, hypothesis.test_type, hypothesis.category.value,
            hypothesis.statement, hypothesis.confidence, hypothesis.status.value,
            json.dumps(hypothesis.supporting_facts, ensure_ascii=False),
            hypothesis.testable_prediction, hypothesis.falsification_design,
            hypothesis.verified_by_execution_id, hypothesis.refuted_by_execution_id,
            hypothesis.verification_notes, now, now, hypothesis.project_id,
            json.dumps(hypothesis.prediction, ensure_ascii=False) if hypothesis.prediction else None,
        )
        conflict_clause = ' ON CONFLICT(hypothesis_id) DO NOTHING' if if_absent else ''
        await self._ensure_table()

        async with self._get_lock():
            db = await self._get_db()
            try:
                cursor = await db.execute(
                    'SELECT source_execution_id FROM hypotheses WHERE hypothesis_id = ?', (hypothesis_id,))
                existing = await cursor.fetchone()
                if existing and existing[0] != values[1]:
                    raise HTTPException(403, 'Hypothesis source identity cannot be changed')
                await db.execute(
                    """INSERT INTO hypotheses
                       (hypothesis_id, source_execution_id, module, version, test_type,
                        category, statement, confidence, status, supporting_facts,
                        testable_prediction, falsification_design,
                        verified_by_execution_id, refuted_by_execution_id,
                        verification_notes, created_at, updated_at, project_id, prediction_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""" + conflict_clause,
                    values,
                )
                await db.commit()
                return hypothesis_id
            except (Exception, asyncio.CancelledError) as e:
                logger.error(f"Failed to add hypothesis: {e}")
                try:
                    await db.rollback()
                except Exception:
                    await self.close()
                raise

    async def update_status(
        self,
        hypothesis_id: str,
        status: HypothesisStatus,
        verified_by: Optional[str] = None,
        refuted_by: Optional[str] = None,
        notes: Optional[str] = None,
        *,
        expected_status: Optional[HypothesisStatus] = None,
        expected_updated_at: Optional[datetime] = None,
    ) -> bool:
        """Update status; optional read-version guards reject stale decisions."""
        await self._ensure_table()

        async with self._get_lock():
            db = await self._get_db()
            try:
                fields = ["status = ?", "updated_at = ?"]
                params = [status.value, datetime.now().isoformat()]

                if status == HypothesisStatus.CONFIRMED:
                    fields.extend([
                        "verified_by_execution_id = COALESCE(?, CASE WHEN status = ? THEN verified_by_execution_id END)",
                        "refuted_by_execution_id = NULL",
                    ])
                    params.extend([verified_by, status.value])
                elif status == HypothesisStatus.REFUTED:
                    fields.extend([
                        "refuted_by_execution_id = COALESCE(?, CASE WHEN status = ? THEN refuted_by_execution_id END)",
                        "verified_by_execution_id = NULL",
                    ])
                    params.extend([refuted_by, status.value])
                elif status == HypothesisStatus.UNTESTED:
                    fields.extend(["verified_by_execution_id = NULL", "refuted_by_execution_id = NULL"])
                # A new state must not inherit the previous state's explanation.
                # Deprecation keeps the old evidence for historical reference.
                if notes is not None:
                    fields.append("verification_notes = ?")
                    params.append(notes)
                elif status != HypothesisStatus.DEPRECATED:
                    fields.append("verification_notes = CASE WHEN status = ? THEN verification_notes END")
                    params.append(status.value)

                params.append(hypothesis_id)
                where = 'hypothesis_id = ?'
                if expected_status is not None:
                    where += ' AND status = ?'
                    params.append(expected_status.value)
                if expected_updated_at is not None:
                    where += ' AND updated_at = ?'
                    params.append(expected_updated_at.isoformat())

                cursor = await db.execute(
                    f"UPDATE hypotheses SET {', '.join(fields)} WHERE {where}",
                    tuple(params),
                )
                await db.commit()
                if cursor.rowcount == 0 and (expected_status is not None or expected_updated_at is not None):
                    raise HypothesisUpdateConflict(hypothesis_id)
                return cursor.rowcount == 1
            except HypothesisUpdateConflict:
                raise
            except HTTPException:
                raise
            except (Exception, asyncio.CancelledError) as e:
                logger.error(f"Failed to update hypothesis {hypothesis_id}: {e}")
                try:
                    await db.rollback()
                except Exception:
                    # Do not reuse a connection with an unacknowledged write.
                    await self.close()
                if isinstance(e, asyncio.CancelledError):
                    raise
                return False

    async def get(self, hypothesis_id: str, *, strict: bool = False) -> Optional[Hypothesis]:
        """Get a single hypothesis by ID."""
        with ledger_read_boundary(strict, 'hypothesis', 'get'):
            await self._ensure_table()

            async with self._get_lock():
                db = await self._get_db()
                try:
                    cursor = await db.execute(
                        "SELECT * FROM hypotheses WHERE hypothesis_id = ?",
                        (hypothesis_id,),
                    )
                    row = await cursor.fetchone()
                    try:
                        return self._row_to_hypothesis(row) if row else None
                    except (ValueError, TypeError) as error:
                        if not strict:
                            raise
                        logger.warning('Skipping malformed hypothesis row: %s', type(error).__name__)
                        return None
                except HTTPException:
                    raise
                except Exception as e:
                    if strict:
                        raise
                    logger.error(f"Failed to get hypothesis {hypothesis_id}: {e}")
                    return None

    async def list_for_module(
        self,
        module: str,
        version: Optional[str] = None,
        status: Optional[HypothesisStatus] = None,
        limit: int = 50,
        source_execution_ids: Optional[list[str]] = None,
        *, strict: bool = False,
    ) -> list[Hypothesis]:
        """List hypotheses for a module/version, optionally filtered by status."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError('limit must be a non-negative integer')
        if limit == 0 or (source_execution_ids is not None and not source_execution_ids):
            return []
        with ledger_read_boundary(strict, 'hypothesis', 'history'):
            await self._ensure_table()

            async with self._get_lock():
                db = await self._get_db()
                try:
                    query = "SELECT * FROM hypotheses WHERE module = ?"
                    params = [module]
                    if version:
                        query += " AND version = ?"
                        params.append(version)
                    if status:
                        query += " AND status = ?"
                        params.append(status.value)
                    if source_execution_ids is not None:
                        query += ' AND source_execution_id IN (' + ','.join('?' for _ in source_execution_ids) + ')'
                        params.extend(source_execution_ids)
                    query += " ORDER BY updated_at DESC, hypothesis_id DESC"
                    hypotheses = []
                    async with db.execute(query, tuple(params)) as cursor:
                        async for row in cursor:
                            try:
                                hyp = self._row_to_hypothesis(row)
                            except (ValueError, TypeError) as error:
                                logger.warning('Skipping malformed hypothesis row: %s', type(error).__name__)
                                continue
                            hypotheses.append(hyp)
                            if len(hypotheses) == limit:
                                break
                    return hypotheses
                except HTTPException:
                    raise
                except Exception as e:
                    if strict:
                        raise
                    logger.error(f"Failed to list hypotheses for {module}: {e}")
                    return []

    async def list_for_project(
        self,
        project_id: str,
        status: Optional[HypothesisStatus] = None,
        limit: int = 50,
    ) -> list[Hypothesis]:
        """Hypotheses from every plan of a project, newest first."""
        if not project_id or limit <= 0:
            return []
        await self._ensure_table()
        query = 'SELECT * FROM hypotheses WHERE project_id = ?'
        params: list = [project_id]
        if status:
            query += ' AND status = ?'
            params.append(status.value)
        query += ' ORDER BY updated_at DESC, hypothesis_id DESC LIMIT ?'
        params.append(limit)
        async with self._get_lock():
            db = await self._get_db()
            found = []
            async with db.execute(query, tuple(params)) as cursor:
                async for row in cursor:
                    try:
                        found.append(self._row_to_hypothesis(row))
                    except (ValueError, TypeError) as error:
                        logger.warning('Skipping malformed hypothesis row: %s', type(error).__name__)
            return found

    async def assign_project(self, source_execution_id: str, project_id: str) -> int:
        """Give hypotheses recorded before projects were tracked the project of the run they came from."""
        await self._ensure_table()
        async with self._get_lock():
            db = await self._get_db()
            cursor = await db.execute(
                "UPDATE hypotheses SET project_id = ? WHERE source_execution_id = ? AND project_id = ''",
                (project_id, source_execution_id))
            await db.commit()
            return cursor.rowcount

    async def evaluate_against_facts(
        self,
        hypothesis_id: str,
        new_execution_id: str,
        *, strict: bool = False,
    ) -> Optional[Hypothesis]:
        """Check a hypothesis's prediction against a new run, when that run is the one it was about.

        Only the prediction is checked, whatever the category: the notes say so, and the prompt
        presents a prediction that held as such, not as a proven cause.
        """
        read_options = {'strict': True} if strict else {}
        hyp = await self.get(hypothesis_id, **read_options)
        if not hyp or hyp.status != HypothesisStatus.UNTESTED or hyp.source_execution_id == new_execution_id:
            return hyp
        expected = hyp.prediction or predictions.from_text(hyp.testable_prediction)
        if not predictions.checkable(expected):
            return hyp  # Not checkable by a program; the user can decide it on the report page.
        new_fact = await fact_ledger.load(new_execution_id, **read_options)
        source = await fact_ledger.load(hyp.source_execution_id, **read_options)
        if not new_fact or not source:
            return hyp
        outcome = predictions.check(expected, source, new_fact)
        if outcome is None:
            return hyp
        held, notes = outcome
        try:
            saved = await self.update_status(
                hypothesis_id,
                HypothesisStatus.CONFIRMED if held else HypothesisStatus.REFUTED,
                verified_by=new_execution_id if held else None,
                refuted_by=None if held else new_execution_id,
                notes=notes,
                expected_status=HypothesisStatus.UNTESTED,
                expected_updated_at=hyp.updated_at,
            )
        except HypothesisUpdateConflict:
            logger.info('Skipped stale evaluation for hypothesis %s', hypothesis_id)
            return await self.get(hypothesis_id, **read_options)
        if not saved:
            raise RuntimeError(f'Failed to persist hypothesis evaluation for {hypothesis_id}')
        return await self.get(hypothesis_id, **read_options)

    async def evaluate_project_hypotheses(self, project_id: str, new_execution_id: str) -> list[Hypothesis]:
        """Check every open hypothesis of the project against a new run; any plan's run may answer one."""
        results = []
        for hyp in await self.list_for_project(project_id, status=HypothesisStatus.UNTESTED, limit=200):
            updated = await self.evaluate_against_facts(hyp.hypothesis_id, new_execution_id)
            if updated:
                results.append(updated)
        return results

    async def expire(self, project_id: str, max_age_days: int) -> int:
        """Open hypotheses no run has answered in ``max_age_days`` stop reaching the prompt."""
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        await self._ensure_table()
        async with self._get_lock():
            db = await self._get_db()
            cursor = await db.execute(
                "UPDATE hypotheses SET status = ?, updated_at = ?, verification_notes = ? "
                "WHERE project_id = ? AND status = ? AND created_at < ?",
                (HypothesisStatus.DEPRECATED.value, datetime.now().isoformat(),
                 f'超过 {max_age_days} 天没有可比的运行，已过期', project_id,
                 HypothesisStatus.UNTESTED.value, cutoff))
            await db.commit()
            return cursor.rowcount

    @staticmethod
    def format_for_prompt(
        hypotheses: list[Hypothesis],
        confirmed_limit: int = 5,
        untested_limit: int = 5,
    ) -> str:
        """Format hypotheses into a prompt context, separating confirmed vs untested."""
        confirmed = [h for h in hypotheses if h.status == HypothesisStatus.CONFIRMED][:confirmed_limit]
        untested = [h for h in hypotheses if h.status == HypothesisStatus.UNTESTED][:untested_limit]
        refuted = [h for h in hypotheses if h.status == HypothesisStatus.REFUTED][:3]

        parts = []

        if confirmed:
            parts.append("### 已记录的确认结果（以验证范围为准，不代表因果已证实）")
            for h in confirmed:
                parts.append(f"- [{h.category.value}] {h.statement} (置信度: {h.confidence:.0%}, 来源: {h.source_execution_id})")
                parts.append(f"  · 验证执行: {h.verified_by_execution_id or '未记录'}；预测: {h.testable_prediction or '未记录'}")
                parts.append(f"  · 验证范围: {h.verification_notes or '未记录，不可视为已完成独立实验'}")
            parts.append("")

        if untested:
            parts.append("### 未验证假设（仅供参考，不可直接当作事实引用）")
            for h in untested:
                # The number lets a report say which open hypothesis it restates, so the old one is retired.
                parts.append(f"- [{h.category.value}] 编号 {h.hypothesis_id[:REF_LENGTH]}：{h.statement} "
                             f"(来源: {h.source_execution_id})")
                expected = predictions.describe(h.prediction) or h.testable_prediction
                if expected:
                    parts.append(f"  · 可验证预测: {expected}")
            parts.append("")

        if refuted:
            parts.append("### 已记录的证伪结果（以验证范围为准，注意避免重复）")
            for h in refuted:
                parts.append(f"- [{h.category.value}] {h.statement} (证伪于: {h.refuted_by_execution_id})")
                parts.append(f"  · 预测: {h.testable_prediction or '未记录'}；验证范围: {h.verification_notes or '未记录'}")
            parts.append("")

        return "\n".join(parts) if parts else "（无相关假设）"


# Module singleton
hypothesis_ledger = HypothesisLedger()
