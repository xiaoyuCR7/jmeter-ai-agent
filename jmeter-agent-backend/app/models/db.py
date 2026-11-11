"""SQLAlchemy ORM models for persistent storage."""

import logging

from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, Text,
    ForeignKey, JSON, create_engine, event, inspect,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.core.time import utc_now_naive


class Base(DeclarativeBase):
    pass


# ── Project ──────────────────────────────────────────────

class ProjectModel(Base):
    __tablename__ = "projects"

    id = Column(String(50), primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    base_url = Column(String(500), default="")
    environment_data = Column(JSON, default=dict)  # serialized ProjectEnvironment
    environments_data = Column(JSON, nullable=True)  # serialized ProjectEnvironments
    created_at = Column(DateTime, default=utc_now_naive)
    updated_at = Column(DateTime, default=utc_now_naive)
    version = Column(Integer, nullable=False, default=1, server_default="1")

    test_plans = relationship("TestPlanModel", back_populates="project", cascade="all, delete-orphan")
    baselines = relationship("BaselineModel", back_populates="project", cascade="all, delete-orphan")


# ── Test Plan ────────────────────────────────────────────

class TestPlanModel(Base):
    __tablename__ = "test_plans"

    id = Column(String(50), primary_key=True)
    project_id = Column(String(50), ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    test_type = Column(String(30), default="baseline")
    jmx_path = Column(String(500), default="", index=True)  # Looked up by path whenever a run names a file.
    test_config = Column(JSON, default=dict)
    endpoints_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utc_now_naive)
    updated_at = Column(DateTime, default=utc_now_naive)
    version = Column(Integer, nullable=False, default=1, server_default="1")

    project = relationship("ProjectModel", back_populates="test_plans")
    executions = relationship("ExecutionModel", back_populates="test_plan", cascade="all, delete-orphan")


# ── Execution ────────────────────────────────────────────

class ExecutionModel(Base):
    __tablename__ = "executions"

    id = Column(String(50), primary_key=True)
    project_id = Column(String(50), ForeignKey("projects.id"), nullable=False, index=True)
    test_plan_id = Column(String(50), ForeignKey("test_plans.id"), nullable=True, index=True)
    status = Column(String(20), default="pending")
    start_time = Column(DateTime, nullable=True)
    end_time = Column(DateTime, nullable=True)
    jmx_path = Column(String(500), default="")
    report_dir = Column(String(500), default="")
    log_path = Column(String(500), default="")
    error_message = Column(Text, nullable=True)
    resource_snapshot_pre = Column(JSON, nullable=True)
    resource_snapshot_post = Column(JSON, nullable=True)
    ai_analysis_data = Column(JSON, nullable=True)  # serialized ReportAnalysis
    summary_data = Column(JSON, nullable=True)       # serialized DetailedTestSummary
    source_jmx_path = Column(String(500), nullable=True)
    source_sha256 = Column(String(64), nullable=True)
    context_snapshot = Column(JSON, nullable=True)
    report_status = Column(String(20), default="pending")
    report_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now_naive, index=True)  # Every listing is newest first.
    version = Column(Integer, nullable=False, default=1, server_default="1")

    test_plan = relationship("TestPlanModel", back_populates="executions")


# ── Baseline ─────────────────────────────────────────────

class BaselineModel(Base):
    __tablename__ = "performance_baselines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String(50), ForeignKey("projects.id"), nullable=False, index=True)
    test_plan_id = Column(String(50), ForeignKey("test_plans.id"), nullable=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    execution_id = Column(String(50), nullable=True)  # which run this baseline is based on
    thresholds = Column(JSON, default=dict)  # {"p99_max": 500, "error_rate_max": 0.01}
    metrics_snapshot = Column(JSON, default=dict)  # snapshot of key metrics
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utc_now_naive)

    project = relationship("ProjectModel", back_populates="baselines")


# ── Requirements and monitoring ──────────────────────────

class PerformanceRequirementModel(Base):
    """One structured requirement set per project (see services/requirements.py)."""
    __tablename__ = "performance_requirements"

    project_id = Column(String(50), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    data = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)


