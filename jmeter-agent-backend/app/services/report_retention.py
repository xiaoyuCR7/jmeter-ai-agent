"""Frees the disk space old runs hold, without losing what a run found.

Every run writes JMeter's HTML dashboard, about 3 MB of which is the same template each time, and its raw
JTL, which grows with the run. Nothing removed them until the project was deleted. Pruning removes those
two from runs older than a number of days. The run itself stays: its summary, AI report, plan snapshot,
logs and place in the history are in the database or small files. The run an active baseline was taken
from is never touched, nor a run still in flight.
"""
import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta
from typing import Optional

from app.core.config import safe_path, settings
from app.services.storage import store

logger = logging.getLogger(__name__)

CHECK_SECONDS = 24 * 3600
TERMINAL = ('completed', 'failed', 'cancelled')


def _size(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


def usage() -> dict:
    """How much the reports folder holds, and how much of it pruning could free."""
    root = settings.REPORTS_DIR
    total = dashboards = results = runs = 0
    try:
        names = os.listdir(root)
    except OSError:
        names = []
    for name in names:
        run_dir = os.path.join(root, name)
        if not os.path.isdir(run_dir):
            continue
        runs += 1
        total += _size(run_dir)
        dashboards += _size(os.path.join(run_dir, 'html'))
        results += _size(os.path.join(run_dir, 'result.jtl'))
    return {'reports_dir': root, 'runs': runs, 'total_bytes': total, 'dashboard_bytes': dashboards,
            'result_bytes': results, 'retention_days': settings.REPORTS_RETENTION_DAYS,
            'retention_includes_results': settings.REPORTS_RETENTION_INCLUDE_JTL}


async def _protected() -> set:
    """Runs an active baseline was taken from: comparisons and re-analysis still read them."""
    from app.services.db_store import db_store
    try:
        baselines = await db_store.list_baselines()
    except Exception as error:
        logger.warning('Baselines unreadable; nothing is pruned: %s', error)
        raise
    return {item['execution_id'] for item in baselines if item.get('is_active') and item.get('execution_id')}


def _prune_run(execution, include_jtl: bool) -> int:
    try:
        run_dir = safe_path(execution.report_dir or '', [settings.REPORTS_DIR])
    except ValueError:
        return 0  # A legacy run outside the reports folder is left alone.
    if not os.path.isdir(run_dir) or os.path.basename(run_dir) != execution.id:
        return 0
    freed = 0
    html = os.path.join(run_dir, 'html')
    if os.path.isdir(html):
        size = _size(html)
        shutil.rmtree(html, ignore_errors=True)
        freed += size - _size(html)
    if include_jtl:
        jtl = os.path.join(run_dir, 'result.jtl')
        if os.path.isfile(jtl):
            size = _size(jtl)
            try:
                os.remove(jtl)
                freed += size
            except OSError as error:
                logger.warning('Could not remove %s: %s', jtl, error)
    return freed


async def prune(days: int, include_jtl: bool = False, now: Optional[datetime] = None) -> dict:
    """Remove the dashboard (and with ``include_jtl`` the raw results) of runs that ended over ``days`` ago."""
    if days < 1:
        raise ValueError('days must be at least 1')
    cutoff = (now or datetime.now()) - timedelta(days=days)
    protected = await _protected()
    pruned, freed = 0, 0
    for execution in list(store.executions.values()):
        status = getattr(execution.status, 'value', execution.status)
        ended = execution.end_time or execution.start_time
        if status not in TERMINAL or execution.id in protected or ended is None:
            continue
        if ended.replace(tzinfo=None) > cutoff:
            continue
        released = await asyncio.to_thread(_prune_run, execution, include_jtl)
        if released:
            pruned, freed = pruned + 1, freed + released
    if pruned:
        logger.info('Pruned %d run(s) older than %d days, freed %.1f MB', pruned, days, freed / 1024 ** 2)
    return {'pruned_runs': pruned, 'freed_bytes': freed, 'days': days, 'include_results': include_jtl,
            'kept_for_baselines': len(protected)}


async def keep_pruned() -> None:
    """Background task: prune at startup and once a day while REPORTS_RETENTION_DAYS is set."""
    while True:
        days = settings.REPORTS_RETENTION_DAYS
        if days > 0:
            try:
                await prune(days, settings.REPORTS_RETENTION_INCLUDE_JTL)
            except Exception as error:
                logger.warning('Report pruning skipped: %s', error)
        await asyncio.sleep(CHECK_SECONDS)
