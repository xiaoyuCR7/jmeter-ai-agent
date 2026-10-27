"""Webhook delivery for lifecycle events. No network: requests go to an httpx MockTransport."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.models.schemas import (
    DetailedTestSummary, ReportAnalysis, TestExecutionInfo as Execution, TestExecutionStatus as Status,
)
from app.services import notifications
from app.services import requirements as requirement_service


@pytest.fixture
def webhook(monkeypatch):
    """Collects delivered requests; ``replies`` scripts the receiver's status codes (or responses)."""
    received, replies = [], []
    def handle(request):
        received.append(request)
        reply = replies.pop(0) if replies else 200
        return reply if isinstance(reply, httpx.Response) else httpx.Response(reply)
    state = SimpleNamespace(handler=handle)  # a test may swap the handler, e.g. to raise
    real_client = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: state.handler(request)), **kwargs))
    monkeypatch.setattr(notifications, 'RETRY_DELAYS', (0, 0, 0))
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_URL', 'https://hooks.example.test/jmeter')
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_SECRET', None)
    for flag in ('WEBHOOK_ON_START', 'WEBHOOK_ON_COMPLETE', 'WEBHOOK_ON_FAILURE', 'WEBHOOK_ON_ANALYSIS'):
        monkeypatch.setattr(notifications.settings, flag, True)
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_INCLUDE_ANALYSIS', False)
    monkeypatch.setattr(requirement_service, 'get_requirements', AsyncMock(return_value=None))
    state.received, state.replies = received, replies
    state.bodies = lambda: [json.loads(r.content) for r in received]
    return state


def _execution(status=Status.COMPLETED, **fields):
    start = datetime(2026, 10, 27, 10, 0, 0)
    return Execution(id='run-1', project_id='proj', test_plan_id='plan-x', status=status,
                     jmx_path='missing.jmx', start_time=start, end_time=start + timedelta(seconds=61), **fields)


def _summary():
    return DetailedTestSummary(total_requests=100, avg_response_time=120, min_response_time=5, max_response_time=900,
                               error_rate=0.5, throughput=10, received_bytes_per_sec=1, sent_bytes_per_sec=1,
                               percentiles={'p95': 300, 'p99': 600})


async def _deliver(event, execution):
    notifications._send(event, execution)
    await notifications.drain()


@pytest.mark.asyncio
async def test_completed_payload_carries_context_metrics_and_verdict(webhook):
    await _deliver('test.completed', _execution(summary=_summary()))
    [body] = webhook.bodies()
    assert body['event'] == 'test.completed' and body['status'] == 'completed'
    assert body['execution_id'] == 'run-1' and body['project_id'] == 'proj' and body['test_plan_id'] == 'plan-x'
    assert body['duration_seconds'] == 61.0
    assert body['summary']['p95_response_time'] == 300 and body['summary']['p99_response_time'] == 600
    assert body['requirements'] == {'verdict': 'none', 'passed': 0, 'failed': 0, 'missing': 0}
    request = webhook.received[0]
    assert request.headers['X-JMeter-Agent-Event'] == 'test.completed'
    assert request.headers['X-JMeter-Agent-Delivery'] == body['delivery_id']
    assert 'X-JMeter-Agent-Signature' not in request.headers


