"""Webhook notifications for execution lifecycle events.

``register()`` subscribes this module to the execution lifecycle once at startup, so
every run notifies the same way whichever entry point started it (Web, agent tools,
scheduler, MCP). Deliveries run in the background: a slow or failing receiver never
holds up a run, and failed deliveries are retried.

Webhook payload format (POST JSON):
{
  "event": "test.started" | "test.completed" | "test.failed" | "test.cancelled" | "analysis.completed",
  "delivery_id": "...",          # also X-JMeter-Agent-Delivery; unchanged across retries
  "timestamp": "2026-...",
  "execution_id": "...", "project_id": "...", "test_plan_id": "...", "test_plan_name": "...",
  "status": "running" | "completed" | "failed" | "cancelled",
  "start_time": "...", "end_time": "...", "duration_seconds": 60.2,
  "summary": { ... key metrics ... },
  "requirements": {"verdict": "pass" | "fail" | "incomplete" | "none", "passed": 3, "failed": 1, "missing": 0},
  "error": "...",
  "analysis": { ... }            # analysis.completed only
}

With WEBHOOK_SECRET set, X-JMeter-Agent-Signature is "sha256=" followed by the hex
HMAC-SHA256 of the raw request body, keyed with the secret.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from typing import Optional

import httpx

from app.core.config import settings
from app.core.time import utc_now_naive
from app.services.storage import store

logger = logging.getLogger(__name__)

FINISHED_EVENTS = {'completed': 'test.completed', 'failed': 'test.failed', 'cancelled': 'test.cancelled'}
# Seconds to wait before each retry; network errors, 408, 429 and 5xx are retried.
RETRY_DELAYS = (1.0, 5.0, 15.0)

_pending: set[asyncio.Task] = set()


def _enabled(event: str) -> bool:
    if not settings.WEBHOOK_URL:
        return False
    return {
        'test.started': settings.WEBHOOK_ON_START,
        'test.completed': settings.WEBHOOK_ON_COMPLETE,
        'test.failed': settings.WEBHOOK_ON_FAILURE,
        'test.cancelled': settings.WEBHOOK_ON_FAILURE,
        'analysis.completed': settings.WEBHOOK_ON_ANALYSIS,
    }[event]


def _key_metrics(summary: dict) -> dict:
    percentiles = summary.get('percentiles') if isinstance(summary.get('percentiles'), dict) else {}
    return {
        'total_requests': summary.get('total_requests'),
        'avg_response_time': summary.get('avg_response_time'),
        'p95_response_time': summary.get('p95_response_time') or percentiles.get('p95'),
        'p99_response_time': summary.get('p99_response_time') or percentiles.get('p99'),
        'error_rate': summary.get('error_rate'),
        'throughput': summary.get('throughput'),
    }


async def _requirements_verdict(execution) -> Optional[dict]:
    from app.services import requirements as requirement_service
    try:
        stored = await requirement_service.get_requirements(execution.project_id or 'default')
        result = requirement_service.evaluate(stored, execution.summary,
                                              requirement_service.sampler_routes(execution.jmx_path))
    except Exception as error:
        logger.warning('Requirement verdict unavailable for webhook of %s: %s', execution.id, error)
        return None
    return {'verdict': result.verdict, 'passed': result.passed, 'failed': result.failed, 'missing': result.missing}


async def build_payload(event: str, execution) -> dict:
    plan = store.get_test_plan(execution.test_plan_id) if execution.test_plan_id else None
    payload = {
        'event': event,
        'delivery_id': uuid.uuid4().hex,
        'timestamp': utc_now_naive().isoformat(),
        'execution_id': execution.id,
        'project_id': execution.project_id,
        'test_plan_id': execution.test_plan_id,
        'test_plan_name': plan.name if plan else '',
        'status': execution.status.value,
        'start_time': execution.start_time.isoformat() if execution.start_time else None,
        'end_time': execution.end_time.isoformat() if execution.end_time else None,
        'duration_seconds': (round((execution.end_time - execution.start_time).total_seconds(), 1)
                             if execution.start_time and execution.end_time else None),
    }
    if execution.summary:
        payload['summary'] = _key_metrics(execution.summary.model_dump())
        payload['requirements'] = await _requirements_verdict(execution)
    if execution.error_message:
        payload['error'] = execution.error_message
    if event == 'analysis.completed' and execution.ai_analysis:
        analysis = execution.ai_analysis
        payload['analysis'] = {
            'status': analysis.analysis_status,
            'overall_assessment': analysis.overall_assessment,
            'conclusion': analysis.conclusion,
        }
        if analysis.requirements_verdict:
            payload['requirements'] = analysis.requirements_verdict
    return payload


def sign(body: bytes, secret: str) -> str:
    return 'sha256=' + hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()


async def _deliver(event: str, execution):
    try:
        payload = await build_payload(event, execution)
    except Exception:
        logger.exception('Could not build %s webhook for %s', event, execution.id)
        return
    body = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'X-JMeter-Agent-Event': event,
               'X-JMeter-Agent-Delivery': payload['delivery_id']}
    if settings.WEBHOOK_SECRET:
        headers['X-JMeter-Agent-Signature'] = sign(body, settings.WEBHOOK_SECRET)
    problem = ''
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                logger.warning('Webhook %s for %s failed (%s); retry %d in %ss',
                               event, execution.id, problem, attempt, RETRY_DELAYS[attempt - 1])
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])
            try:
                response = await client.post(settings.WEBHOOK_URL, content=body, headers=headers)
            except httpx.HTTPError as error:
                problem = f'{type(error).__name__}: {error}'
                continue
            if response.status_code < 400:
                logger.info('Webhook fired: %s for %s', event, execution.id)
                return
            problem = f'HTTP {response.status_code}: {response.text[:200]}'
            if response.status_code < 500 and response.status_code not in (408, 429):
                break  # the receiver rejected it; resending the same body will not help
    logger.error('Webhook %s for %s not delivered: %s', event, execution.id, problem)


def _send(event: str, execution):
    if not _enabled(event):
        return
    task = asyncio.create_task(_deliver(event, execution))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _on_started(execution):
    _send('test.started', execution)


async def _on_finished(execution):
    _send(FINISHED_EVENTS[execution.status.value], execution)


async def _on_analysis(execution):
    _send('analysis.completed', execution)


LISTENERS = (('started', _on_started), ('finished', _on_finished), ('analysis_completed', _on_analysis))


def register(lifecycle):
    for event, handler in LISTENERS:
        lifecycle.on(event, handler)


def unregister(lifecycle):
    for event, handler in LISTENERS:
        lifecycle.off(event, handler)


async def drain(timeout: Optional[float] = None):
    """Wait for deliveries in flight; on shutdown, give up on the rest after ``timeout``."""
    if not _pending:
        return
    _, unfinished = await asyncio.wait(list(_pending), timeout=timeout)
    for task in unfinished:
        task.cancel()
    await asyncio.gather(*unfinished, return_exceptions=True)
