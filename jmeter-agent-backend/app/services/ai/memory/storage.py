"""Where memory lives: the app's own SQLite database, so one file holds the projects and what they taught.

Facts, hypotheses and lessons each keep their own table and connection in that file. Earlier
versions kept them in facts.db, hypotheses.db and a LanceDB folder; the first use of a table copies
those rows in and renames the old file, so nothing is lost and nothing is read twice.
"""
import asyncio
import logging
import os
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

MIGRATED_SUFFIX = '.migrated'


def memory_db_path() -> str:
    """The app's database file when it is SQLite (resolved as SQLAlchemy does), else memory.db beside it."""
    url = settings.DATABASE_URL
    if url.startswith('sqlite') and ':///' in url:
        path = url.split(':///', 1)[1].split('?', 1)[0]
        if path and path != ':memory:':
            return os.path.abspath(path)
    return os.path.join(settings.BASE_DIR, 'memory.db')


def retire_legacy(path: str) -> None:
    """Rename an old store once its rows are copied; a file still in use stays and is copied again later."""
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(path + suffix):
            try:
                os.replace(path + suffix, path + suffix + MIGRATED_SUFFIX)
            except OSError as error:
                logger.warning('Could not rename %s after copying it: %s', path + suffix, error)


class MemoryDatabase:
    """One aiosqlite connection to a memory table, opened on first use."""

    TABLE = ''
    LEGACY_FILE = ''  # The file this table lived in before, under BASE_DIR.

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or memory_db_path()
        self._db_conn = None
        # Created on first use: an asyncio.Lock made at import time (no running loop)
        # on Windows can hang aiosqlite.
        self._db_lock: Optional[asyncio.Lock] = None
        # Only the shared database adopts old files; a store opened on a path of its own (tests) does not.
        self._adopted = db_path is not None

    def _get_lock(self) -> asyncio.Lock:
        if self._db_lock is None:
            self._db_lock = asyncio.Lock()
        return self._db_lock

    async def _open(self):
        import aiosqlite
        db = await aiosqlite.connect(self.db_path, timeout=30)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
        except (Exception, asyncio.CancelledError):
            await self._close_quietly(db)
            raise
        return db

    async def _close_quietly(self, db):
        try:
            await db.close()
        except Exception:
            logger.warning('Failed to close an uninitialized %s connection', self.TABLE)

    async def _get_db(self):
        if self._db_conn is not None:
            return self._db_conn
        # Each connection runs on its own (non-daemon) thread. aiosqlite stops that thread when opening
        # fails, but not when the caller is cancelled while it opens: the thread then outlived its
        # connection and kept the process from exiting. So opening always finishes, and a caller that
        # was cancelled meanwhile closes what was opened.
        opening = asyncio.ensure_future(self._open())
        try:
            db = await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                await self._close_quietly(await opening)
            except (Exception, asyncio.CancelledError):
                pass  # Opening failed or was cancelled itself, and closed what it had opened.
            raise
        self._db_conn = db
        return db

    async def close(self):
        """Close the connection (safe to call more than once)."""
        if self._db_conn is not None:
            try:
                await self._db_conn.close()
            except Exception:
                logger.debug("Closing the memory database failed", exc_info=True)
            self._db_conn = None

    async def _adopt_legacy(self, db) -> None:
        """Copy the rows of this table's old file in, once; call with the table created, before commit."""
        if self._adopted:
            return
        self._adopted = True
        legacy = os.path.join(settings.BASE_DIR, self.LEGACY_FILE)
        if not os.path.exists(legacy) or os.path.abspath(legacy) == os.path.abspath(self.db_path):
            return
        await db.commit()  # ATTACH cannot run inside a transaction.
        await db.execute('ATTACH DATABASE ? AS legacy', (legacy,))
        try:
            found = [row[0] async for row in await db.execute(
                "SELECT name FROM legacy.sqlite_master WHERE type = 'table' AND name = ?", (self.TABLE,))]
            if found:
                ours = [row[1] async for row in await db.execute(f'PRAGMA main.table_info({self.TABLE})')]
                theirs = {row[1] async for row in await db.execute(f'PRAGMA legacy.table_info({self.TABLE})')}
                columns = ', '.join(name for name in ours if name in theirs)
                cursor = await db.execute(f'INSERT OR IGNORE INTO main.{self.TABLE} ({columns}) '
                                          f'SELECT {columns} FROM legacy.{self.TABLE}')
                await db.commit()
                logger.info('Copied %s %s rows from %s', cursor.rowcount, self.TABLE, legacy)
        finally:
            await db.execute('DETACH DATABASE legacy')
        retire_legacy(legacy)
