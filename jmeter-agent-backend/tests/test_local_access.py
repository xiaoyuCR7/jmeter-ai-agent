"""The access token stands between the API and anything local that is not the user."""
import httpx
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core import local_access
from app.middleware.local_guard import (
    LocalAccessMiddleware, OriginGuardMiddleware, ReportSandboxMiddleware, TrustedHostMiddleware,
)


def guarded_app():
    from app.api.routes import create_session, session_token
    app = FastAPI()
    app.add_api_route('/api/v1/session', create_session, methods=['POST'])
    app.add_api_route('/api/v1/session/token', session_token)
    app.add_api_route('/api/v1/confirmations/{item}', lambda item: {'ok': True}, methods=['POST'])
    for path in ('/health', '/assets/app.js', '/reports/run/result.jtl', '/reports/run/html/index.html',
                 '/reports/run/html/content/js/dashboard.js'):
        app.add_api_route(path, lambda: {'ok': True})

    @app.websocket('/ws/executions/{item}')
    async def live(websocket: WebSocket, item: str):
        await websocket.accept()
        await websocket.close()
    app.add_middleware(LocalAccessMiddleware)
    app.add_middleware(ReportSandboxMiddleware)
    app.add_middleware(OriginGuardMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1'])
    return app


def client(app=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or guarded_app()), base_url='http://127.0.0.1:8000')


@pytest.mark.asyncio
async def test_a_local_program_without_the_token_cannot_drive_the_api():
    """What a test plan's HTTP sampler looks like: loopback, right Host, no Origin."""
    async with client() as http:
        response = await http.post('/api/v1/confirmations/x')
        assert response.status_code == 401 and response.json()['code'] == 'local_access_required'
        assert (await http.post('/api/v1/confirmations/x', headers={'Authorization': 'Bearer wrong'})).status_code == 401
        assert (await http.get('/reports/run/result.jtl')).status_code == 401
        assert (await http.get('/api/v1/session/token')).status_code == 401


@pytest.mark.asyncio
async def test_the_ui_shell_and_health_stay_public():
    async with client() as http:
        assert (await http.get('/health')).status_code == 200
        assert (await http.get('/assets/app.js')).status_code == 200


@pytest.mark.asyncio
async def test_the_token_works_as_a_bearer_for_mcp_and_scripts():
    async with client() as http:
        headers = {'Authorization': f'Bearer {local_access.get_token()}'}
        assert (await http.post('/api/v1/confirmations/x', headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_the_start_link_trades_the_token_for_a_cookie_scripts_cannot_read():
    async with client() as http:
        assert (await http.post('/api/v1/session', json={'token': 'wrong'})).status_code == 401
        assert (await http.post('/api/v1/session', json={'token': 7})).status_code == 401
        response = await http.post('/api/v1/session', json={'token': local_access.get_token()})
        assert response.status_code == 200
        cookie = response.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie
        assert (await http.post('/api/v1/confirmations/x')).status_code == 200
        assert (await http.get('/reports/run/result.jtl')).status_code == 200


def test_websockets_need_the_cookie_too(monkeypatch):
    monkeypatch.setattr(local_access, 'is_loopback_client', lambda scope: True)  # TestClient's peer is not an address
    with TestClient(guarded_app(), base_url='http://127.0.0.1:8000') as http:
        url = 'ws://127.0.0.1:8000/ws/executions/x'
        with pytest.raises(WebSocketDisconnect):
            with http.websocket_connect(url):
                pass
        cookie = f'{local_access.COOKIE_NAME}={local_access.get_token()}'
        with http.websocket_connect(url, headers={'Cookie': cookie}):
            pass


@pytest.mark.asyncio
async def test_reports_are_sandboxed_and_only_the_dashboard_is_open():
    """The sandboxed dashboard cannot send the cookie for its own scripts, so only it stays open."""
    async with client() as http:
        page = await http.get('/reports/run/html/index.html')
        assert page.status_code == 200
        assert 'sandbox' in page.headers['content-security-policy']
        assert page.headers['x-content-type-options'] == 'nosniff'
        assert (await http.get('/reports/run/html/content/js/dashboard.js')).status_code == 200
        headers = {'Authorization': f'Bearer {local_access.get_token()}'}
        assert 'sandbox' in (await http.get('/reports/run/result.jtl', headers=headers)).headers['content-security-policy']


@pytest.mark.parametrize('client_address, allowed', [
    (('127.0.0.1', 1), True), (('::1', 1), True), (('::ffff:127.0.0.1', 1), True), (None, True),
    (('192.168.1.20', 1), False), (('::ffff:10.0.0.2', 1), False), (('not-an-ip', 1), False),
])
def test_only_this_machine_counts_as_local(client_address, allowed):
    assert local_access.is_loopback_client({'client': client_address}) is allowed


@pytest.mark.asyncio
async def test_a_forged_host_header_does_not_let_another_machine_in():
    transport = httpx.ASGITransport(app=guarded_app(), client=('192.168.1.20', 5000))
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8000') as http:
        assert (await http.get('/health')).status_code == 403


def test_the_token_is_stable_and_outside_what_agent_tools_can_read(monkeypatch):
    from app.core.config import safe_path
    first = local_access.get_token()
    assert local_access.get_token() == first and len(first) >= 40
    assert local_access.access_url('http://x').endswith(f'#token={first}')
    with pytest.raises(ValueError, match='Access denied'):
        safe_path(local_access.token_path())
