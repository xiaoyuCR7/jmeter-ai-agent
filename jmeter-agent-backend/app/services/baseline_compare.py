"""Automatic comparison of a run with its active baseline and the previous run of the same plan."""
import logging
import math
from datetime import datetime
from typing import Optional

from sqlalchemy import or_, select

from app.models.schemas import DetailedTestSummary, TestExecutionStatus
from app.services.jmeter.regression_engine import RegressionEngine

logger = logging.getLogger(__name__)

REGRESSION_THRESHOLD_PCT = 20.0
# Transaction-level checks: response time grows, or errors rise by at least this many percentage points.
TRANSACTION_METRICS = (('p95_response_time', 'P95'), ('avg_response_time', '平均'), ('error_rate', '错误率'))
ERROR_RATE_POINTS = 1.0
# Below these sizes a percentile is a handful of samples, so a change in it is not evidence.
MIN_RUN_SAMPLES = 100
MIN_TRANSACTION_SAMPLES = 30
# Runs at different concurrency answer different questions.
LOAD_TOLERANCE_PCT = 10.0


def snapshot_of(summary: DetailedTestSummary) -> dict:
    """What a baseline keeps of a run: everything except the bulky timeline."""
    return summary.model_dump(exclude={'timeline_segments', 'error_details', 'response_time_distribution'})


def _summary_from_snapshot(snapshot: dict) -> Optional[DetailedTestSummary]:
    try:
        return DetailedTestSummary.model_validate(snapshot) if snapshot.get('total_requests') else None
    except Exception:
        return None


def compare_transactions(baseline: DetailedTestSummary, target: DetailedTestSummary,
                         threshold_pct: float = REGRESSION_THRESHOLD_PCT) -> list[dict]:
    rows = []
    base_labels, target_labels = baseline.per_label or {}, target.per_label or {}
    for label in sorted(set(base_labels) & set(target_labels)):
        if min(base_labels[label].get('count', MIN_TRANSACTION_SAMPLES),
               target_labels[label].get('count', MIN_TRANSACTION_SAMPLES)) < MIN_TRANSACTION_SAMPLES:
            continue
        for key, title in TRANSACTION_METRICS:
            old, new = base_labels[label].get(key), target_labels[label].get(key)
            if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
                continue
            if key == 'error_rate':
                regression = new - old >= ERROR_RATE_POINTS
                delta_pct = None if old == 0 else round((new - old) / old * 100, 1)
            else:
                delta_pct = None if old == 0 else round((new - old) / old * 100, 1)
                regression = delta_pct is not None and delta_pct > threshold_pct
            if regression or (delta_pct is not None and abs(delta_pct) > threshold_pct):
                rows.append({'label': label, 'metric': key, 'metric_title': title, 'baseline': old, 'target': new,
                             'delta_pct': delta_pct, 'is_regression': regression})
    rows.sort(key=lambda r: (not r['is_regression'], -(abs(r['delta_pct']) if r['delta_pct'] is not None else math.inf)))
    return rows[:20]


def _peak_threads(summary: DetailedTestSummary) -> Optional[int]:
    return ((summary.diagnostics or {}).get('load') or {}).get('peak_threads')


def comparability_notes(baseline: DetailedTestSummary, target: DetailedTestSummary) -> list[dict]:
    """Why a comparison of these two runs should be read with care."""
    notes = []
    old, new = _peak_threads(baseline), _peak_threads(target)
    if old and new and abs(new - old) / old * 100 > LOAD_TOLERANCE_PCT:
        notes.append({'kind': 'load', 'baseline': old, 'target': new})
    if min(baseline.total_requests, target.total_requests) < MIN_RUN_SAMPLES:
        notes.append({'kind': 'samples', 'baseline': baseline.total_requests, 'target': target.total_requests})
    return notes


def _comparison(name: str, baseline: DetailedTestSummary, target: DetailedTestSummary, **identity) -> dict:
    report = RegressionEngine(REGRESSION_THRESHOLD_PCT).compare(baseline, target, name, 'current').to_dict()
    transactions = compare_transactions(baseline, target)
    verdict = report['verdict']
    if any(t['is_regression'] for t in transactions) and verdict in ('stable', 'improvement'):
        verdict = 'mixed' if verdict == 'improvement' else 'regression'
    notes = comparability_notes(baseline, target)
    if any(note['kind'] == 'load' for note in notes):
        verdict = 'unknown'
    return {**identity, 'name': name, 'verdict': verdict, 'summary': report['summary'], 'deltas': report['deltas'],
            'transactions': transactions, 'threshold_pct': REGRESSION_THRESHOLD_PCT, 'notes': notes}


