"""Predictions a later run can check: one metric, optionally of one request, under a stated load.

A prediction holding says only that the numbers moved as predicted. It does not prove the
explanation behind it, so the ledger keeps the explanation as a hypothesis either way.
"""
import math
import re
from typing import Optional

METRICS = ('p95', 'p99', 'avg', 'error_rate', 'throughput')
CHANGES = ('decrease', 'increase', 'below', 'above')
_ALIASES = {
    'p95_response_time': 'p95', 'p99_response_time': 'p99', 'avg_response_time': 'avg', 'average': 'avg',
    '平均': 'avg', '平均响应时间': 'avg', 'tps': 'throughput', '吞吐量': 'throughput', '错误率': 'error_rate',
}
_TOTALS = {'p95': lambda f: f.percentiles.get('p95'), 'p99': lambda f: f.percentiles.get('p99'),
           'avg': lambda f: f.avg_response_time, 'error_rate': lambda f: f.error_rate,
           'throughput': lambda f: f.throughput}
_PER_LABEL = {'p95': 'p95_response_time', 'p99': 'p99_response_time', 'avg': 'avg_response_time',
              'error_rate': 'error_rate', 'throughput': 'throughput'}
_NAMES = {'p95': 'P95 ', 'p99': 'P99 ', 'avg': '平均响应时间', 'error_rate': '错误率', 'throughput': '吞吐量'}
_UNITS = {'p95': 'ms', 'p99': 'ms', 'avg': 'ms', 'error_rate': '%', 'throughput': '/s'}


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize(raw, labels) -> Optional[dict]:
    """A model's prediction in the one shape the ledger checks, or None when it cannot be checked."""
    if not isinstance(raw, dict):
        return None
    metric = str(raw.get('metric') or '').strip().lower()
    metric = _ALIASES.get(metric, metric)
    change = str(raw.get('change') or '').strip().lower()
    value = _number(raw.get('value'))
    if metric not in METRICS or change not in CHANGES or value is None or value < 0:
        return None
    if change in ('decrease', 'increase') and value > 1000:
        return None  # A percentage; anything larger is a misread threshold.
    prediction = {'metric': metric, 'change': change, 'value': value}
    label = str(raw.get('label') or '').strip()
    if label:
        if label not in labels:
            return None  # A request this run does not have can never be compared.
        prediction['label'] = label
    threads = _number(raw.get('threads'))
    if threads is not None:
        if threads < 1 or threads != int(threads):
            return None
        prediction['threads'] = int(threads)
    after = str(raw.get('after') or '').strip()
    if after:
        prediction['after'] = after[:120]
    return prediction


def checkable(prediction: Optional[dict]) -> bool:
    """A program can settle it: no change other than the thread count has to be made first.

    A prediction about a fix ("after warming the pool") read against a plain rerun would be
    refuted by a run that never made the fix, so it waits for a person instead.
    """
    return bool(prediction) and not prediction.get('after')


def signature(prediction: Optional[dict]) -> Optional[tuple]:
    """What two predictions must share to be the same claim, whatever numbers they state."""
    if not prediction:
        return None
    return (prediction['metric'], prediction.get('label'), prediction['change'], prediction.get('threads'),
            bool(prediction.get('after')))


def from_text(text: Optional[str]) -> Optional[dict]:
    """Older hypotheses wrote predictions as text; the two forms they were asked for still count."""
    text = (text or '').strip().lower()
    directional = re.fullmatch(
        r'(p99|throughput|tps|吞吐量)\s*(?:should\s*)?(decrease|下降|降低|increase|提升|增加)'
        r'(?:\s*(?:by\s*)?(?:>=|>|至少|超过)?\s*(\d+(?:\.\d+)?)%)?', text)
    if directional:
        metric, direction, value = directional.groups()
        return {'metric': 'p99' if metric == 'p99' else 'throughput',
                'change': 'decrease' if direction in ('decrease', '下降', '降低') else 'increase',
                'value': float(value or 0)}
    bound = re.fullmatch(r'(?:error_rate|错误率)\s*(?:should\s*)?(?:drop\s*)?(?:below|低于)\s*(\d+(?:\.\d+)?)%', text)
    if bound:
        return {'metric': 'error_rate', 'change': 'below', 'value': float(bound.group(1))}
    return None


def describe(prediction: Optional[dict]) -> str:
    if not prediction:
        return ''
    target = f"{prediction['label']} 的" if prediction.get('label') else '整体'
    value, unit = prediction['value'], _UNITS[prediction['metric']]
    how = {'decrease': f'下降至少 {value:g}%', 'increase': f'上升至少 {value:g}%',
           'below': f'低于 {value:g}{unit}', 'above': f'高于 {value:g}{unit}'}[prediction['change']]
    when = f"（线程数 {prediction['threads']}）" if prediction.get('threads') else '（负载不变）'
    after = f"，前提：{prediction['after']}" if prediction.get('after') else ''
    return f"{target}{_NAMES[prediction['metric']]}{how}{when}{after}"


def _value(fact, prediction) -> Optional[float]:
    label = prediction.get('label')
    if label:
        row = (fact.per_label or {}).get(label) or {}
        if not row.get('count'):
            return None
        return _number(row.get(_PER_LABEL[prediction['metric']]))
    return _number(_TOTALS[prediction['metric']](fact))


def applies(prediction: dict, source, run) -> bool:
    """Whether ``run`` is the run the prediction was about: same project and environment, stated load."""
    if not checkable(prediction):
        return False
    if run.execution_id == source.execution_id or run.total_requests <= 0 or source.total_requests <= 0:
        return False
    if not source.project_id or source.project_id != run.project_id:
        return False
    if not source.environment_fingerprint or source.environment_fingerprint != run.environment_fingerprint:
        return False
    threads = run.load.get('threads')
    if prediction.get('threads'):
        return threads == prediction['threads']
    # No load stated: the prediction is about running the same load again.
    return bool(threads) and threads == source.load.get('threads') \
        and run.load.get('think_time_ms') == source.load.get('think_time_ms')


def check(prediction: dict, source, run) -> Optional[tuple[bool, str]]:
    """(held, note) when ``run`` can answer the prediction, otherwise None."""
    if not applies(prediction, source, run):
        return None
    change, value = prediction['change'], prediction['value']
    if not math.isfinite(value) or (change in ('decrease', 'increase') and value > 1000):
        return None
    new = _value(run, prediction)
    if new is None or new < 0 or (prediction['metric'] == 'error_rate' and new > 100):
        return None
    if change in ('below', 'above'):
        held = new < value if change == 'below' else new > value
        return held, f"只检查了预测：{describe(prediction)}；运行 {run.execution_id} 实测 {new:g}。"
    old = _value(source, prediction)
    if old is None or old <= 0:
        return None
    delta = (new - old) / old * 100
    if not math.isfinite(delta):
        return None
    held = (-delta if change == 'decrease' else delta) >= value
    return held, (f"只检查了预测：{describe(prediction)}；{old:g} → {new:g}（{delta:+.1f}%），"
                  f"运行 {source.execution_id} → {run.execution_id}。")
