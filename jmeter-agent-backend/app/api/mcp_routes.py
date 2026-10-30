"""MCP transports.

POST /mcp is Streamable HTTP (JSON responses, no server-initiated stream), for
current clients such as Claude Code, Cursor and VS Code. The legacy 2024-11-05
HTTP+SSE transport stays available: GET /mcp/sse advertises a POST URI and
replies arrive only on that SSE connection.
"""

import json
import asyncio
import logging
import secrets
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, Response
import anyio
from fastapi.responses import StreamingResponse

from app.services.mcp_server import mcp_server, MCPTransportError, SUPPORTED_PROTOCOL_VERSIONS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mcp"])


class _MCPStreamingResponse(StreamingResponse):
    """Own the session even if ASGI send fails while the generator is suspended."""
    def __init__(self, content, server, session, **kwargs):
        self.server = server
        self.session = session
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                # The shared response closes its iterator. This additionally
                # owns sessions when headers fail before iteration ever starts.
                await self.server.close_session(self.session)


async def init_mcp_server():
    """Register all tools, resources, and prompts — called from main.py lifespan."""
    mcp_server.register_all_agent_tools()
    mcp_server.register_all_resources()
    mcp_server.register_all_prompts()
    logger.info(f"MCP Server initialized: {len(mcp_server._tools)} tools, "
                f"{len(mcp_server._resources)} resources, "
                f"{len(mcp_server._prompts)} prompts")


@router.get("/mcp/status")
async def mcp_status():
    """Get MCP server status — tools, resources, prompts count."""
    return {
        "server_name": mcp_server.name,
        "server_version": mcp_server.version,
        "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "transports": ["streamable-http", "sse"],
        "tools_count": len(mcp_server._tools),
        "resources_count": len(mcp_server._resources),
        "prompts_count": len(mcp_server._prompts),
        "tools": [
            {"name": t.name, "description": t.description, "risk": t.risk}
            for t in mcp_server._tools.values()
        ],
        "resources": [
            {"uri": r.uri, "name": r.name, "description": r.description}
            for r in mcp_server._resources.values()
        ],
        "prompts": [
            {"name": p.name, "description": p.description}
            for p in mcp_server._prompts.values()
        ],
        "endpoints": {
            "streamable_http": "/api/v1/mcp",
            "messages": "/api/v1/mcp/messages",
            "sse": "/api/v1/mcp/sse",
            "status": "/api/v1/mcp/status",
        },
    }


async def _client_left(request: Request):
    while not await request.is_disconnected():
        await asyncio.sleep(0.25)


@router.post("/mcp")
async def mcp_streamable_http(request: Request):
    """Streamable HTTP: one JSON-RPC message in, one JSON response out (202 for notifications)."""
    version = request.headers.get("mcp-protocol-version")
    if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise HTTPException(400, f"Unsupported MCP protocol version: {version}")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "MCP message exceeds 1 MiB")
    work = asyncio.create_task(mcp_server.handle_request(bytes(body), request.headers.get("mcp-session-id", "")))
    left = asyncio.create_task(_client_left(request))
    try:
        # Work nobody is waiting for must not continue, such as a confirmation left on the user's screen.
        await asyncio.wait({work, left}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        left.cancel()
        if not work.done():
            work.cancel()
    if work.cancelled() or not work.done():
        await asyncio.gather(work, return_exceptions=True)
        return Response(status_code=204)
    response = work.result()
    if response is None:
        return Response(status_code=202)
    # Stateless apart from telling clients apart: the ID only scopes cancellation, so it is not checked.
    initialized = "protocolVersion" in response.get("result", {})
    headers = {"Mcp-Session-Id": secrets.token_urlsafe(16)} if initialized else None
    return JSONResponse(response, headers=headers)


@router.get("/mcp")
async def mcp_streamable_http_stream():
    # No server-initiated messages: the spec lets servers decline the GET stream.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp/messages")
async def mcp_messages(request: Request):
    """Accept one session message. Missing sessions are not a custom HTTP transport."""
    session_id = request.query_params.get("session_id")
    if not session_id:
        raise HTTPException(400, "Connect to /api/v1/mcp/sse first")
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise HTTPException(413, "MCP message exceeds 1 MiB")
        session = mcp_server.get_session(session_id)
        mcp_server.submit(session, bytes(body))
    except MCPTransportError as error:
        raise HTTPException(error.status_code, error.detail) from error
    return Response(status_code=202)


@router.get("/mcp/sse")
async def mcp_sse(request: Request):
    """Each stream owns its queue and pending tasks; disconnect revokes its POST URI."""
    server = mcp_server
    try:
        # Admission must happen before response headers, so limits return HTTP 429.
        session = server.create_session()
    except MCPTransportError as error:
        raise HTTPException(error.status_code, error.detail) from error

    async def event_stream():
        try:
            root = request.scope.get("root_path", "").rstrip("/")
            endpoint = f"{root}/api/v1/mcp/messages?session_id={session.id}"
            yield f"event: endpoint\ndata: {endpoint}\n\n"
            last_heartbeat = asyncio.get_running_loop().time()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    response = await asyncio.wait_for(session.queue.get(), timeout=15.0)
                    yield f"event: message\ndata: {json.dumps(response, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    if asyncio.get_running_loop().time() - last_heartbeat >= 15:
                        last_heartbeat = asyncio.get_running_loop().time()
                        yield ": heartbeat\n\n"
        finally:
            # Starlette cancels streams on disconnect. Shield task cleanup from
            # its AnyIO cancellation scope, then wait for tool finally blocks.
            with anyio.CancelScope(shield=True):
                await server.close_session(session)

    return _MCPStreamingResponse(event_stream(), server, session, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
