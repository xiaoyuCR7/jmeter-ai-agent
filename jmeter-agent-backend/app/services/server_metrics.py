"""Server-side evidence: Prometheus metrics for a run's time window, aligned with JMeter.

The Prometheus URL and queries come only from the project's monitoring
configuration that the user saved; requests and AI tool arguments can never
point the server at another URL. Raw query results are cached next to the JTL
so later analyses see the same evidence even after Prometheus retention ends.
"""
import json
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from app.models.schemas import DetailedTestSummary

logger = logging.getLogger(__name__)

CACHE_FILE = 'server_metrics.json'
MAX_SERIES_PER_QUERY = 8
MAX_POINTS = 240
# Below 8 points even r = 0.7 can easily come from chance (p > 0.05).
MIN_CORRELATION_POINTS = 8
STRONG_CORRELATION = 0.7


class MetricQuery(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    promql: str = Field(..., min_length=1, max_length=2000)
    unit: str = Field('', max_length=20)
    saturation: Optional[float] = Field(None, description='Values at or above this count as saturated')


class MonitoringConfig(BaseModel):
    prometheus_url: str = Field(..., max_length=500)
    queries: list[MetricQuery] = Field(default_factory=list, max_length=30)
    step_seconds: Optional[int] = Field(None, ge=1, le=3600)

    @field_validator('prometheus_url')
    @classmethod
    def _http_url(cls, value):
        parts = urlsplit(value.strip())
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password:
            raise ValueError('prometheus_url must be an http(s) URL without credentials')
        return value.strip().rstrip('/')


PRESETS: dict[str, list[MetricQuery]] = {
    'node': [
        MetricQuery(name='CPU 使用率', unit='%', saturation=85,
                    promql='100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[1m])))'),
        MetricQuery(name='内存使用率', unit='%', saturation=90,
                    promql='100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'),
        MetricQuery(name='磁盘 IO 繁忙度', unit='%', saturation=80,
                    promql='100 * max by (instance) (rate(node_disk_io_time_seconds_total[1m]))'),
        MetricQuery(name='网络入流量', unit='MB/s',
                    promql='sum by (instance) (rate(node_network_receive_bytes_total{device!="lo"}[1m])) / 1048576'),
        MetricQuery(name='1 分钟负载', unit='', promql='node_load1'),
    ],
    'jvm': [
        MetricQuery(name='JVM 堆使用率', unit='%', saturation=85,
                    promql='100 * sum by (instance) (jvm_memory_used_bytes{area="heap"}) / sum by (instance) (jvm_memory_max_bytes{area="heap"})'),
        MetricQuery(name='GC 停顿占比', unit='%', saturation=10,
                    promql='100 * sum by (instance) (rate(jvm_gc_pause_seconds_sum[1m]))'),
        MetricQuery(name='活跃线程数', unit='', promql='sum by (instance) (jvm_threads_live_threads)'),
        MetricQuery(name='连接池等待数', unit='', saturation=1,
                    promql='sum by (instance) (hikaricp_connections_pending)'),
    ],
    'mysql': [
        MetricQuery(name='MySQL 连接数', unit='', promql='mysql_global_status_threads_connected'),
        MetricQuery(name='MySQL 运行线程', unit='', promql='mysql_global_status_threads_running'),
        MetricQuery(name='MySQL 慢查询', unit='/s', saturation=1,
                    promql='rate(mysql_global_status_slow_queries[1m])'),
    ],
}


# ── Persistence ──────────────────────────────────────────────

async def get_config(project_id: str) -> Optional[MonitoringConfig]:
    from app.models.db import MonitoringConfigModel
    from app.services.db_store import db_store
    async with await db_store._session() as session:
        row = await session.get(MonitoringConfigModel, project_id)
        return MonitoringConfig.model_validate(row.data) if row else None


async def save_config(project_id: str, config: MonitoringConfig) -> MonitoringConfig:
    from app.models.db import MonitoringConfigModel
    from app.services.db_store import db_store
    async with await db_store._session() as session:
        row = await session.get(MonitoringConfigModel, project_id)
        if row is None:
            session.add(MonitoringConfigModel(project_id=project_id, data=config.model_dump()))
        else:
            row.data = config.model_dump()
        await session.commit()
    return config


async def delete_config(project_id: str) -> bool:
    from app.models.db import MonitoringConfigModel
    from app.services.db_store import db_store
    async with await db_store._session() as session:
        row = await session.get(MonitoringConfigModel, project_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


# ── Collection ───────────────────────────────────────────────

def _describe(error: Exception) -> str:
    """One readable line; httpx status errors otherwise append a documentation link."""
    if isinstance(error, httpx.HTTPStatusError):
        return f'HTTP {error.response.status_code} {error.response.reason_phrase} from {error.request.url}'[:300]
    return f'{type(error).__name__}: {error}'[:300]


def _series_label(labels: dict) -> str:
    shown = {k: v for k, v in labels.items() if k != '__name__'}
    return ','.join(f'{k}={v}' for k, v in sorted(shown.items())) or 'all'


async def _query_range(client: httpx.AsyncClient, base: str, promql: str, start: float, end: float, step: int):
    response = await client.get(f'{base}/api/v1/query_range',
                                params={'query': promql, 'start': start, 'end': end, 'step': step})
    response.raise_for_status()
    body = response.json()
    if body.get('status') != 'success':
        raise ValueError(body.get('error') or 'Prometheus query failed')
    series = []
    for item in body.get('data', {}).get('result', [])[:MAX_SERIES_PER_QUERY]:
        points = []
        for timestamp, value in item.get('values', []):
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                points.append([float(timestamp), number])
        if points:
            series.append({'labels': _series_label(item.get('metric', {})), 'points': points})
    return series


async def collect(config: MonitoringConfig, start: datetime, end: datetime, *, transport=None) -> dict:
    """Query every configured metric for the run window plus one minute either side."""
    window_start = (start - timedelta(seconds=60)).timestamp()
    window_end = (end + timedelta(seconds=60)).timestamp()
    step = config.step_seconds or max(5, math.ceil((window_end - window_start) / MAX_POINTS))
    metrics = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), transport=transport,
                                 follow_redirects=False) as client:
        for query in config.queries:
            entry = query.model_dump()
            try:
                entry['series'] = await _query_range(client, config.prometheus_url, query.promql,
                                                     window_start, window_end, step)
            except Exception as error:
                entry['series'] = []
                entry['error'] = _describe(error)
            metrics.append(entry)
    return {'collected_at': datetime.now().isoformat(timespec='seconds'), 'prometheus_url': config.prometheus_url,
            'test_start': start.timestamp(), 'test_end': end.timestamp(), 'step': step, 'metrics': metrics}