class MonitoringConfigModel(Base):
    """Per-project Prometheus endpoint and queries (see services/server_metrics.py)."""
    __tablename__ = "monitoring_configs"

    project_id = Column(String(50), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    data = Column(JSON, nullable=False)


OBSOLETE_TABLES = (
    "schedule_dispatches", "schedule_principals", "schedules", "tool_confirmations",
    "analysis_receipt_owners", "analysis_receipts", "resource_leases", "mcp_session_messages",
    "mcp_session_routes", "ws_event_deliveries", "ws_worker_subscriptions", "coordination_operations",
    "process_owners", "coordination_protocol", "project_access_audits", "project_members",
    "project_access_policies", "user_access_revisions", "api_tokens", "users", "business_data_revision",
)

_engine = None
_async_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        db_url = settings.DATABASE_URL
        _engine = create_async_engine(
            db_url,
            echo=False,
            connect_args={"check_same_thread": False} if "sqlite" in db_url else {},
        )
        # Enable WAL mode on SQLite
        if "sqlite" in db_url:
            @event.listens_for(_engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
    return _engine


def get_session_factory() -> async_sessionmaker:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _async_session_factory


_sync_engines: dict = {}


def get_sync_engine():
    """A blocking engine on the same SQLite file, for a single-row read from code that cannot await.

    None for any other database. Each read opens and closes its own connection (NullPool), so no file
    handle outlives it.
    """
    from sqlalchemy.pool import NullPool
    url = get_engine().sync_engine.url
    if not url.drivername.startswith('sqlite'):
        return None
    key = str(url)
    if key not in _sync_engines:
        _sync_engines[key] = create_engine(url.set(drivername='sqlite'), poolclass=NullPool)
    return _sync_engines[key]


# ── Schema upkeep ────────────────────────────────────────
#
# Two kinds of change, kept apart:
#
# - ``_ensure_current`` makes a database of any age usable: it adds the columns and indexes newer code
#   expects. It only ever adds, checks before each addition and runs at every start, so a database
#   restored from an old backup is repaired the same way a fresh one is created.
# - ``MIGRATIONS`` are the changes that must happen exactly once because they remove something. Each has
#   a number; the database records the highest one applied (``schema_migrations``), and a start applies
#   only the ones after it. To change the schema destructively, append a step; never edit an applied one.

ADDED_COLUMNS = {
    "executions": {
        "summary_data": "JSON", "source_jmx_path": "VARCHAR(500)",
        "source_sha256": "VARCHAR(64)",
        "context_snapshot": "JSON",
        "report_status": "VARCHAR(20) DEFAULT 'pending'",
        "report_error": "TEXT",
        "version": "INTEGER NOT NULL DEFAULT 1",
    },
    "projects": {"environments_data": "JSON", "version": "INTEGER NOT NULL DEFAULT 1"},
    "test_plans": {"version": "INTEGER NOT NULL DEFAULT 1"},
}


def _ensure_current(connection):
    for table, columns in ADDED_COLUMNS.items():
        existing = {column["name"] for column in inspect(connection).get_columns(table)}
        for name, declaration in columns.items():
            if name not in existing:
                # Real failures must surface instead of masquerading as duplicates, hence the check first.
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    # create_all adds indexes only with their table; a database from before them gets them here.
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(connection, checkfirst=True)


def _drop_multi_user_tables(connection):
    # Child tables first so foreign keys never block the drop.
    for table in OBSOLETE_TABLES:
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")


def _drop_unused_plan_storage(connection):
    """The per-row copy of environments and of each plan's XML: nothing has written either for many versions.

    Kept when they hold data, so a database that somehow has some loses nothing.
    """
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())  # A new database has neither table yet.
    if "environments" in tables:
        if connection.exec_driver_sql("SELECT COUNT(*) FROM environments").scalar() == 0:
            connection.exec_driver_sql("DROP TABLE environments")
    if "test_plans" in tables and "jmx_content" in {
            column["name"] for column in inspector.get_columns("test_plans")}:
        if connection.exec_driver_sql(
                "SELECT COUNT(*) FROM test_plans WHERE jmx_content IS NOT NULL").scalar() == 0:
            try:
                connection.exec_driver_sql("ALTER TABLE test_plans DROP COLUMN jmx_content")
            except Exception as error:  # SQLite before 3.35 cannot drop a column; an unused one is harmless.
                logger.info("Unused column test_plans.jmx_content kept: %s", error)


MIGRATIONS = (
    (1, "drop the tables of the multi-user, scheduling and cross-process versions", _drop_multi_user_tables),
    (2, "drop the unused environments table and test_plans.jmx_content", _drop_unused_plan_storage),
)


def _applied_version(connection) -> int:
    connection.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT)")
    return connection.exec_driver_sql("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").scalar() or 0


def _migrate(connection):
    applied = _applied_version(connection)
    for version, description, step in MIGRATIONS:
        if version <= applied:
            continue
        step(connection)
        connection.exec_driver_sql(
            "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)"
            if connection.dialect.paramstyle == 'qmark' else
            "INSERT INTO schema_migrations (version, description, applied_at) VALUES (%s, %s, %s)",
            (version, description, utc_now_naive().isoformat()))
        logger.info("Database migration %d applied: %s", version, description)


async def init_db():
    """Create missing tables, bring an older database up to date, and apply the one-time migrations."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(_migrate_before_create)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_current)


def _migrate_before_create(connection):
    # Before create_all: a leftover table of an old version must be gone before its name is reused.
    _migrate(connection)


async def get_db() -> AsyncSession:
    """Dependency: yield an async DB session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()
