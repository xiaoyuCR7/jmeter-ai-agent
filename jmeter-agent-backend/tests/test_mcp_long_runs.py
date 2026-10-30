"""Long work over MCP: runs outlive their request, and work nobody waits for stops."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api import mcp_routes
from app.services.ai.agent.tools import jmeter
from app.services.mcp_server import MCPServer


def call(name, arguments=None, req_id=1):
    return {'jsonrpc': '2.0', 'id': req_id, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments or {}}}


def cancelled(request_id):
    return {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': request_id}}


@pytest.fixture
def server(monkeypatch):
    server = MCPServer()
    monkeypatch.setattr(mcp_routes, 'mcp_server', server)
    return server


def http_client():
    app = FastAPI()
    app.include_router(mcp_routes.router, prefix='/api/v1')
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')


async def until(condition):
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError('condition never became true')


@pytest.mark.asyncio
async def test_mcp_starts_runs_in_the_background_but_agents_wait_for_them(server, monkeypatch):
    from app.services.ai.agent.tools import get_all_builtin_tools
    server.register_all_agent_tools()
    started = SimpleNamespace(id='run-1', test_plan_id='plan', status=SimpleNamespace(value='pending'))
    submit = AsyncMock(return_value=started)
    monkeypatch.setattr(jmeter.execution_lifecycle, 'submit', submit)
    monkeypatch.setattr(server._tool_registry, 'source', 'mcp')
    from app.services.confirmations import confirmations
    monkeypatch.setattr(confirmations, 'ask', AsyncMock(return_value='approved'))
    response = await server.handle_request(json.dumps(call('execute_test', {'jmx_path': 'plan.jmx'})))
    result = json.loads(response['result']['content'][0]['text'])
    assert result['execution_id'] == 'run-1' and 'get_execution_status' in result['next']
    submit.assert_awaited_once()

    descriptions = {tool.name: tool for tool in get_all_builtin_tools()}
    assert descriptions['execute_test'].handler is jmeter.execute_test
    assert descriptions['execute_test'].long_running
    assert 'get_execution_status' in server._tools['execute_test'].description
    assert 'get_execution_status' in server._tools['execute_distributed'].description


def test_long_tools_are_not_cut_off_by_the_default_tool_limit():
    from app.core.config import settings
    from app.services.ai.agent.tools import get_all_builtin_tools
    tools = {tool.name: tool for tool in get_all_builtin_tools()}
    for name in ('execute_test', 'execute_distributed', 'selfheal_test', 'import_openapi_spec'):
        assert tools[name].timeout_seconds > settings.JMETER_TIMEOUT
    assert tools['read_file'].timeout_seconds == settings.AGENT_TOOL_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_status_tool_reports_a_run_and_a_missing_one(monkeypatch):
    from datetime import datetime
    from app.models.schemas import TestExecutionInfo, TestExecutionStatus
    from app.services.storage import store
    execution = TestExecutionInfo(id='run-1', project_id='p', test_plan_id='t', jmx_path='x.jmx',
                                  status=TestExecutionStatus.RUNNING, start_time=datetime(2026, 10, 30), progress=40)
    monkeypatch.setattr(store, 'get_execution', lambda execution_id: execution if execution_id == 'run-1' else None)
    status = await jmeter.get_execution_status('run-1')
    assert status['status'] == 'running' and status['progress'] == 40 and status['summary'] is None
    assert 'not found' in (await jmeter.get_execution_status('missing'))['error']


@pytest.mark.asyncio
async def test_a_cancel_notification_stops_a_streamable_request(server):
    stopped = asyncio.Event()
    async def slow():
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()
    server.register_tool('slow', 'slow', {}, slow)
    async with http_client() as http:
        headers = {'Mcp-Session-Id': 'client-a'}
        request = asyncio.create_task(http.post('/api/v1/mcp', json=call('slow'), headers=headers))
        await until(lambda: ('client-a', 1) in server._http_requests)
        # Another client's request 1 is someone else's.
        await http.post('/api/v1/mcp', json=cancelled(1), headers={'Mcp-Session-Id': 'client-b'})
        assert not stopped.is_set()
        assert (await http.post('/api/v1/mcp', json=cancelled(1), headers=headers)).status_code == 202
        response = await request
    assert stopped.is_set() and response.json()['error']['code'] == -32800
    assert server._http_requests == {}


@pytest.mark.asyncio
async def test_a_client_that_leaves_takes_its_request_with_it(server, monkeypatch):
    from app.services.confirmations import ConfirmationManager, confirmations
    server.register_tool('danger', 'danger', {}, AsyncMock(), risk='write')
    gone = asyncio.Event()
    async def receive():
        await gone.wait()
        return {'type': 'http.disconnect'}
    body = json.dumps(call('danger')).encode()
    messages = [{'type': 'http.request', 'body': body, 'more_body': False}]
    async def first_then_wait():
        return messages.pop(0) if messages else await receive()
    from starlette.requests import Request
    request = Request({'type': 'http', 'method': 'POST', 'path': '/api/v1/mcp', 'headers': [],
                       'query_string': b''}, first_then_wait)
    route = asyncio.create_task(mcp_routes.mcp_streamable_http(request))
    await until(confirmations.list_pending)
    gone.set()
    response = await route
    assert response.status_code == 204
    assert confirmations.list_pending() == []


@pytest.mark.asyncio
async def test_each_initialize_gets_a_session_id_and_bad_versions_are_refused(server):
    params = {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'c', 'version': '1'}}
    async with http_client() as http:
        first = await http.post('/api/v1/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': params})
        second = await http.post('/api/v1/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': params})
        assert first.headers['mcp-session-id'] != second.headers['mcp-session-id']
        ping = {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}
        assert 'mcp-session-id' not in (await http.post('/api/v1/mcp', json=ping)).headers
        assert (await http.post('/api/v1/mcp', json=ping, headers={'MCP-Protocol-Version': '2025-06-18'})).status_code == 200
        refused = await http.post('/api/v1/mcp', json=ping, headers={'MCP-Protocol-Version': '1999-01-01'})
        assert refused.status_code == 400


@pytest.mark.asyncio
async def test_prompts_need_their_required_arguments():
    server = MCPServer()
    server.register_all_prompts()
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'prompts/get', 'params': {'name': 'analyze-execution'}}
    assert (await server.handle_request(json.dumps(request)))['error']['code'] == -32602
    request['params']['arguments'] = {'execution_id': 'run-1'}
    assert 'run-1' in (await server.handle_request(json.dumps(request)))['result']['messages'][0]['content']['text']


@pytest.mark.asyncio
async def test_clients_are_told_about_approvals_and_background_runs():
    params = {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'c', 'version': '1'}}
    response = await MCPServer().handle_request(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': params}))
    instructions = response['result']['instructions']
    assert 'approve' in instructions and 'get_execution_status' in instructions


@pytest.mark.asyncio
async def test_status_lists_each_tools_risk_and_full_description(server):
    server.register_all_agent_tools()
    status = await mcp_routes.mcp_status()
    tools = {tool['name']: tool for tool in status['tools']}
    assert tools['delete_file']['risk'] == 'write' and tools['read_file']['risk'] == 'read'
    assert tools['execute_test']['description'].endswith('execution_id.')
