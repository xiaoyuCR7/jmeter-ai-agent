"""Webhook delivery for lifecycle events. No network: requests go to an httpx MockTransport."""
import hashlib
import hmac
import json
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
    """Collects delivered requests; ``replies`` scripts the receiver's status codes."""
    received, replies = [], []
    def handle(request):
        received.append(request)
        return httpx.Response(replies.pop(0) if replies else 200)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, 'AsyncClient',
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(notifications, 'RETRY_DELAYS', (0, 0, 0))
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_URL', 'https://hooks.example.test/jmeter')
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_SECRET', None)
    for flag in ('WEBHOOK_ON_START', 'WEBHOOK_ON_COMPLETE', 'WEBHOOK_ON_FAILURE', 'WEBHOOK_ON_ANALYSIS'):
        monkeypatch.setattr(notifications.settings, flag, True)
    monkeypatch.setattr(requirement_service, 'get_requirements', AsyncMock(return_value=None))
    return SimpleNamespace(received=received, replies=replies,
                           bodies=lambda: [json.loads(r.content) for r in received])


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
async def test_signature_is_hmac_of_the_raw_body(webhook, monkeypatch):
    monkeypatch.setattr(notifications.settings, 'WEBHOOK_SECRET', 's3cret')
    await _deliver('test.started', _execution(Status.RUNNING))
    request = webhook.received[0]
    expected = 'sha256=' + hmac.new(b's3cret', request.content, hashlib.sha256).hexdigest()
    assert request.headers['X-JMeter-Agent-Signature'] == expected


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
async def test_analysis_event_reports_the_analysis_and_its_verdict(webhook):
    analysis = ReportAnalysis(analysis_status='failed', overall_assessment='AI analysis failed: timeout',
                              conclusion='AI 分析未完成', requirements_verdict={'verdict': 'fail', 'passed': 1,
                                                                            'failed': 1, 'missing': 0})
    await _deliver('analysis.completed', _execution(summary=_summary(), ai_analysis=analysis))
    [body] = webhook.bodies()
    assert body['analysis'] == {'status': 'failed', 'overall_assessment': 'AI analysis failed: timeout',
                                'conclusion': 'AI 分析未完成'}
    assert body['requirements']['verdict'] == 'fail'


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
