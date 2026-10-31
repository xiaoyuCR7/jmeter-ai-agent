"""What one run measured, per transaction and over time, in the form the analysis model reads.

Everything comes from the JTL. Reports, the analysis agent and MCP all share this block,
so each of them reasons from the same interface, error and timing evidence.
"""
import json
from typing import Optional

from app.models.schemas import DetailedTestSummary
from app.services.ml_analysis import ml_detector

MAX_LABELS = 30
MAX_ERRORS = 10
MAX_ANOMALIES = 8
# Below these sizes a percentile is one or two samples under another name.
MIN_LABEL_SAMPLES = 20


def _text(value, limit: int = 160) -> str:
    return str(value)[:limit]


def _label_row(name: str, stats: dict, steady: Optional[dict]) -> dict:
    success = stats.get('success') or {}
    row = {'label': _text(name), 'count': stats.get('count'), 'error_rate': stats.get('error_rate'),
           'avg_response_time': stats.get('avg_response_time'), 'p95_response_time': stats.get('p95_response_time'),
           'p99_response_time': stats.get('p99_response_time'), 'max_response_time': stats.get('max_response_time'),
           'throughput': stats.get('throughput'), 'success_p95': success.get('p95_response_time')}
    if steady:
        row['steady_p95'], row['steady_p99'] = steady.get('p95_response_time'), steady.get('p99_response_time')
    if stats.get('is_transaction'):
        row['is_transaction'] = True
    if (stats.get('count') or 0) < MIN_LABEL_SAMPLES:
        row.update({key: None for key in ('p95_response_time', 'p99_response_time', 'success_p95',
                                          'steady_p95', 'steady_p99') if key in row})
    return row


def build(summary: DetailedTestSummary) -> dict:
    diagnostics = summary.diagnostics or {}
    steady = diagnostics.get('steady') or {}
    steady_labels = steady.get('labels') or {}
    # Worst first, so a cut in a plan with many samplers drops the healthy ones.
    labels = sorted((summary.per_label or {}).items(),
                    key=lambda item: (-(item[1].get('error_rate') or 0), -(item[1].get('p95_response_time') or 0)))
    report = ml_detector.analyze_timeline(summary.timeline_segments)
    return {
        'success': summary.success,
        'load': diagnostics.get('load'),
        'steady': {key: value for key, value in steady.items() if key != 'labels'} or None,
        'timing': diagnostics.get('timing'),
        'slowest_samples': diagnostics.get('slowest'),
        'labels': [_label_row(name, stats, steady_labels.get(name)) for name, stats in labels[:MAX_LABELS]],
        'labels_total': len(labels),
        'errors': [{'error_type': _text(error.error_type), 'count': error.count, 'percentage': error.percentage,
                    'labels': {_text(name): count for name, count in error.labels.items()}}
                   for error in summary.error_details[:MAX_ERRORS]],
        'anomalies': [anomaly.description for anomaly in report.anomalies[:MAX_ANOMALIES]],
    }


def format_for_prompt(run: Optional[dict]) -> str:
    if not run:
        return ''
    return ('## 运行诊断（由 JTL 确定性计算）\n'
            + json.dumps(run, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            + '\n字段说明：labels 是接口明细，按错误率和 P95 从差到好排序，分位数为 null 表示样本太少；'
              'errors.labels 是错误集中的接口；steady_p95、steady_p99 是稳定段的分位数；'
              'slowest_samples 是最慢的单个样本，offset_s 是距开始的秒数。')
