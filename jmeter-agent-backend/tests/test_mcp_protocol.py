"""MCP 2024-11-05 contracts: isolated in-process sessions, no external tools."""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.services.mcp_server import MCPServer


def rpc(method='ping', req_id=1, params=None):
    body = {'jsonrpc': '2.0', 'id': req_id, 'method': method}
    if params is not None:
        body['params'] = params
    return json.dumps(body)


def request(method='GET', query='', body=''):
    async def receive():
        return {'type': 'http.request', 'body': body.encode(), 'more_body': False}
    return Request({'type': 'http', 'method': method, 'path': '/api/v1/mcp/sse',
                    'root_path': '', 'scheme': 'http', 'server': ('test', 80),
                    'query_string': query.encode(), 'headers': [(b'host', b'test')]}, receive)


@pytest.fixture
def server(monkeypatch):
    from app.api import mcp_routes
    server = MCPServer()
    monkeypatch.setattr(mcp_routes, 'mcp_server', server)
    return server


async def connect():
    from app.api.mcp_routes import mcp_sse
    response = await mcp_sse(request())
    iterator = response.body_iterator
    first = await anext(iterator)
    assert first.startswith('event: endpoint\n')
    endpoint = first.split('data: ', 1)[1].strip()
    query = urlsplit(endpoint).query
    assert parse_qs(query).get('session_id'), 'each endpoint must identify a private session'
    return iterator, query, parse_qs(query)['session_id'][0]


async def post(query, body):
    from app.api.mcp_routes import mcp_messages
    return await mcp_messages(request('POST', query, body))


async def receive(iterator):
    chunk = await asyncio.wait_for(anext(iterator), 1)
    assert chunk.startswith('event: message\n')
    return json.loads(chunk.split('data: ', 1)[1])


async def initialize(iterator, query):
    response = await post(query, rpc('initialize', 'init', {
        'protocolVersion': '2024-11-05', 'capabilities': {},
        'clientInfo': {'name': 'test-client', 'version': '1'},
    }))
    assert response.status_code == 202
    result = await receive(iterator)
    assert result['result']['protocolVersion'] == '2024-11-05'
    await post(query, json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [None, [], [1], 1, 'text', {}, {'method': 'ping'},
    {'jsonrpc': '1.0', 'method': 'ping', 'id': 1},
    *[{'jsonrpc': '2.0', 'method': 'ping', 'id': value} for value in [None, True, 1.5, [], {}]],
    {'jsonrpc': '2.0', 'method': 2, 'id': 1}])
async def test_invalid_envelopes_return_invalid_request(body):
    result = await MCPServer().handle_request(json.dumps(body))
    assert result['error']['code'] == -32600


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [None, [], 1, 'text'])
async def test_non_object_params_are_invalid_params(params):
    result = await MCPServer().handle_request(json.dumps({
        'jsonrpc': '2.0', 'id': 7, 'method': 'ping', 'params': params}))
    assert result == {'jsonrpc': '2.0', 'id': 7, 'error': {'code': -32602, 'message': 'Invalid params'}}


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['notifications/initialized', 'notifications/unknown', 'ping'])
async def test_notifications_never_receive_a_response(method):
    result = await MCPServer().handle_request(json.dumps({'jsonrpc': '2.0', 'method': method}))
    assert result is None


@pytest.mark.asyncio
async def test_parse_error_and_numeric_string_ids_are_preserved():
    server = MCPServer()
    assert (await server.handle_request('{'))['error']['code'] == -32700
    for identifier in [0, 1, '1', '']:
        assert (await server.handle_request(rpc(req_id=identifier)))['id'] == identifier


@pytest.mark.asyncio
async def test_sse_post_accepted_and_responses_are_private(server):
    a, aq, aid = await connect()
    b, bq, bid = await connect()
    try:
        assert aid != bid
        await initialize(a, aq)
        response = await post(aq, rpc('tools/list', 42))
        assert response.status_code == 202
        assert response.body == b''
        assert (await receive(a))['id'] == 42
        assert server._sessions[bid].queue.empty()
    finally:
        await a.aclose()
        await b.aclose()
    assert server._sessions == {}