async def probe(prometheus_url: str, *, transport=None) -> dict:
    """Check that the URL answers like Prometheus, separately from whether each query matches anything."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), transport=transport, follow_redirects=False) as client:
            response = await client.get(f'{prometheus_url}/api/v1/status/buildinfo')
            response.raise_for_status()
            body = response.json()
        if body.get('status') != 'success':
            raise ValueError('response is not from the Prometheus HTTP API')
        return {'reachable': True, 'version': body.get('data', {}).get('version'), 'error': None}
    except Exception as error:
        return {'reachable': False, 'version': None, 'error': _describe(error)}


def load_cached(report_dir: Optional[str]) -> Optional[dict]:
    if not report_dir:
        return None
    try:
        with open(os.path.join(report_dir, CACHE_FILE), encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def store_cache(report_dir: Optional[str], raw: dict) -> None:
    if not report_dir or not os.path.isdir(report_dir):
        return
    # Only complete collections are cached; a Prometheus outage is retried next time.
    if any(metric.get('error') for metric in raw.get('metrics', [])):
        return
    path = os.path.join(report_dir, CACHE_FILE)
    with open(path + '.tmp', 'w', encoding='utf-8') as stream:
        json.dump(raw, stream, ensure_ascii=False)
    os.replace(path + '.tmp', path)


# ── Analysis ─────────────────────────────────────────────────

def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 4:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 2)


def _percentile(values: list[float], p: float) -> float:
    data = sorted(values)
    k = (len(data) - 1) * p
    low, high = int(k), min(int(k) + 1, len(data) - 1)
    return data[low] if low == high else data[low] * (high - k) + data[high] * (k - low)


def _timeline_buckets(summary: DetailedTestSummary) -> tuple[dict[int, tuple[float, float]], int]:
    """Map each segment's start offset to (avg response time, error rate), plus the segment width."""
    buckets, width = {}, 30
    for segment in summary.timeline_segments:
        try:
            start, end = (int(part) for part in segment.time_range.rstrip('s').split('-'))
        except ValueError:
            continue
        buckets[start] = (segment.avg_response_time, segment.error_rate)
        width = max(end - start, 1)
    return buckets, width


