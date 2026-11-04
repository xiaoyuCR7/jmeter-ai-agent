"""Tools for questions about one finished run, bound to that run.

The model never passes a path or an execution id it read somewhere: each tool already knows the
run, and compare_with_run accepts only runs of the same project.
"""

import asyncio
import csv
import os
from typing import Optional

from app.core.config import settings, safe_path
from app.services.ai.agent.tool_registry import Tool

MAX_SAMPLES = 30


def _jtl(execution) -> str:
    return safe_path(os.path.join(execution.report_dir or '', 'result.jtl'), [settings.REPORTS_DIR])


def _rows(jtl_path: str):
    """(row, timeStamp, elapsed) for each readable sample."""
    with open(jtl_path, encoding='utf-8', errors='replace', newline='') as file:
        for row in csv.DictReader(file):
            try:
                yield row, int(row.get('timeStamp') or 0), int(row.get('elapsed') or 0)
            except ValueError:
                continue


def _samples(jtl_path: str, label, failed_only, min_elapsed_ms, from_second, to_second, limit) -> dict:
    # Threads write rows as their samples end, so the file is not in start order: count from the
    # earliest sample, not the first row, or "second 0" moves and early samples get negative seconds.
    first = min((stamp for _, stamp, _ in _rows(jtl_path)), default=0)
    matched, total = [], 0
    for row, stamp, elapsed in _rows(jtl_path):
        second = (stamp - first) / 1000
        if ((label and row.get('label') != label) or (failed_only and row.get('success') == 'true')
                or (min_elapsed_ms is not None and elapsed < min_elapsed_ms)
                or (from_second is not None and second < from_second)
                or (to_second is not None and second > to_second)):
            continue
        total += 1
        if len(matched) < limit:
            matched.append({
                'second': round(second, 1), 'label': row.get('label'), 'elapsed_ms': elapsed,
                'connect_ms': row.get('Connect'), 'latency_ms': row.get('Latency'),
                'code': row.get('responseCode'), 'success': row.get('success') == 'true',
                'message': (row.get('failureMessage') or row.get('responseMessage') or '')[:200],
                'threads': row.get('allThreads'),
            })
    return {'matched': total, 'shown': len(matched), 'samples': matched,
            'note': 'seconds count from the earliest sample; the first rows that match are shown'}


def run_tools(execution) -> list[Tool]:
    """Read-only tools for questions about ``execution``."""
    from app.services.storage import store

    async def find_samples(label: Optional[str] = None, failed_only: bool = False,
                           min_elapsed_ms: Optional[int] = None, from_second: Optional[float] = None,
                           to_second: Optional[float] = None, limit: int = 20) -> dict:
        limit = max(1, min(int(limit), MAX_SAMPLES))
        return await asyncio.to_thread(_samples, _jtl(execution), label, failed_only, min_elapsed_ms,
                                       from_second, to_second, limit)

    async def read_run_log(lines: int = 100) -> dict:
        from app.services.ai.agent.tools.jmeter import read_test_log
        if not execution.log_path:
            return {'error': 'This run has no JMeter log'}
        return await read_test_log(execution.log_path, max(1, min(int(lines), 500)))

    async def describe_plan() -> dict:
        from app.services.ai.agent.tools.jmx import parse_jmx_tree
        return await parse_jmx_tree(execution.jmx_path)

    async def detect_anomalies() -> dict:
        from app.services.ai.agent.tools.resource import detect_anomalies as detect
        return await detect(execution.id)

    async def list_runs(limit: int = 10) -> dict:
        runs = sorted((e for e in store.executions.values()
                       if e.test_plan_id == execution.test_plan_id and e.id != execution.id
                       and e.status.value == 'completed' and e.summary is not None),
                      key=lambda e: e.start_time or 0, reverse=True)[:max(1, min(int(limit), 20))]
        return {'runs': [{
            'execution_id': e.id, 'start_time': e.start_time.isoformat() if e.start_time else None,
            'environment': (e.context_snapshot or {}).get('environment_id'),
            'total_requests': e.summary.total_requests, 'error_rate': e.summary.error_rate,
            'avg_ms': e.summary.avg_response_time, 'p95_ms': e.summary.percentiles.get('p95'),
            'throughput': e.summary.throughput} for e in runs]}

    async def compare_with_run(other_execution_id: str) -> dict:
        from app.services.ai.agent.tools.resource import compare_runs
        other = store.get_execution(other_execution_id)
        if other is None or other.project_id != execution.project_id or other.status.value != 'completed':
            return {'error': 'Compare only with a finished run of the same project; list_runs shows them'}
        return await compare_runs(_jtl(other), _jtl(execution), baseline_name=other.id[:8], target_name='this run')

    def tool(name, description, handler, properties=None):
        return Tool(name, description, {'type': 'object', 'properties': properties or {}}, handler)

    return [
        tool('find_samples', 'Individual samples of this run, filtered: by request label, failures only, '
             'slower than a threshold, or a time window in seconds from the start. Shows status code, failure '
             'message, connect and latency.', find_samples, {
                 'label': {'type': 'string'}, 'failed_only': {'type': 'boolean'},
                 'min_elapsed_ms': {'type': 'integer'}, 'from_second': {'type': 'number'},
                 'to_second': {'type': 'number'}, 'limit': {'type': 'integer', 'description': f'At most {MAX_SAMPLES}'}}),
        tool('read_run_log', "The end of this run's JMeter log", read_run_log,
             {'lines': {'type': 'integer', 'description': 'At most 500'}}),
        tool('describe_plan', 'The test plan this run executed: thread groups, requests, timers, assertions', describe_plan),
        tool('detect_anomalies', "Statistical spikes and drops in this run's timeline", detect_anomalies),
        tool('list_runs', 'Earlier finished runs of the same plan, newest first, with headline numbers', list_runs,
             {'limit': {'type': 'integer', 'description': 'At most 20'}}),
        tool('compare_with_run', 'Compare this run with an earlier finished run of the same project, per request',
             compare_with_run, {'other_execution_id': {'type': 'string'}}),
    ]
