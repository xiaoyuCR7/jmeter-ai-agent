"""Database-backed async store — replaces in-memory SharedStore."""

import logging
from typing import Optional, List, Dict, Any

from app.core.errors import ServiceError
from sqlalchemy import select, delete, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import (
    get_session_factory, ProjectModel, TestPlanModel, ExecutionModel,
    EnvironmentModel, BaselineModel,
)
from app.core.time import utc_now_naive

logger = logging.getLogger(__name__)


def _project_conditions(column, project_id=None):
    return [column == project_id] if project_id is not None else []


def _immutable_identity(row, updates):
    for key in ('id', 'project_id'):
        if key in updates and updates[key] != getattr(row, key, None):
            raise ServiceError(422, 'Resource identity and project cannot be changed')


async def _source(session, model, resource_id, project_id):
    if resource_id is None:
        return None
    record = await session.get(model, resource_id)
    if record is None:
        raise ServiceError(404, 'Source resource not found')
    if record.project_id != project_id:
        raise ServiceError(422, 'Source resource project mismatch')
    return record


async def _check_plan_children(session, plan_ids, project_id):
    """Old inconsistent foreign keys must not cascade into another project."""
    if not plan_ids:
        return
    for model in (ExecutionModel, BaselineModel):
        foreign = await session.execute(select(model.id).where(
            model.test_plan_id.in_(plan_ids), model.project_id != project_id,
        ).limit(1))
        if foreign.first() is not None:
            raise ServiceError(409, 'Inconsistent resource ownership requires repair')


async def _versioned_update(session, model, row, updates, expected_version, *, touch_updated=False):
    """Atomically reject an object loaded before a newer committed write."""
    expected = updates.get('storage_version', row.version) if expected_version is None else expected_version
    if type(expected) is not int or expected < 1:
        raise ServiceError(409, 'Business record version is stale')
    values = {key: val for key, val in updates.items()
              if key != 'storage_version' and hasattr(row, key) and key != 'version'}
    if touch_updated:
        values['updated_at'] = utc_now_naive()
    values['version'] = model.version + 1
    changed = await session.execute(update(model).where(
        model.id == row.id, model.version == expected).values(**values).execution_options(
            synchronize_session=False))
    if changed.rowcount != 1:
        raise ServiceError(409, 'Business record changed; refresh and retry')
    session.expire(row)
    await session.refresh(row)


