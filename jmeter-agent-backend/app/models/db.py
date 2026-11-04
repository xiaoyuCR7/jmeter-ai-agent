"""SQLAlchemy ORM models for persistent storage."""

from uuid import uuid4
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text,
    ForeignKey, JSON, create_engine, event, inspect,
)
from sqlalchemy.orm import DeclarativeBase, relationship, Session
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

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
    environments = relationship("EnvironmentModel", back_populates="project", cascade="all, delete-orphan")
    baselines = relationship("BaselineModel", back_populates="project", cascade="all, delete-orphan")


class EnvironmentModel(Base):
    __tablename__ = "environments"

    id = Column(String(50), primary_key=True)
    project_id = Column(String(50), ForeignKey("projects.id"), nullable=False)
    name = Column(String(100), nullable=False)
    base_url = Column(String(500), default="")
    variables = Column(JSON, default=dict)
    headers = Column(JSON, default=dict)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utc_now_naive)

    project = relationship("ProjectModel", back_populates="environments")


# ── Test Plan ────────────────────────────────────────────

class TestPlanModel(Base):
    __tablename__ = "test_plans"

    id = Column(String(50), primary_key=True)
    project_id = Column(String(50), ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    test_type = Column(String(30), default="baseline")
    jmx_path = Column(String(500), default="")
    jmx_content = Column(Text, nullable=True)  # raw JMX XML
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
    test_plan_id = Column(String(50), ForeignKey("test_plans.id"), nullable=True)
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
    created_at = Column(DateTime, default=utc_now_naive)
    version = Column(Integer, nullable=False, default=1, server_default="1")

    test_plan = relationship("TestPlanModel", back_populates="executions")


# ── Baseline ─────────────────────────────────────────────

class BaselineModel(Base):
    __tablename__ = "performance_baselines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String(50), ForeignKey("projects.id"), nullable=False, index=True)
    test_plan_id = Column(String(50), ForeignKey("test_plans.id"), nullable=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    execution_id = Column(String(50), nullable=True)  # which run this baseline is based on
    thresholds = Column(JSON, default=dict)  # {"p99_max": 500, "error_rate_max": 0.01}
    metrics_snapshot = Column(JSON, default=dict)  # snapshot of key metrics
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utc_now_naive)

    project = relationship("ProjectModel", back_populates="baselines")


# ── Schedule ─────────────────────────────────────────────

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


async def init_db():
    """Create all tables on startup."""
    engine = get_engine()
    async with engine.begin() as conn:
        # Multi-user, scheduling and cross-process tables from earlier versions.
        # Child tables first so foreign keys never block the drop.
        for table in OBSOLETE_TABLES:
            await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        await conn.run_sync(Base.metadata.create_all)
        # Inspect before adding compatible nullable/defaulted columns. Real
        # migration failures must surface instead of masquerading as duplicates.
        def migrate(connection):
            additions = {
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
            for table, columns in additions.items():
                existing = {column["name"] for column in inspect(connection).get_columns(table)}
                for name, declaration in columns.items():
                    if name not in existing:
                        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        await conn.run_sync(migrate)


async def get_db() -> AsyncSession:
    """Dependency: yield an async DB session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()
