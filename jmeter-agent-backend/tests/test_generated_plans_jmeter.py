"""Generated plans against real JMeter and a local server: what they send, not what their XML says.

Starts JMeter, so it only runs on request:
    RUN_JMETER_TESTS=1 pytest tests/test_generated_plans_jmeter.py
Every bug here passed XML-level tests: a plan JMeter refused, assertions that checked nothing.
"""
import csv
import json
import os
import subprocess
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.models.schemas import APIEndpoint, ExtractionRule, ResponseCheck, TestConfig as LoadConfig, VariableBinding
from app.services.jmeter import jmx_lint
from app.services.jmeter.jmx_generator import JMXGenerator

pytestmark = pytest.mark.skipif(not os.environ.get('RUN_JMETER_TESTS'), reason='set RUN_JMETER_TESTS=1 to start JMeter')

JMETER = Path(__file__).resolve().parents[2] / 'bin' / ('jmeter.bat' if os.name == 'nt' else 'jmeter')


@pytest.fixture
def service():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode()
            path, auth = self.path.split('?')[0], self.headers.get('Authorization')
            seen.append((self.command, path, auth, raw))
            if path == '/login':
                return self._reply(200, {'code': 0, 'token': 'tok'})
            if path == '/public':
                return self._reply(401 if auth else 200, {'code': 0})
            if path == '/broken':
                return self._reply(200, {'code': 500})
            if auth != 'Bearer tok':
                return self._reply(401, {'code': 401})
            if self.command == 'POST':
                return self._reply(201, {'code': 0, 'id': 101})
            return self._reply(200 if path in ('/orders', '/orders/101') else 404, {'code': 0})

        do_GET = do_POST = _handle

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}', seen
    server.shutdown()