async def _active_baseline(project_id: str, test_plan_id: Optional[str]) -> Optional[dict]:
    """Prefer the plan's own baseline; fall back to a project-wide one (no plan)."""
    from app.models.db import BaselineModel
    from app.services.db_store import db_store
    async with await db_store._session() as session:
        rows = (await session.execute(select(BaselineModel).where(
            BaselineModel.project_id == project_id, BaselineModel.is_active == True,  # noqa: E712
            or_(BaselineModel.test_plan_id == test_plan_id, BaselineModel.test_plan_id.is_(None)),
        ).order_by(BaselineModel.created_at.desc()))).scalars().all()
        rows.sort(key=lambda row: row.test_plan_id != test_plan_id)
        return db_store._baseline_to_dict(rows[0]) if rows else None


def _previous_run(execution_id: str, project_id: str, test_plan_id: Optional[str], start: Optional[datetime]):
    from app.services.storage import store
    if not test_plan_id or start is None:
        return None
    candidates = [e for e in store.executions.values()
                  if e.id != execution_id and e.project_id == project_id and e.test_plan_id == test_plan_id
                  and e.status == TestExecutionStatus.COMPLETED and e.start_time and e.start_time < start
                  and e.summary is not None and e.summary.total_requests > 0]
    return max(candidates, key=lambda e: e.start_time, default=None)


async def evidence_for(execution, summary: DetailedTestSummary) -> dict:
    from app.services.storage import store
    get = execution.get if isinstance(execution, dict) else lambda key, default=None: getattr(execution, key, default)
    execution_id, project_id, plan_id = get('id'), get('project_id') or 'default', get('test_plan_id')
    start = get('start_time')
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    result = {'baseline': None, 'previous': None}

    baseline = await _active_baseline(project_id, plan_id)
    if baseline and baseline.get('execution_id') != execution_id:
        source = store.executions.get(baseline.get('execution_id') or '')
        base_summary = (source.summary if source is not None and source.summary is not None
                        else _summary_from_snapshot(baseline.get('metrics_snapshot') or {}))
        if base_summary is not None and base_summary.total_requests > 0:
            result['baseline'] = _comparison(baseline['name'], base_summary, summary,
                                             baseline_id=baseline['id'], execution_id=baseline.get('execution_id'))
        else:
            result['baseline'] = {'name': baseline['name'], 'baseline_id': baseline['id'], 'verdict': 'unknown',
                                  'summary': 'Baseline has no stored metrics', 'deltas': [], 'transactions': []}
    elif baseline:
        result['is_baseline'] = True

    previous = _previous_run(execution_id, project_id, plan_id, start)
    if previous is not None:
        result['previous'] = _comparison(previous.start_time.strftime('%Y-%m-%d %H:%M'), previous.summary, summary,
                                         execution_id=previous.id)
    return result


_METRIC_TITLES = {'avg_response_time': '平均响应时间', 'p95_response_time': 'P95', 'p99_response_time': 'P99',
                  'max_response_time': '最大响应时间', 'min_response_time': '最小响应时间',
                  'error_rate': '错误率', 'throughput': '吞吐量'}


def _format_one(title: str, comparison: dict) -> list[str]:
    lines = [f"### {title}「{comparison['name']}」：{comparison['verdict']}"]
    for note in comparison.get('notes', []):
        if note['kind'] == 'load':
            lines.append(f"- 负载不同（峰值线程 {note['baseline']}→{note['target']}），不判定退化，差异可能只是负载造成的。")
        else:
            lines.append(f"- 样本量偏小（{note['baseline']}→{note['target']}），差异可能只是波动。")
    changed = [d for d in comparison.get('deltas', []) if d['delta_pct'] is not None and abs(d['delta_pct']) >= 5]
    if changed:
        lines.append('- 整体：' + '，'.join(
            f"{_METRIC_TITLES.get(d['metric'], d['metric'])} {d['baseline']}→{d['target']}（{d['delta_pct']:+}%"
            + ('，退化' if d['is_regression'] else '') + '）' for d in changed))
    for t in comparison.get('transactions', [])[:8]:
        change = f"{t['delta_pct']:+}%" if t['delta_pct'] is not None else '新增'
        lines.append(f"- [{t['label']}] {t['metric_title']} {t['baseline']}→{t['target']}（{change}"
                     + ('，退化' if t['is_regression'] else '') + '）')
    return lines


def format_for_prompt(evidence: Optional[dict]) -> str:
    if not evidence or (not evidence.get('baseline') and not evidence.get('previous')):
        note = '本次执行就是当前基线。' if evidence and evidence.get('is_baseline') else '没有可对比的基线或历史执行。'
        return f'## 历史对比\n{note}'
    lines = [f'## 历史对比（退化阈值 {REGRESSION_THRESHOLD_PCT:g}%，确定性计算）']
    if evidence.get('baseline'):
        lines += _format_one('与基线', evidence['baseline'])
    if evidence.get('previous'):
        lines += _format_one('与上一次执行', evidence['previous'])
    lines.append('若存在退化，请说明退化出现在哪些接口、与服务端指标是否吻合，以及最可能的变更原因。')
    return '\n'.join(lines)
