"""The live view of a running execution: 5-second buckets from the JTL, the planned duration, and its endpoint."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.jmeter import live_stats, run_guard
from app.services.jmeter.live_stats import LiveStats
from app.services.jmeter.load_variants import planned_seconds

T0 = 1_700_000_000_000  # A bucket boundary: divisible by 5000.


def group(scheduler='true', duration='300', delay='0', enabled='true'):
    return (f'<ThreadGroup testname="g" enabled="{enabled}"><stringProp name="ThreadGroup.num_threads">10</stringProp>'
            f'<boolProp name="ThreadGroup.scheduler">{scheduler}</boolProp>'
            f'<stringProp name="ThreadGroup.duration">{duration}</stringProp>'
            f'<stringProp name="ThreadGroup.delay">{delay}</stringProp></ThreadGroup><hashTree/>')


def plan(*groups) -> bytes:
    return (f'<?xml version="1.0"?><jmeterTestPlan><hashTree><TestPlan/><hashTree>{"".join(groups)}'
            f'</hashTree></hashTree></jmeterTestPlan>').encode()


def test_a_scheduled_plan_runs_for_its_longest_group():
    assert planned_seconds(plan(group(duration='300'), group(duration='120', delay='240'))) == 360
    assert planned_seconds(plan(group(duration='60'), group(duration='999', enabled='false'))) == 60


@pytest.mark.parametrize('loose', [group(scheduler='false'), group(duration='${DURATION}'), group(delay='${D}')])
def test_a_plan_that_ends_by_loop_count_or_variables_has_no_planned_time(loose):
    assert planned_seconds(plan(group(), loose)) is None
    assert planned_seconds(b'not xml') is None


def test_rows_fill_five_second_buckets_and_the_newest_is_partial():
    stats = LiveStats(planned_seconds=60, planned_threads=10)
    for second in range(12):  # 12 s of traffic: 10 requests a second, every tenth fails.
        for i in range(10):
            stats.add(T0 + second * 1000 + i * 100, 100 + i * 10, success=i != 0, label='login', threads=second + 1)
    snap = stats.snapshot()
    series = snap['series']
    assert [point['t'] for point in series] == [0, 5, 10]
    assert [point['requests'] for point in series] == [50, 50, 20]
    assert series[0]['throughput'] == 10.0 and series[0]['error_rate'] == 10.0
    assert series[0]['avg'] == 145.0 and series[0]['p95'] == 190.0 and series[0]['threads'] == 5
    assert [point['partial'] for point in series] == [False, False, True]
    assert snap['totals'] == {'requests': 120, 'errors': 12, 'error_rate': 10.0, 'avg': 145.0,
                              'throughput': round(120 / 11.9, 2), 'threads': 12}  # First to last start time.
    assert snap['window']['seconds'] == 10 and snap['window']['requests'] == 100  # Finished buckets only.
    assert snap['labels'] == [{'label': 'login', 'requests': 120, 'errors': 12, 'error_rate': 10.0, 'avg': 145.0}]


def test_requests_with_errors_come_first_then_the_slowest():
    stats = LiveStats()
    stats.add(T0, 900, True, 'slow')
    stats.add(T0, 50, False, 'failing')
    stats.add(T0, 300, True, 'medium')
    assert [row['label'] for row in stats.snapshot()['labels']] == ['failing', 'slow', 'medium']


def test_a_late_row_still_lands_in_its_bucket_and_settled_buckets_keep_their_p95():
    stats = LiveStats()
    stats.add(T0 + 6000, 100, True)
    stats.add(T0 + 1000, 400, True)  # Started earlier, finished later: written after.
    assert [point['t'] for point in stats.snapshot()['series']] == [0, 5]
    stats.add(T0 + 60_000, 100, True)  # A minute on: the first buckets settle.
    first = stats.snapshot()['series'][0]
    assert stats.buckets[T0 // 5000].times is None and first['p95'] == 400.0


def test_only_the_last_hour_of_buckets_is_kept():
    stats = LiveStats()
    for i in range(live_stats.KEEP_BUCKETS + 10):
        stats.add(T0 + i * 5000, 10, True)
    assert len(stats.snapshot()['series']) == live_stats.KEEP_BUCKETS


def test_progress_follows_the_clock_when_the_plan_has_a_duration(monkeypatch):
    stats = LiveStats(planned_seconds=200)
    monkeypatch.setattr(time, 'monotonic', lambda: stats.started + 50)
    assert stats.progress() == 25
    assert stats.snapshot()['remaining_seconds'] == 150
    monkeypatch.setattr(time, 'monotonic', lambda: stats.started + 500)
    assert stats.progress() == 99  # Never 100 before JMeter has actually ended.
    assert LiveStats().progress() is None


@pytest.mark.asyncio
async def test_the_watch_feeds_the_live_view_even_with_the_guard_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'RUN_GUARD_ENABLED', False)
    path = tmp_path / 'result.jtl'
    path.write_text('timeStamp,elapsed,label,success,allThreads\n'
                    + ''.join(f'{T0 + i},{20 + i},home,true,3\n' for i in range(5)), encoding='utf-8')
    stats = LiveStats()
    task = asyncio.create_task(run_guard.watch(str(path), None, live=stats, check_seconds=0.01))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if stats.requests == 5:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert stats.requests == 5 and stats.snapshot()['totals']['threads'] == 3


@pytest.mark.asyncio
async def test_the_live_endpoint_says_when_there_is_nothing_live(monkeypatch):
    from app.api import execution_routes
    from app.models.schemas import TestExecutionStatus
    execution = SimpleNamespace(status=TestExecutionStatus.RUNNING, progress=40, start_time=None, test_plan_id='plan-1',
                                project_id='p', context_snapshot={'purpose': {'kind': 'capacity', 'step': 2}})
    monkeypatch.setattr(execution_routes, 'store', SimpleNamespace(
        executions={'run-1': execution},
        get_test_plan=lambda plan_id: SimpleNamespace(name='Checkout') if plan_id == 'plan-1' else None))
    idle = await execution_routes.get_execution_live('run-1')
    assert idle['available'] is False and idle['purpose'] == {'kind': 'capacity', 'step': 2}
    assert idle['plan_name'] == 'Checkout' and idle['project_id'] == 'p'
    live_stats.start('run-1', 60, 10).add(T0, 120, True, 'home')
    try:
        live = await execution_routes.get_execution_live('run-1')
    finally:
        live_stats.finish('run-1')
    assert live['available'] is True and live['totals']['requests'] == 1 and live['planned_threads'] == 10
    with pytest.raises(Exception):
        await execution_routes.get_execution_live('missing')


@pytest.mark.asyncio
async def test_the_executor_keeps_ticking_for_as_long_as_jmeter_runs(tmp_path, monkeypatch):
    """It once stopped calling after its own 5%-a-tick guess reached 95, freezing real progress after 95 s."""
    import asyncio
    from app.services.jmeter import executor as module
    monkeypatch.setattr(module, 'PROGRESS_TICK_SECONDS', 0.001)
    flag, ticks = tmp_path / '_done.flag', []

    def callback(pct):
        ticks.append(pct)
        if len(ticks) == 40:
            flag.write_text('COMPLETED')
    await asyncio.wait_for(module.JMeterExecutor.__new__(module.JMeterExecutor)._monitor_progress('x', str(flag), callback), 5)
    assert ticks[:40] == [None] * 40 and ticks[-1] == 100


@pytest.mark.asyncio
async def test_the_live_stream_sends_changes_and_closes_when_the_run_ends(monkeypatch):
    import json
    from types import SimpleNamespace
    from app.api import execution_routes
    from app.models.schemas import TestExecutionInfo, TestExecutionStatus
    execution = TestExecutionInfo(id='run-9', test_plan_id='p', status=TestExecutionStatus.RUNNING, jmx_path='x.jmx')
    monkeypatch.setattr(execution_routes.store, '_executions', {'run-9': execution})
    monkeypatch.setattr(execution_routes.store, '_test_plans', {})
    monkeypatch.setattr(execution_routes, 'LIVE_PUSH_SECONDS', 0.01)
    monkeypatch.setattr(execution_routes, 'LIVE_KEEPALIVE_SECONDS', 0.03)

    async def connected():
        return False
    request = SimpleNamespace(is_disconnected=connected)
    response = await execution_routes.stream_execution_live('run-9', request)
    assert response.media_type == 'text/event-stream'
    received = []

    async def read():
        async for chunk in response.body_iterator:
            received.append(chunk)
    reader = asyncio.create_task(read())
    await asyncio.sleep(0.15)
    # Nothing changed meanwhile: one view, then only keep-alive comments.
    views = [chunk for chunk in received if chunk.startswith('data: ')]
    assert len(views) == 1 and json.loads(views[0][6:])['status'] == 'running'
    assert any(chunk.startswith(':') for chunk in received)
    execution.status = TestExecutionStatus.COMPLETED
    await asyncio.wait_for(reader, timeout=2)
    views = [json.loads(chunk[6:]) for chunk in received if chunk.startswith('data: ')]
    assert [view['status'] for view in views] == ['running', 'completed']

    with pytest.raises(execution_routes.HTTPException):
        await execution_routes.stream_execution_live('missing', request)