def run_plan(plan, folder):
    path, results = folder / 'plan.jmx', folder / 'out.jtl'
    path.write_text(plan, encoding='utf-8')
    subprocess.run([str(JMETER), '-n', '-t', str(path), '-l', str(results), '-j', str(folder / 'jmeter.log')],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    assert results.exists(), (folder / 'jmeter.log').read_text(encoding='utf-8', errors='replace')[-800:]
    with results.open(encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


BEARER = VariableBinding(variable_name='token', target_location='header', target_key='Authorization',
                         template='Bearer ${token}')
FLOW = [
    APIEndpoint(name='Login', method='POST', path='/login', body={'u': 'a'}, phase='once_per_user',
                extractions=[ExtractionRule(ref_name='token', extract_type='json_path', expression='$.token')]),
    APIEndpoint(name='Public', method='GET', path='/public'),
    APIEndpoint(name='Create', method='POST', path='/orders', body=[{'sku': 'A'}], expected_status=201, bindings=[BEARER],
                depends_on=['Login'], response_checks=[ResponseCheck(json_path='$.code', expected='0')],
                extractions=[ExtractionRule(ref_name='order_id', extract_type='json_path', expression='$.id')]),
    APIEndpoint(name='Read', method='GET', path='/orders/{id}', depends_on=['Create'], bindings=[
        BEARER, VariableBinding(variable_name='order_id', target_location='path', target_key='id', template='${order_id}')]),
    APIEndpoint(name='Rare', method='GET', path='/orders', weight=25, depends_on=['Login'], bindings=[BEARER]),
]


def test_a_full_flow_runs_clean(service, tmp_path):
    url, seen = service
    plan = JMXGenerator().generate_basic_jmx(FLOW, LoadConfig(
        test_type='baseline', concurrent_users=2, ramp_up_seconds=1, duration_seconds=4, think_time_ms=20), url)
    assert jmx_lint.lint(plan) == []
    rows = run_plan(plan, tmp_path)
    assert [row for row in rows if row['success'] != 'true'] == []
    counts = Counter(row['label'] for row in rows)
    assert counts['Login'] == 2  # Once per user, however long the run.
    assert 0 < counts['Rare'] < counts['Read'] * 0.5
    assert not any(auth for method, path, auth, _ in seen if path in ('/login', '/public'))


@pytest.mark.parametrize('endpoint,failure', [
    (APIEndpoint(name='x', method='GET', path='/login', expected_status=201), 'Expected status 201'),
    (APIEndpoint(name='x', method='GET', path='/broken', response_checks=[ResponseCheck(json_path='$.code', expected='0')]), 'code'),
])
def test_assertions_really_fail_requests(service, tmp_path, endpoint, failure):
    url, _ = service
    plan = JMXGenerator().generate_basic_jmx([endpoint], LoadConfig(
        test_type='custom', concurrent_users=1, ramp_up_seconds=0, duration_seconds=0, iterations=1), url)
    row = run_plan(plan, tmp_path)[0]
    assert row['success'] == 'false' and failure in row['failureMessage']


def test_an_expected_error_status_passes(service, tmp_path):
    url, _ = service
    plan = JMXGenerator().generate_basic_jmx([APIEndpoint(name='x', method='GET', path='/private', expected_status=401)],
                                             LoadConfig(test_type='custom', concurrent_users=1, ramp_up_seconds=0,
                                                        duration_seconds=0, iterations=1), url)
    assert run_plan(plan, tmp_path)[0]['success'] == 'true'


def test_steps_add_threads_over_time(service, tmp_path):
    url, _ = service
    plan = JMXGenerator().generate_basic_jmx([APIEndpoint(name='x', method='GET', path='/public')], LoadConfig(
        test_type='ramp_up', concurrent_users=4, step_users=2, step_duration=3, think_time_ms=50), url)
    rows = run_plan(plan, tmp_path)
    first = min(int(row['timeStamp']) for row in rows)
    early = max(int(row['allThreads']) for row in rows if int(row['timeStamp']) - first < 2000)
    assert early == 2 and max(int(row['allThreads']) for row in rows) == 4


def test_a_failed_login_stops_that_user_instead_of_failing_everything_after_it(service, tmp_path):
    url, seen = service
    endpoints = [APIEndpoint(name='Login', method='GET', path='/private', phase='once_per_user'),
                 APIEndpoint(name='Next', method='GET', path='/public')]
    plan = JMXGenerator().generate_basic_jmx(endpoints, LoadConfig(
        test_type='baseline', concurrent_users=2, ramp_up_seconds=0, duration_seconds=2), url)
    assert Counter(row['label'] for row in run_plan(plan, tmp_path)) == {'Login': 2}
    assert {path for _, path, _, _ in seen} == {'/private'}


def test_setup_runs_once_before_the_load(service, tmp_path):
    url, seen = service
    endpoints = [APIEndpoint(name='Seed', method='POST', path='/login', phase='setup'),
                 APIEndpoint(name='Public', method='GET', path='/public')]
    plan = JMXGenerator().generate_basic_jmx(endpoints, LoadConfig(
        test_type='baseline', concurrent_users=2, ramp_up_seconds=0, duration_seconds=2, think_time_ms=50), url)
    rows = run_plan(plan, tmp_path)
    assert Counter(row['label'] for row in rows)['Seed'] == 1 and seen[0][1] == '/login'


def test_load_check_tells_jmeter_refusal_from_a_good_plan(service, monkeypatch):
    from app.services.jmeter import executor
    url, seen = service
    monkeypatch.setattr(executor, 'get_executor', lambda: type('E', (), {'jmeter_path': str(JMETER)})())
    import importlib
    check = importlib.reload(jmx_lint).load_check  # conftest replaces it for every other test
    plan = JMXGenerator().generate_basic_jmx([APIEndpoint(name='x', method='GET', path='/public')],
                                             LoadConfig(test_type='baseline', concurrent_users=5, duration_seconds=60), url)
    assert check(plan) == {'checked': True, 'ok': True, 'error': None}
    assert seen == []  # Nothing reached the target.
    refused = check(plan.replace('</CookieManager>', '</CookieManager><hashTree/>', 1).replace('<CacheManager', '<NoSuchElement', 1)
                    .replace('</CacheManager>', '</NoSuchElement>', 1))
    assert refused['checked'] and refused['ok'] is False