def analyze(raw: dict, summary: DetailedTestSummary) -> dict:
    test_start, test_end = raw['test_start'], raw['test_end']
    buckets, width = _timeline_buckets(summary)
    rows, findings, chart = [], [], []
    for metric in raw.get('metrics', []):
        if metric.get('error'):
            rows.append({'name': metric['name'], 'unit': metric.get('unit', ''), 'error': metric['error']})
            continue
        if not any(test_start <= t <= test_end for series in metric.get('series', []) for t, _ in series['points']):
            # The query ran but matched nothing in the window: wrong labels, exporter missing, or past retention.
            rows.append({'name': metric['name'], 'unit': metric.get('unit', ''), 'no_data': True})
            continue
        for series in metric.get('series', []):
            inside = [(t, v) for t, v in series['points'] if test_start <= t <= test_end]
            if not inside:
                continue
            values = [v for _, v in inside]
            peak_t, peak_v = max(inside, key=lambda point: point[1])
            row = {
                'name': metric['name'], 'unit': metric.get('unit', ''), 'series': series['labels'],
                'avg': round(sum(values) / len(values), 2), 'max': round(peak_v, 2),
                'p95': round(_percentile(values, 0.95), 2), 'peak_at_s': int(peak_t - test_start),
                'saturation': metric.get('saturation'), 'above_saturation_pct': None, 'corr_response_time': None,
                'corr_points': 0, 'corr_strong': False,
            }
            if metric.get('saturation') is not None:
                row['above_saturation_pct'] = round(
                    sum(v >= metric['saturation'] for v in values) / len(values) * 100, 1)
            # Average the metric inside each JMeter segment, then correlate.
            per_bucket = {}
            for t, v in inside:
                per_bucket.setdefault(int((t - test_start) // width) * width, []).append(v)
            common = sorted(set(per_bucket) & set(buckets))
            row['corr_response_time'] = _pearson([sum(per_bucket[b]) / len(per_bucket[b]) for b in common],
                                                 [buckets[b][0] for b in common])
            row['corr_points'] = len(common)
            row['corr_strong'] = (row['corr_response_time'] is not None and len(common) >= MIN_CORRELATION_POINTS
                                  and row['corr_response_time'] >= STRONG_CORRELATION)
            rows.append(row)
            chart.append({'name': metric['name'], 'unit': metric.get('unit', ''), 'series': series['labels'],
                          'points': [[round(t - test_start), round(v, 3)] for t, v in series['points']]})
            label = f"{metric['name']}[{series['labels']}]"
            if row['above_saturation_pct']:
                findings.append(f"{label} 有 {row['above_saturation_pct']}% 的时间达到饱和阈值 "
                                f"{metric['saturation']}{row['unit']}（峰值 {row['max']}{row['unit']}，第 {row['peak_at_s']}s）")
            if row['corr_strong']:
                findings.append(f"{label} 与响应时间强正相关（r={row['corr_response_time']}，{row['corr_points']} 个时间段）")
    return {'status': 'ok', 'collected_at': raw.get('collected_at'), 'prometheus_url': raw.get('prometheus_url'),
            'step': raw.get('step'), 'metrics': rows, 'findings': findings, 'chart': chart}


async def evidence_for(execution, summary: DetailedTestSummary) -> dict:
    get = execution.get if isinstance(execution, dict) else lambda key, default=None: getattr(execution, key, default)
    config = await get_config(get('project_id') or 'default')
    if config is None or not config.queries:
        return {'status': 'not_configured'}
    start, end = get('start_time'), get('end_time')
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    if not start or not end or end <= start:
        return {'status': 'error', 'error': 'Execution has no usable start/end time'}
    raw = load_cached(get('report_dir'))
    if raw is None or raw.get('prometheus_url') != config.prometheus_url:
        raw = await collect(config, start, end)
        try:
            store_cache(get('report_dir'), raw)
        except OSError as error:
            logger.warning('Could not cache server metrics: %s', error)
    result = analyze(raw, summary)
    if result['metrics'] and all(row.get('error') for row in result['metrics']):
        result['status'] = 'error'
        result['error'] = result['metrics'][0]['error']
    elif not any('avg' in row for row in result['metrics']):
        result['status'] = 'no_data'
    return result


def format_for_prompt(evidence: Optional[dict]) -> str:
    if not evidence or evidence.get('status') == 'not_configured':
        return ('## 服务端指标\n未配置服务端监控。根因只能由客户端数据推测，必须以【假设】形式给出，'
                '并建议需要采集的服务端指标（CPU、内存、GC、连接池、慢查询等）。')
    if evidence.get('status') == 'error':
        return f"## 服务端指标\n采集失败：{evidence.get('error')}。不要编造服务端数据。"
    if evidence.get('status') == 'no_data':
        return ('## 服务端指标\nPrometheus 可访问，但压测时间窗内所有查询都没有数据。'
                '根因只能以【假设】形式给出，不要编造服务端数据。')
    lines =['## 服务端指标（Prometheus，压测时间窗内，确定性计算）']
    for row in evidence.get('metrics', []):
        if row.get('error'):
            lines.append(f"- {row['name']}：采集失败（{row['error']}）")
            continue
        if row.get('no_data'):
            lines.append(f"- {row['name']}：压测时间窗内无数据")
            continue
        unit = row['unit']
        extra = []
        if row.get('above_saturation_pct') is not None:
            extra.append(f"≥{row['saturation']}{unit} 的时间占 {row['above_saturation_pct']}%")
        if row.get('corr_response_time') is not None:
            points = row.get('corr_points', 0)
            extra.append(f"与响应时间相关系数 {row['corr_response_time']}（{points} 个时间段"
                         + ('' if points >= MIN_CORRELATION_POINTS else '，点数太少，不可作为证据') + '）')
        lines.append(f"- {row['name']}[{row['series']}]：平均 {row['avg']}{unit}，P95 {row['p95']}{unit}，"
                     f"峰值 {row['max']}{unit}（第 {row['peak_at_s']}s）" + ('；' + '；'.join(extra) if extra else ''))
    if evidence.get('findings'):
        lines.append('关键信号：' + '；'.join(evidence['findings']))
    lines.append('根因判断须引用上述服务端指标；指标与现象不一致时要明确指出。'
                 '相关不等于因果：爬坡时负载和响应时间会一起上升，要结合时间点判断。')
    return '\n'.join(lines)