@pytest.mark.asyncio
async def test_missing_session_is_rejected(server):
    stream, query, _ = await connect()
    try:
        for candidate, status in [('session_id=unknown', 404), ('', 400)]:
            with pytest.raises(HTTPException) as exc:
                await post(candidate, rpc())
            assert exc.value.status_code == status
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_sse_readiness_requires_initialize_then_initialized_notification(server):
    stream, query, _ = await connect()
    try:
        await post(query, json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        await post(query, rpc('tools/list', 1))
        assert (await receive(stream))['error']['code'] == -32002
        await post(query, rpc('initialize', 2, {'protocolVersion': '2024-11-05', 'capabilities': {},
                                            'clientInfo': {'name': 'test', 'version': '1'}}))
        assert 'result' in await receive(stream)
        await post(query, rpc('tools/list', 3))
        assert (await receive(stream))['error']['code'] == -32002
        await post(query, json.dumps({'jsonrpc': '2.0', 'method': 'initialized'}))
        await post(query, rpc('tools/list', 4))
        assert (await receive(stream))['error']['code'] == -32002
        await post(query, json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        await post(query, rpc('tools/list', 5))
        assert 'result' in await receive(stream)
        await post(query, rpc('initialize', 6, {}))
        assert 'error' in await receive(stream)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_duplicate_request_ids_never_execute_twice(server):
    handler = AsyncMock(return_value={'ok': True})
    server.register_tool('count', 'count', {}, handler)
    stream, query, _ = await connect()
    try:
        await initialize(stream, query)
        body = rpc('tools/call', 42, {'name': 'count', 'arguments': {}})
        await post(query, body)
        await receive(stream)
        with pytest.raises(HTTPException) as exc:
            await post(query, body)
        assert exc.value.status_code == 409
        handler.assert_awaited_once_with()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_disconnect_cancels_and_awaits_pending_tool(server):
    started, stopped = asyncio.Event(), asyncio.Event()
    async def slow():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            stopped.set()
    server.register_tool('slow', 'slow', {}, slow)
    stream, query, sid = await connect()
    await initialize(stream, query)
    await post(query, rpc('tools/call', 42, {'name': 'slow', 'arguments': {}}))
    await asyncio.wait_for(started.wait(), 1)
    session = server._sessions[sid]
    await stream.aclose()
    assert stopped.is_set()
    assert not session.pending
    assert not session.seen_ids
    assert sid not in server._sessions


@pytest.mark.asyncio
async def test_completed_request_history_and_pending_work_are_bounded(server, monkeypatch):
    stream, query, sid = await connect()
    try:
        await initialize(stream, query)
        session = server._sessions[sid]
        monkeypatch.setattr(server, 'MAX_SESSION_REQUESTS', 2)
        await post(query, rpc('ping', 1))
        await receive(stream)
        with pytest.raises(HTTPException) as exc:
            await post(query, rpc('ping', 2))
        assert exc.value.status_code == 429
        assert len(session.seen_ids) == 2
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_malformed_json_is_delivered_only_over_session_sse(server):
    stream, query, _ = await connect()
    try:
        response = await post(query, '{')
        assert response.status_code == 202 and response.body == b''
        assert (await receive(stream))['error']['code'] == -32700
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_queue_and_pending_limits_reject_before_dispatch(server, monkeypatch):
    started = asyncio.Event()
    async def slow():
        started.set()
        await asyncio.Event().wait()
    server.register_tool('slow', 'slow', {}, slow)
    stream, query, sid = await connect()
    try:
        await initialize(stream, query)
        monkeypatch.setattr(server, 'MAX_PENDING_REQUESTS', 1)
        await post(query, rpc('tools/call', 2, {'name': 'slow'}))
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(HTTPException) as exc:
            await post(query, rpc('ping', 3))
        assert exc.value.status_code == 429
        assert 3 not in server._sessions[sid].seen_ids
    finally:
        await stream.aclose()
    monkeypatch.setattr(server, 'MAX_PENDING_REQUESTS', 32)
    monkeypatch.setattr(server, 'RESPONSE_QUEUE_SIZE', 2)
    stream, query, sid = await connect()
    try:
        await initialize(stream, query)
        await post(query, rpc('ping', 2))
        await post(query, rpc('ping', 3))
        with pytest.raises(HTTPException) as exc:
            await post(query, rpc('ping', 4))
        assert exc.value.status_code == 429
        await receive(stream)
        await receive(stream)
        assert not server._sessions[sid].pending
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_invalid_initialized_notification_does_not_execute_tools(server):
    handler = AsyncMock(return_value={})
    server.register_tool('test', 'test', {}, handler)
    stream, query, _ = await connect()
    try:
        await post(query, json.dumps({'jsonrpc': '2.0', 'method': 'tools/call',
                                     'params': {'name': 'test', 'arguments': {}}}))
        await post(query, rpc('initialize', 1, {}))
        assert (await receive(stream))['error']['code'] == -32602
        handler.assert_not_awaited()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_session_cancellation_removes_task_even_before_first_run(server):
    stream, query, sid = await connect()
    try:
        await initialize(stream, query)
        await post(query, rpc('ping', 1))
        await post(query, json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                     'params': {'requestId': 1}}))
        for _ in range(5):
            await asyncio.sleep(0)
        assert server._sessions[sid].pending == {}
        assert server._sessions[sid].queue.empty()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_sse_capacity_is_http_429_before_stream_headers(server, monkeypatch):
    from app.api.mcp_routes import mcp_sse
    monkeypatch.setattr(server, 'MAX_OWNER_SESSIONS', 1)
    stream, _, _ = await connect()
    try:
        with pytest.raises(HTTPException) as exc:
            await mcp_sse(request())
        assert exc.value.status_code == 429
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_on', ['http.response.start', 'http.response.body'])
async def test_failed_endpoint_send_closes_session_without_gc(server, fail_on):
    from app.api.mcp_routes import mcp_sse
    response = await mcp_sse(request())
    async def wait_disconnect():
        await asyncio.Event().wait()
    async def broken_send(message):
        if message['type'] == fail_on:
            raise OSError('client left during endpoint send')
    with pytest.raises((OSError, ExceptionGroup)):
        await response(request().scope, wait_disconnect, broken_send)
    assert not server._sessions


@pytest.mark.asyncio
async def test_request_timeout_cancels_work_and_delivers_error(server, monkeypatch):
    stopped = asyncio.Event()
    async def slow():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    server.register_tool('slow', 'slow', {}, slow)
    stream, query, sid = await connect()
    try:
        await initialize(stream, query)
        monkeypatch.setattr(server, 'REQUEST_TIMEOUT_SECONDS', 0.01)
        await post(query, rpc('tools/call', 2, {'name': 'slow'}))
        result = await receive(stream)
        assert result['error']['message'] == 'Request timed out'
        assert stopped.is_set()
        assert not server._sessions[sid].pending
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_oversized_message_is_rejected_without_dispatch(server):
    stream, query, sid = await connect()
    try:
        with pytest.raises(HTTPException) as exc:
            await post(query, ' ' * (1024 * 1024 + 1))
        assert exc.value.status_code == 413
        assert not server._sessions[sid].pending
        assert server._sessions[sid].queue.empty()
    finally:
        await stream.aclose()