@pytest.mark.asyncio
async def test_signature_covers_the_timestamp_and_raw_body(webhook, monkeypatch):
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_SECRET', 's3cret')
    await _deliver('test.started', _execution(Status.RUNNING))
    request = webhook.received[0]
    timestamp = request.headers['X-JMeter-Agent-Timestamp']
    assert abs(int(timestamp) - time.time()) < 60
    expected = 'sha256=' + hmac.new(b's3cret', timestamp.encode() + b'.' + request.content, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(request.headers['X-JMeter-Agent-Signature'], expected)
    # The same body under a later timestamp needs a signature only the secret holder can make.
    replayed = 'sha256=' + hmac.new(b's3cret', b'9999999999.' + request.content, hashlib.sha256).hexdigest()
    assert request.headers['X-JMeter-Agent-Signature'] != replayed


@pytest.mark.asyncio
async def test_redirects_count_as_failures_and_are_not_followed(webhook, caplog):
    webhook.replies.append(httpx.Response(302, headers={'Location': 'https://elsewhere.example.test/'}))
    await _deliver('test.completed', _execution())
    assert len(webhook.received) == 1
    assert 'not delivered' in caplog.text and 'redirects' in caplog.text


@pytest.mark.asyncio
async def test_logs_name_the_host_but_never_the_tokened_url_or_raw_reply(webhook, monkeypatch, caplog):
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_URL', 'https://hooks.example.test/T0K3N?key=SECRETKEY')
    webhook.replies.append(httpx.Response(403, content=b'denied\nFAKE LOG LINE ' + b'x' * 5000))
    await _deliver('test.completed', _execution())
    assert 'hooks.example.test' in caplog.text
    assert 'T0K3N' not in caplog.text and 'SECRETKEY' not in caplog.text
    assert 'denied FAKE LOG LINE' in caplog.text and 'x' * 300 not in caplog.text
    def unreachable(request):
        raise httpx.ConnectError(f'cannot reach {request.url}')
    webhook.handler = unreachable
    caplog.clear()
    await _deliver('test.completed', _execution())
    assert 'ConnectError' in caplog.text and 'T0K3N' not in caplog.text


@pytest.mark.parametrize('message, expected', [
    ('Error reading C:\\Users\\bob\\proj\\reports\\abc\\jmeter.log: denied\nTraceback (most recent call last)',
     'Error reading jmeter.log: denied'),
    ('cannot open /home/bob/plans/x.jmx (errno 2)', 'cannot open x.jmx (errno 2)'),
    ('see https://example.com/a/b for help', 'see https://example.com/a/b for help'),
    ('JMeter exited 1', 'JMeter exited 1'),
])
def test_errors_sent_out_lose_local_paths_and_extra_lines(message, expected):
    assert notifications.safe_error(message) == expected


def test_long_errors_are_clipped():
    assert len(notifications.safe_error('x' * 5000)) == notifications.ERROR_LIMIT


@pytest.mark.parametrize('url, secret, expected', [
    (None, None, []),
    ('https://hooks.example.test/x', 's', []),
    ('http://127.0.0.1:9000/hook', 's', []),
    ('https://hooks.example.test/x', None, ['WEBHOOK_SECRET is not set']),
    ('http://hooks.example.test/x', 's', ['plain http']),
    ('ftp://hooks.example.test/x', 's', ['not an http(s) URL']),
])
def test_risky_webhook_settings_are_flagged(monkeypatch, url, secret, expected):
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_URL', url)
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_SECRET', secret)
    warnings = notifications.config_warnings()
    assert len(warnings) == len(expected)
    assert all(fragment in warning for fragment, warning in zip(expected, warnings))


@pytest.mark.asyncio
async def test_server_errors_are_retried_with_the_same_delivery_id(webhook):
    webhook.replies.extend([503, 500])
    await _deliver('test.failed', _execution(Status.FAILED, error_message='JMeter exited 1'))
    assert len(webhook.received) == 3
    assert len({r.headers['X-JMeter-Agent-Delivery'] for r in webhook.received}) == 1
    assert webhook.bodies()[0]['error'] == 'JMeter exited 1'


@pytest.mark.asyncio
async def test_rejected_deliveries_are_not_retried_and_retries_give_up(webhook):
    webhook.replies.append(400)
    await _deliver('test.completed', _execution())
    assert len(webhook.received) == 1
    webhook.received.clear()
    webhook.replies.extend([500] * 10)
    await _deliver('test.completed', _execution())
    assert len(webhook.received) == len(notifications.RETRY_DELAYS) + 1


@pytest.mark.asyncio
async def test_event_switches_and_missing_url_suppress_delivery(webhook, monkeypatch):
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_ON_FAILURE', False)
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_ON_START', False)
    await _deliver('test.cancelled', _execution(Status.CANCELLED))
    await _deliver('test.failed', _execution(Status.FAILED))
    await _deliver('test.started', _execution(Status.RUNNING))
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_URL', None)
    await _deliver('test.completed', _execution())
    assert webhook.received == []


@pytest.mark.asyncio
async def test_analysis_event_sends_the_full_assessment_only_when_enabled(webhook, monkeypatch):
    analysis = ReportAnalysis(analysis_status='completed', overall_assessment='POST /internal/admin is slow',
                              conclusion='不达标', requirements_verdict={'verdict': 'fail', 'passed': 1,
                                                                       'failed': 1, 'missing': 0})
    await _deliver('analysis.completed', _execution(summary=_summary(), ai_analysis=analysis))
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_INCLUDE_ANALYSIS', True)
    await _deliver('analysis.completed', _execution(summary=_summary(), ai_analysis=analysis))
    default, full = webhook.bodies()
    assert default['analysis'] == {'status': 'completed', 'conclusion': '不达标'}
    assert full['analysis']['overall_assessment'] == 'POST /internal/admin is slow'
    assert default['requirements']['verdict'] == 'fail'


@pytest.mark.asyncio
async def test_registered_listeners_notify_every_run_without_blocking_it(webhook):
    import app.services.execution_lifecycle as module
    lifecycle = module.ExecutionLifecycle()
    notifications.register(lifecycle)
    notifications.register(lifecycle)  # registering twice must not double-send
    execution = _execution(Status.RUNNING)
    await lifecycle.emit('started', execution)
    execution.status = Status.CANCELLED
    await lifecycle.emit('finished', execution)
    await lifecycle.emit('finished', execution)  # one finish per run
    await notifications.drain()
    assert [b['event'] for b in webhook.bodies()] == ['test.started', 'test.cancelled']
    notifications.unregister(lifecycle)
    await lifecycle.emit('started', _execution(Status.RUNNING).model_copy(update={'id': 'run-2'}))
    await notifications.drain()
    assert len(webhook.received) == 2