class DBStore:
    """Async database store for projects, test plans, executions, baselines."""

    async def _session(self) -> AsyncSession:
        factory = get_session_factory()
        return factory()

    # ── Projects ──────────────────────────────────────────

    async def create_project(self, project_data: dict) -> dict:
        async with await self._session() as s:
            p = ProjectModel(
                id=project_data["id"],
                name=project_data["name"],
                description=project_data.get("description", ""),
                base_url=project_data.get("base_url", ""),
                environment_data=project_data.get("environment_data", {}),
                environments_data=project_data.get("environments_data"),
                created_at=project_data.get("created_at", utc_now_naive()),
            )
            s.add(p)
            await s.commit()
            return self._project_to_dict(p)

    async def update_project(self, project_id: str, updates: dict, *, expected_version=None) -> Optional[dict]:
        async with await self._session() as s:
            p = await s.get(ProjectModel, project_id)
            if not p:
                return None
            _immutable_identity(p, updates)
            await _versioned_update(s, ProjectModel, p, updates, expected_version, touch_updated=True)
            await s.commit()
            return self._project_to_dict(p)

    async def delete_project(self, project_id: str) -> bool:
        async with await self._session() as s:
            p = await s.get(ProjectModel, project_id)
            if not p:
                return False
            plan_ids = list((await s.execute(select(TestPlanModel.id).where(TestPlanModel.project_id == p.id))).scalars())
            await _check_plan_children(s, plan_ids, p.id)
            # Standalone executions are not ORM children of the project.
            await s.execute(delete(ExecutionModel).where(ExecutionModel.project_id == p.id))
            await s.delete(p)
            await s.commit()
            return True

    async def get_project(self, project_id: str) -> Optional[dict]:
        async with await self._session() as s:
            p = await s.get(ProjectModel, project_id)
            return self._project_to_dict(p) if p else None

    async def list_projects(self, offset: int = 0, limit: int = 50) -> dict:
        conditions = _project_conditions(ProjectModel.id)
        async with await self._session() as s:
            stmt = select(ProjectModel).where(*conditions).offset(offset).limit(limit).order_by(ProjectModel.created_at.desc())
            result = await s.execute(stmt)
            projects = [self._project_to_dict(p) for p in result.scalars()]
            total = (await s.execute(select(func.count()).select_from(ProjectModel).where(*conditions))).scalar_one()
            return {"projects": projects, "total": total, "offset": offset, "limit": limit}

    def _project_to_dict(self, p: ProjectModel) -> dict:
        return {
            "id": p.id, "name": p.name, "description": p.description,
            "base_url": p.base_url, "environment_data": p.environment_data,
            "environments_data": p.environments_data,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "storage_version": p.version,
        }

    # ── Test Plans ────────────────────────────────────────

    async def create_test_plan(self, plan_data: dict) -> dict:
        project_id = plan_data.get('project_id', 'default')
        async with await self._session() as s:
            if await s.get(ProjectModel, project_id) is None:
                raise ServiceError(404, 'Project not found')
            tp = TestPlanModel(
                id=plan_data["id"],
                project_id=plan_data.get("project_id", "default"),
                name=plan_data["name"],
                description=plan_data.get("description", ""),
                test_type=plan_data.get("test_type", "baseline"),
                jmx_path=plan_data.get("jmx_path", ""),
                jmx_content=plan_data.get("jmx_content"),
                test_config=plan_data.get("test_config", {}),
                endpoints_count=plan_data.get("endpoints_count", 0),
                created_at=plan_data.get("created_at", utc_now_naive()),
            )
            s.add(tp)
            await s.commit()
            return self._plan_to_dict(tp)

    async def update_test_plan(self, plan_id: str, updates: dict, *, expected_version=None) -> Optional[dict]:
        async with await self._session() as s:
            tp = await s.get(TestPlanModel, plan_id)
            if not tp:
                return None
            _immutable_identity(tp, updates)
            await _versioned_update(s, TestPlanModel, tp, updates, expected_version, touch_updated=True)
            await s.commit()
            return self._plan_to_dict(tp)

    async def delete_test_plan(self, plan_id: str) -> bool:
        async with await self._session() as s:
            tp = await s.get(TestPlanModel, plan_id)
            if not tp:
                return False
            await _check_plan_children(s, [tp.id], tp.project_id)
            await s.delete(tp)
            await s.commit()
            return True

    async def get_test_plan(self, plan_id: str) -> Optional[dict]:
        async with await self._session() as s:
            tp = await s.get(TestPlanModel, plan_id)
            return self._plan_to_dict(tp) if tp else None

    async def list_test_plans(self, project_id: str = None, offset: int = 0, limit: int = 50) -> dict:
        conditions = _project_conditions(TestPlanModel.project_id, project_id)
        async with await self._session() as s:
            stmt = select(TestPlanModel).where(*conditions).offset(offset).limit(limit).order_by(TestPlanModel.created_at.desc())
            result = await s.execute(stmt)
            plans = [self._plan_to_dict(p) for p in result.scalars()]
            total = (await s.execute(select(func.count()).select_from(TestPlanModel).where(*conditions))).scalar_one()
            return {"test_plans": plans, "total": total, "offset": offset, "limit": limit}

    def _plan_to_dict(self, tp: TestPlanModel) -> dict:
        return {
            "id": tp.id, "project_id": tp.project_id, "name": tp.name,
            "description": tp.description, "test_type": tp.test_type,
            "jmx_path": tp.jmx_path, "endpoints_count": tp.endpoints_count,
            "test_config": tp.test_config,
            "created_at": tp.created_at.isoformat() if tp.created_at else None,
            "updated_at": tp.updated_at.isoformat() if tp.updated_at else None,
            "storage_version": tp.version,
        }

    # ── Executions ────────────────────────────────────────

    async def create_execution(self, exec_data: dict) -> dict:
        project_id = exec_data.get('project_id', 'default')
        async with await self._session() as s:
            if await s.get(ProjectModel, project_id) is None:
                raise ServiceError(404, 'Project not found')
            await _source(s, TestPlanModel, exec_data.get('test_plan_id'), project_id)
            e = ExecutionModel(
                id=exec_data["id"],
                project_id=exec_data.get("project_id", "default"),
                test_plan_id=exec_data.get("test_plan_id"),
                status=exec_data.get("status", "pending"),
                start_time=exec_data.get("start_time"),
                end_time=exec_data.get("end_time"),
                jmx_path=exec_data.get("jmx_path", ""),
                report_dir=exec_data.get("report_dir", ""),
                log_path=exec_data.get("log_path", ""),
                ai_analysis_data=exec_data.get("ai_analysis_data"),
                summary_data=exec_data.get("summary_data"),
                error_message=exec_data.get("error_message"),
                resource_snapshot_pre=exec_data.get("resource_snapshot_pre"),
                resource_snapshot_post=exec_data.get("resource_snapshot_post"),
                source_jmx_path=exec_data.get("source_jmx_path"),
                source_sha256=exec_data.get("source_sha256"),
                context_snapshot=exec_data.get("context_snapshot"),
                report_status=exec_data.get("report_status", "pending"),
                report_error=exec_data.get("report_error"),
            )
            s.add(e)
            await s.commit()
            return self._exec_to_dict(e)

    async def update_execution(self, exec_id: str, updates: dict, *, expected_version=None) -> Optional[dict]:
        async with await self._session() as s:
            e = await s.get(ExecutionModel, exec_id)
            if not e:
                return None
            _immutable_identity(e, updates)
            await _source(s, TestPlanModel, updates.get('test_plan_id', e.test_plan_id), e.project_id)
            await _versioned_update(s, ExecutionModel, e, updates, expected_version)
            await s.commit()
            return self._exec_to_dict(e)

    async def get_execution(self, exec_id: str) -> Optional[dict]:
        async with await self._session() as s:
            e = await s.get(ExecutionModel, exec_id)
            return self._exec_to_dict(e) if e else None

    async def list_executions(self, project_id: str = None, offset: int = 0, limit: int = 50) -> dict:
        conditions = _project_conditions(ExecutionModel.project_id, project_id)
        async with await self._session() as s:
            stmt = select(ExecutionModel).where(*conditions).offset(offset).limit(limit).order_by(ExecutionModel.created_at.desc())
            result = await s.execute(stmt)
            execs = [self._exec_to_dict(e) for e in result.scalars()]
            total = (await s.execute(select(func.count()).select_from(ExecutionModel).where(*conditions))).scalar_one()
            return {"executions": execs, "total": total, "offset": offset, "limit": limit}

    def _exec_to_dict(self, e: ExecutionModel) -> dict:
        return {
            "id": e.id, "project_id": e.project_id, "test_plan_id": e.test_plan_id,
            "status": e.status, "start_time": e.start_time.isoformat() if e.start_time else None,
            "end_time": e.end_time.isoformat() if e.end_time else None,
            "jmx_path": e.jmx_path, "report_dir": e.report_dir, "log_path": e.log_path,
            "error_message": e.error_message,
            "resource_snapshot_pre": e.resource_snapshot_pre,
            "resource_snapshot_post": e.resource_snapshot_post,
            "ai_analysis_data": e.ai_analysis_data,
            "summary_data": e.summary_data,
            "source_jmx_path": e.source_jmx_path, "source_sha256": e.source_sha256,
            "context_snapshot": e.context_snapshot,
            "report_status": e.report_status, "report_error": e.report_error,
            "storage_version": e.version,
        }

    # ── Baselines ─────────────────────────────────────────

    async def create_baseline(self, data: dict) -> dict:
        project_id = data['project_id']
        async with await self._session() as s:
            if await s.get(ProjectModel, project_id) is None:
                raise ServiceError(404, 'Project not found')
            plan = await _source(s, TestPlanModel, data.get('test_plan_id'), project_id)
            execution = await _source(s, ExecutionModel, data.get('execution_id'), project_id)
            if plan is not None and execution is not None and execution.test_plan_id != plan.id:
                raise ServiceError(422, 'Source execution and test plan mismatch')
            b = BaselineModel(
                project_id=data["project_id"],
                test_plan_id=data.get("test_plan_id"),
                name=data["name"],
                description=data.get("description", ""),
                execution_id=data.get("execution_id"),
                thresholds=data.get("thresholds", {}),
                metrics_snapshot=data.get("metrics_snapshot", {}),
                is_active=data.get("is_active", True),
            )
            if b.is_active:
                await self._deactivate_baselines(s, b.project_id, b.test_plan_id)
            s.add(b)
            await s.commit()
            return self._baseline_to_dict(b)

    async def _deactivate_baselines(self, s, project_id: str, test_plan_id: Optional[str],
                                    keep_id: Optional[int] = None):
        """Deactivate the active baselines of exactly one project/test-plan scope."""
        stmt = update(BaselineModel).where(
            BaselineModel.project_id == project_id,
            BaselineModel.is_active == True,
            BaselineModel.test_plan_id == test_plan_id if test_plan_id
            else BaselineModel.test_plan_id.is_(None),
        )
        if keep_id is not None:
            stmt = stmt.where(BaselineModel.id != keep_id)
        await s.execute(stmt.values(is_active=False))

    async def list_baselines(self, project_id: str = None) -> list:
        conditions = _project_conditions(BaselineModel.project_id, project_id)
        async with await self._session() as s:
            stmt = select(BaselineModel).where(*conditions).order_by(BaselineModel.created_at.desc())
            result = await s.execute(stmt)
            return [self._baseline_to_dict(b) for b in result.scalars()]

    async def get_active_baseline(self, project_id: str, test_plan_id: str = None) -> Optional[dict]:
        async with await self._session() as s:
            await _source(s, TestPlanModel, test_plan_id, project_id)
            stmt = select(BaselineModel).where(
                BaselineModel.project_id == project_id,
                BaselineModel.is_active == True,
            )
            if test_plan_id:
                stmt = stmt.where(BaselineModel.test_plan_id == test_plan_id)
            stmt = stmt.order_by(BaselineModel.created_at.desc()).limit(1)
            result = await s.execute(stmt)
            b = result.scalar_one_or_none()
            return self._baseline_to_dict(b) if b else None

    async def delete_baseline(self, baseline_id: int) -> bool:
        async with await self._session() as s:
            b = await s.get(BaselineModel, baseline_id)
            if not b:
                return False
            await s.delete(b)
            await s.commit()
            return True

    async def set_active_baseline(self, baseline_id: int) -> Optional[dict]:
        """Atomically activate one baseline and deactivate its siblings in the
        same project/test-plan scope. Returns the activated baseline or None."""
        async with await self._session() as s:
            b = await s.get(BaselineModel, baseline_id)
            if not b:
                return None
            await self._deactivate_baselines(s, b.project_id, b.test_plan_id, keep_id=baseline_id)
            b.is_active = True
            await s.commit()
            return self._baseline_to_dict(b)

    def _baseline_to_dict(self, b: BaselineModel) -> dict:
        return {
            "id": b.id, "project_id": b.project_id, "test_plan_id": b.test_plan_id,
            "name": b.name, "description": b.description,
            "execution_id": b.execution_id,
            "thresholds": b.thresholds, "metrics_snapshot": b.metrics_snapshot,
            "is_active": b.is_active,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }


# Module-level singleton
db_store = DBStore()
