"""What one run measured, per transaction and over time, in the form the analysis model reads.

Everything comes from the JTL. Reports, the analysis agent and MCP all share this block,
so each of them reasons from the same interface, error and timing evidence.
"""
import json
import re
from typing import Optional

from app.models.schemas import DetailedTestSummary
from app.services.ml_analysis import ml_detector

MAX_LABELS = 30
MAX_ERRORS = 10
MAX_ANOMALIES = 8
# Below these sizes a percentile is one or two samples under another name.
MIN_LABEL_SAMPLES = 20
MAX_FOCUS = 5
# How far a request must stand out before it is named as a place to look.
SLOW_FACTOR = 2.0      # its P95 against the whole run's
WARMUP_FACTOR = 1.5    # its P95 against its own steady-window P95
TAIL_FACTOR = 5.0      # its P99 against its own average


# A failed run has no JTL to read, so its error message and log are the evidence. First match wins.
FAILURE_PATTERNS = (
    ('load_generator_busy', r'System overloaded|waiting for resources'),
    ('timed_out', r'Test timed out after'),
    ('java_missing', r"JAVA_HOME|java(\.exe)?['\"]? (is )?not (found|recognized)|No such file or directory: 'java'"),
    ('out_of_memory', r'OutOfMemoryError'),
    ('missing_plugin', r'CannotResolveClassException|ClassNotFoundException|NoClassDefFoundError|Missing plugin'),
    ('missing_file', r'FileNotFoundException|NoSuchFileException|File .{1,200} must exist|Could not read file header'),
    ('port_in_use', r'BindException|Address already in use'),
    ('remote_engine', r'Engine is busy|Failed to (configure|start|initiali[sz]e) .{0,80}(remote|engine)|ConnectException: Connection refused to host'),
    ('plan_unreadable', r'Error in NonGUIDriver|SAXParseException|Could not open|Error while processing file'),
)


def diagnose_failure(error_message: Optional[str], log_text: str = '') -> dict:
    """What most likely stopped a run: a code the UI explains, and the line that shows it."""
    lines = [*(error_message or '').splitlines(), *log_text.splitlines()]
    for code, pattern in FAILURE_PATTERNS:
        matched = next((line for line in lines if re.search(pattern, line, re.IGNORECASE)), None)
        if matched is not None:
            return {'code': code, 'line': _text(matched.strip(), 300)}
    errors = [line for line in lines if re.search(r'\b(ERROR|FATAL)\b', line)]
    return {'code': 'unknown', 'line': _text((errors[-1] if errors else error_message or '').strip(), 300)}


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


def focus(rows: list[dict], overall_p95: Optional[float]) -> list[dict]:
    """Where a reader should look first, decided by thresholds rather than by the model.

    Each item names a request, what stands out and the two numbers that show it.
    """
    items = []
    for row in rows:
        label, p95, p99, avg = row['label'], row.get('p95_response_time'), row.get('p99_response_time'),             row.get('avg_response_time')
        if (row.get('error_rate') or 0) > 0:
            items.append({'label': label, 'kind': 'errors', 'error_rate': row['error_rate'], 'count': row.get('count'),
                          'rank': 1000 + row['error_rate']})
        if p95 and overall_p95 and p95 >= SLOW_FACTOR * overall_p95:
            items.append({'label': label, 'kind': 'slow', 'p95_response_time': p95, 'run_p95': overall_p95,
                          'rank': 100 + p95 / overall_p95})
        if p95 and row.get('steady_p95') and p95 >= WARMUP_FACTOR * row['steady_p95']:
            items.append({'label': label, 'kind': 'slow_outside_steady', 'p95_response_time': p95,
                          'steady_p95': row['steady_p95'], 'rank': 10 + p95 / row['steady_p95']})
        if p99 and avg and p99 >= TAIL_FACTOR * avg:
            items.append({'label': label, 'kind': 'long_tail', 'p99_response_time': p99, 'avg_response_time': avg,
                          'rank': p99 / avg})
    items.sort(key=lambda item: -item['rank'])
    return [{key: value for key, value in item.items() if key != 'rank'} for item in items[:MAX_FOCUS]]


def build(summary: DetailedTestSummary) -> dict:
    diagnostics = summary.diagnostics or {}
    steady = diagnostics.get('steady') or {}
    steady_labels = steady.get('labels') or {}
    # Worst first, so a cut in a plan with many samplers drops the healthy ones.
    labels = sorted((summary.per_label or {}).items(),
                    key=lambda item: (-(item[1].get('error_rate') or 0), -(item[1].get('p95_response_time') or 0)))
    report = ml_detector.analyze_timeline(summary.timeline_segments)
    rows = [_label_row(name, stats, steady_labels.get(name)) for name, stats in labels[:MAX_LABELS]]
    return {
        'success': summary.success,
        'load': diagnostics.get('load'),
        'steady': {key: value for key, value in steady.items() if key != 'labels'} or None,
        'timing': diagnostics.get('timing'),
        'slowest_samples': diagnostics.get('slowest'),
        'focus': focus(rows, (summary.percentiles or {}).get('p95')),
        'labels': rows,
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
              'slowest_samples 是最慢的单个样本，offset_s 是距开始的秒数；'
              'focus 是程序按阈值挑出的最该先看的接口：errors 有错误，slow 的 P95 是整体的两倍以上，'
              'slow_outside_steady 的 P95 比自己稳定段高一半以上（慢在爬坡、收尾或建连），'
              'long_tail 的 P99 是自己平均值的五倍以上。先解释 focus 里的项，再看其它。')
