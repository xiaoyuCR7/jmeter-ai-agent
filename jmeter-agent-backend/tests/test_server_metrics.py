"""Server metrics: fake Prometheus only; windows, stats and correlation are deterministic."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.models.schemas import DetailedTestSummary, TimelineSegment
from app.services import server_metrics as sm

START = datetime(2026, 10, 1, 10, 0, 0)
END = START + timedelta(seconds=240)


def summary_with_timeline(rts):
    segments = [TimelineSegment(time_range=f'{i * 30}-{i * 30 + 30}s', avg_response_time=rt, max_response_time=rt,
                                throughput=10, error_rate=0, request_count=300) for i, rt in enumerate(rts)]
    return DetailedTestSummary(total_requests=1800, avg_response_time=100, min_response_time=1, max_response_time=900,
                               error_rate=0, throughput=10, received_bytes_per_sec=1, sent_bytes_per_sec=1,
                               timeline_segments=segments)


def prometheus(handler_values, calls=None):
    """values(query) -> list of (offset_seconds, value) series; one series per query."""
    def handler(request):
        params = dict(request.url.params)
        if calls is not None:
            calls.append(params)
        if params['query'] == 'broken':
            return httpx.Response(400, json={'status': 'error', 'error': 'bad query'})
        points = [[START.timestamp() + offset, str(value)] for offset, value in handler_values(params['query'])]
        return httpx.Response(200, json={'status': 'success', 'data': {'result': [
            {'metric': {'__name__': 'x', 'instance': 'app-1:9100'}, 'values': points}]}})
    return httpx.MockTransport(handler)


def config(*queries):
    return sm.MonitoringConfig(prometheus_url='http://prom.local:9090/', queries=list(queries))


@pytest.mark.asyncio
async def test_collect_queries_run_window_with_margin_and_records_failures():
    calls = []
    cpu = sm.MetricQuery(name='CPU', promql='cpu', unit='%', saturation=85)
    broken = sm.MetricQuery(name='Broken', promql='broken')
    raw = await sm.collect(config(cpu, broken), START, END,
                           transport=prometheus(lambda q: [(0, 10), (30, 'NaN'), (60, 'oops'), (90, 20)], calls))
    assert float(calls[0]['start']) == START.timestamp() - 60 and float(calls[0]['end']) == END.timestamp() + 60
    assert int(calls[0]['step']) >= 5
    assert raw['prometheus_url'] == 'http://prom.local:9090'
    assert [p[1] for p in raw['metrics'][0]['series'][0]['points']] == [10.0, 20.0]  # NaN/garbage dropped
    assert raw['metrics'][0]['series'][0]['labels'] == 'instance=app-1:9100'
    assert raw['metrics'][1]['error'].startswith('HTTP 400 Bad Request from http://prom.local:9090/')


@pytest.mark.asyncio
async def test_saturation_peak_and_response_time_correlation():
    # CPU climbs with response time; memory stays flat (no correlation possible).
    cpu = sm.MetricQuery(name='CPU', promql='cpu', unit='%', saturation=85)
    mem = sm.MetricQuery(name='Mem', promql='mem', unit='%')
    series = {'cpu': [(t, 40 + t / 4) for t in range(0, 241, 10)], 'mem': [(t, 50) for t in range(0, 241, 10)]}
    raw = await sm.collect(config(cpu, mem), START, END, transport=prometheus(lambda q: series[q]))
    result = sm.analyze(raw, summary_with_timeline([100, 150, 220, 300, 420, 600, 700, 800]))
    cpu_row, mem_row = result['metrics']
    assert cpu_row['max'] == 100.0 and cpu_row['peak_at_s'] == 240
    assert cpu_row['above_saturation_pct'] > 0
    assert cpu_row['corr_response_time'] >= 0.9
    assert mem_row['corr_response_time'] is None
    assert any('强正相关' in f for f in result['findings']) and any('饱和' in f for f in result['findings'])
    assert result['chart'][0]['points'][0][0] == 0


def test_points_outside_the_run_window_do_not_count():
    raw = {'test_start': START.timestamp(), 'test_end': END.timestamp(), 'metrics': [
        {'name': 'CPU', 'unit': '%', 'saturation': 85, 'series': [{'labels': 'all', 'points': [
            [START.timestamp() - 30, 99.0], [START.timestamp() + 60, 20.0], [END.timestamp() + 30, 99.0]]}]}]}
    row = sm.analyze(raw, summary_with_timeline([100]))['metrics'][0]
    assert row['max'] == 20.0 and row['above_saturation_pct'] == 0


@pytest.mark.parametrize('url', ['ftp://prom:9090', 'http://user:pw@prom:9090', 'prom:9090'])
def test_config_rejects_non_http_or_credentialed_urls(url):
    with pytest.raises(ValidationError):
        sm.MonitoringConfig(prometheus_url=url)


@pytest.mark.asyncio
async def test_evidence_caches_complete_collections_only(tmp_path, monkeypatch):
    cpu = sm.MetricQuery(name='CPU', promql='cpu', unit='%')
    calls = []

    async def fake_config(project_id):
        return config(cpu)
    monkeypatch.setattr(sm, 'get_config', fake_config)
    real_collect = sm.collect

    async def collect(cfg, start, end):
        calls.append(1)
        return await real_collect(cfg, start, end, transport=prometheus(lambda q: [(0, 10), (60, 30)]))
    monkeypatch.setattr(sm, 'collect', collect)
    execution = SimpleNamespace(project_id='p', start_time=START, end_time=END, report_dir=str(tmp_path), id='run')
    first = await sm.evidence_for(execution, summary_with_timeline([100, 120]))
    second = await sm.evidence_for(execution, summary_with_timeline([100, 120]))
    assert first['status'] == second['status'] == 'ok' and len(calls) == 1
    assert json.loads((tmp_path / sm.CACHE_FILE).read_text(encoding='utf-8'))['metrics'][0]['name'] == 'CPU'

    raw = await real_collect(config(sm.MetricQuery(name='B', promql='broken')), START, END,
                             transport=prometheus(lambda q: []))
    (tmp_path / sm.CACHE_FILE).unlink()
    sm.store_cache(str(tmp_path), raw)
    assert not (tmp_path / sm.CACHE_FILE).exists()


@pytest.mark.asyncio
async def test_unconfigured_projects_tell_the_model_evidence_is_missing(monkeypatch):
    async def none(project_id):
        return None
    monkeypatch.setattr(sm, 'get_config', none)
    evidence = await sm.evidence_for(SimpleNamespace(project_id='p'), summary_with_timeline([1]))
    assert evidence == {'status': 'not_configured'}
    assert '【假设】' in sm.format_for_prompt(evidence)


def test_prompt_lists_values_saturation_and_correlation():
    text = sm.format_for_prompt({'status': 'ok', 'findings': ['CPU 饱和'], 'metrics': [
        {'name': 'CPU', 'unit': '%', 'series': 'instance=a', 'avg': 70, 'p95': 95, 'max': 99, 'peak_at_s': 120,
         'saturation': 85, 'above_saturation_pct': 40.0, 'corr_response_time': 0.88}]})
    assert '峰值 99%（第 120s）' in text and '40.0%' in text and '0.88' in text and 'CPU 饱和' in text


def test_queries_without_points_in_the_window_are_reported_as_no_data():
    raw = {'test_start': START.timestamp(), 'test_end': END.timestamp(), 'metrics': [
        {'name': 'CPU', 'unit': '%', 'series': []},
        {'name': 'Mem', 'unit': '%', 'series': [{'labels': 'all', 'points': [[END.timestamp() + 30, 50.0]]}]}]}
    rows = sm.analyze(raw, summary_with_timeline([100]))['metrics']
    assert rows == [{'name': 'CPU', 'unit': '%', 'no_data': True}, {'name': 'Mem', 'unit': '%', 'no_data': True}]
    assert '无数据' in sm.format_for_prompt({'status': 'ok', 'metrics': rows})


@pytest.mark.asyncio
async def test_evidence_says_no_data_when_nothing_matched(tmp_path, monkeypatch):
    async def fake_config(project_id):
        return config(sm.MetricQuery(name='CPU', promql='cpu'))
    monkeypatch.setattr(sm, 'get_config', fake_config)
    real_collect = sm.collect

    async def collect(cfg, start, end):
        return await real_collect(cfg, start, end, transport=prometheus(lambda q: []))
    monkeypatch.setattr(sm, 'collect', collect)
    execution = SimpleNamespace(project_id='p', start_time=START, end_time=END, report_dir=str(tmp_path), id='run')
    evidence = await sm.evidence_for(execution, summary_with_timeline([100]))
    assert evidence['status'] == 'no_data'
    assert '【假设】' in sm.format_for_prompt(evidence)


@pytest.mark.asyncio
async def test_probe_separates_unreachable_from_reachable():
    def handler(request):
        assert request.url.path == '/api/v1/status/buildinfo'
        return httpx.Response(200, json={'status': 'success', 'data': {'version': '2.53.0'}})
    ok = await sm.probe('http://prom.local:9090', transport=httpx.MockTransport(handler))
    assert ok == {'reachable': True, 'version': '2.53.0', 'error': None}
    not_prometheus = await sm.probe('http://prom.local:9090', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'hello': 'world'})))
    assert not_prometheus['reachable'] is False and 'Prometheus' in not_prometheus['error']
    down = await sm.probe('http://prom.local:9090', transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError('refused'))))
    assert down['reachable'] is False and 'ConnectError' in down['error']
