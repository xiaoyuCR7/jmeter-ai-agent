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
  "error": "...",                # first line only, local paths reduced to file names
  "analysis": {"status": "...", "conclusion": "..."}   # analysis.completed only; the full
                                 # assessment too with WEBHOOK_INCLUDE_ANALYSIS
}

Every request carries X-JMeter-Agent-Timestamp (Unix seconds). With WEBHOOK_SECRET set,
X-JMeter-Agent-Signature is "sha256=" followed by the hex HMAC-SHA256, keyed with the
secret, of "<timestamp>." + the raw request body. Receivers should compare it in constant
time, reject timestamps more than 5 minutes old and drop delivery IDs already seen.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from typing import Optional
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.core.time import utc_now_naive
from app.services.storage import store

logger = logging.getLogger(__name__)

FINISHED_EVENTS = {'completed': 'test.completed', 'failed': 'test.failed', 'cancelled': 'test.cancelled'}
# Seconds to wait before each retry; network errors, 408, 429 and 5xx are retried.
RETRY_DELAYS = (1.0, 5.0, 15.0)

ERROR_LIMIT = 300
CONCLUSION_LIMIT = 500
# Windows drive paths and POSIX absolute paths, so receivers do not learn the host's layout.
_LOCAL_PATH = re.compile(r'(?<!\w)[A-Za-z]:[\\/][^\s\'"<>|]*|(?<![\w.:/])/(?:[^\s/\'"<>|]+/)+[^\s/\'"<>|]*')
LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1'}

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


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + '…'


def safe_error(message: str) -> str:
    """First line of an error, local paths reduced to their file names."""
    lines = [line.strip() for line in str(message).splitlines() if line.strip()]
    first = lines[0] if lines else ''
    return _clip(_LOCAL_PATH.sub(lambda m: re.split(r'[\\/]', m.group(0).rstrip('\\/'))[-1], first), ERROR_LIMIT)


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
        payload['error'] = safe_error(execution.error_message)
    if event == 'analysis.completed' and execution.ai_analysis:
        analysis = execution.ai_analysis
        # Model-written text can quote endpoints and internals; send it in full only when asked.
        payload['analysis'] = {'status': analysis.analysis_status,
                               'conclusion': _clip(analysis.conclusion, CONCLUSION_LIMIT)}
        if settings.WEBHOOK_INCLUDE_ANALYSIS:
            payload['analysis']['overall_assessment'] = analysis.overall_assessment
        if analysis.requirements_verdict:
            payload['requirements'] = analysis.requirements_verdict
    return payload


def sign(timestamp: str, body: bytes, secret: str) -> str:
    message = timestamp.encode('ascii') + b'.' + body
    return 'sha256=' + hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()


def _host(url: str) -> str:
    """Webhook URLs often carry their token in the path or query; logs name only the host."""
    return urlsplit(url).hostname or 'webhook'


def _log_safe(text: str) -> str:
    return _clip(' '.join(text.split()), 200)


async def _post(client, url: str, body: bytes, headers: dict) -> tuple[int, str]:
    """POST and read at most 200 bytes of the reply; only the status matters."""
    async with client.stream('POST', url, content=body, headers=headers) as response:
        snippet = b''
        async for chunk in response.aiter_bytes():
            snippet += chunk
            if len(snippet) >= 200:
                break
        return response.status_code, snippet[:200].decode('utf-8', errors='replace')


async def _deliver(event: str, execution):
    try:
        payload = await build_payload(event, execution)
    except Exception:
        logger.exception('Could not build %s webhook for %s', event, execution.id)
        return
    body = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
    url, host = settings.WEBHOOK_URL, _host(settings.WEBHOOK_URL)
    problem = ''
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False) as client:
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                logger.warning('Webhook %s for %s to %s failed (%s); retry %d in %ss',
                               event, execution.id, host, problem, attempt, RETRY_DELAYS[attempt - 1])
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])
            # Signed afresh on every attempt, so a retry is never rejected as stale.
            timestamp = str(int(time.time()))
            headers = {'Content-Type': 'application/json', 'X-JMeter-Agent-Event': event,
                       'X-JMeter-Agent-Delivery': payload['delivery_id'], 'X-JMeter-Agent-Timestamp': timestamp}
            if settings.WEBHOOK_SECRET:
                headers['X-JMeter-Agent-Signature'] = sign(timestamp, body, settings.WEBHOOK_SECRET)
            try:
                status, reply = await _post(client, url, body, headers)
            except httpx.HTTPError as error:
                problem = type(error).__name__  # its message may repeat the URL and its token
                continue
            if 200 <= status < 300:
                logger.info('Webhook fired: %s for %s', event, execution.id)
                return
            if 300 <= status < 400:
                problem = f'HTTP {status}: the URL redirects; set WEBHOOK_URL to the final address'
                break
            problem = f'HTTP {status}: {_log_safe(reply)}'
            if status < 500 and status not in (408, 429):
                break  # the receiver rejected it; resending the same body will not help
    logger.error('Webhook %s for %s to %s not delivered: %s', event, execution.id, host, problem)


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


def config_warnings() -> list[str]:
    url = settings.WEBHOOK_URL
    if not url:
        return []
    warnings = []
    parts = urlsplit(url)
    host = parts.hostname or ''
    local = host in LOOPBACK_HOSTS or host.endswith('.localhost')
    if not settings.WEBHOOK_SECRET:
        warnings.append('WEBHOOK_SECRET is not set: receivers cannot verify that webhooks come from this app')
    if parts.scheme not in ('http', 'https'):
        warnings.append('WEBHOOK_URL is not an http(s) URL; deliveries will fail')
    elif parts.scheme == 'http' and not local:
        warnings.append(f'WEBHOOK_URL sends events to {host} over plain http; use https')
    return warnings


def register(lifecycle):
    for warning in config_warnings():
        logger.warning(warning)
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
