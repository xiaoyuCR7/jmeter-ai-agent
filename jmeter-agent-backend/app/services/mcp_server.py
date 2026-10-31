"""MCP (Model Context Protocol) Server — JSON-RPC 2.0 over HTTP/SSE.

Exposes all agent tools as MCP tools, plus resources and prompt templates.
Enables Claude Code, Cursor, Copilot, and other MCP clients to orchestrate
the JMeter Agent as a tool in their workflow.

Spec: https://spec.modelcontextprotocol.io/
"""

import json
import asyncio
import logging
import secrets
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Tool / Resource / Prompt Definitions ──────────────────

@dataclass
class MCPTool:
    name: str
    description: str
    inputSchema: dict
    risk: str = 'read'


@dataclass
class MCPResource:
    uri: str
    name: str
    description: str
    mimeType: str = "application/json"


@dataclass
class MCPPrompt:
    name: str
    description: str
    arguments: list[dict] = field(default_factory=list)


@dataclass
class MCPSession:
    """One legacy SSE connection; never a business-event subscriber."""
    id: str
    queue: asyncio.Queue
    phase: str = "new"
    pending: dict = field(default_factory=dict)
    seen_ids: set = field(default_factory=set)
    closed: bool = False
    server: Any = None


# Newest first; an unknown client version gets the newest one we speak.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class MCPTransportError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class MCPServer:
    """MCP Protocol server — wraps agent tools into MCP-compatible interface.

    Usage:
        server = MCPServer()
        server.register_all_agent_tools()
        # app/api/mcp_routes.py carries the requests
    """

    MAX_SESSIONS = 4
    MAX_PENDING_REQUESTS = 32
    MAX_SESSION_REQUESTS = 4096
    RESPONSE_QUEUE_SIZE = 64
    REQUEST_TIMEOUT_SECONDS = 300

    def __init__(self, name: str = "jmeter-agent", version: str = "1.0.0"):
        self.name = name
        self.version = version
        self._tools: Dict[str, MCPTool] = {}
        from app.services.ai.agent.tool_registry import ToolRegistry
        self._tool_registry = ToolRegistry(source='mcp')
        self._resources: Dict[str, MCPResource] = {}
        self._prompts: Dict[str, MCPPrompt] = {}
        self._sessions: Dict[str, MCPSession] = {}
        self._http_requests: Dict[tuple, asyncio.Task] = {}

    # ── Registration ────────────────────────────────────

    def register_tool(self, name: str, description: str, schema: dict, handler: Callable, risk='read',
                      long_running=False):
        from app.services.ai.agent.tool_registry import Tool
        if name in self._tools:
            return
        definition = Tool(name, description, schema, handler, risk=risk, long_running=long_running)
        self._tool_registry.register(definition)
        self._tools[name] = MCPTool(
            name=name, description=description, inputSchema=definition.parameters, risk=risk,
        )

    def register_resource(self, uri: str, name: str, description: str, mime: str = "application/json"):
        self._resources[uri] = MCPResource(
            uri=uri, name=name, description=description, mimeType=mime,
        )

    def register_prompt(self, name: str, description: str, arguments: list[dict] = None):
        self._prompts[name] = MCPPrompt(
            name=name, description=description, arguments=arguments or [],
        )

    def register_all_agent_tools(self):
        """Register every built-in agent tool as an MCP tool."""
        from app.services.ai.agent.tools import get_all_builtin_tools
        from app.services.ai.agent.tools.jmeter import background_tools
        started_in_background = {tool.name: tool for tool in background_tools}
        for tool in get_all_builtin_tools():
            tool = started_in_background.get(tool.name, tool)
            self.register_tool(
                name=tool.name,
                description=tool.description,
                schema=tool.parameters if hasattr(tool, 'parameters') else {},
                handler=tool.handler,
                risk=tool.risk,
                long_running=tool.long_running,
            )

    def register_all_resources(self):
        """Register projects, test plans, executions, baselines as MCP resources."""
        # Dynamic resources — list will query actual store at read time
        self.register_resource(
            "resource://projects", "Projects",
            "All test projects with environment configs",
        )
        self.register_resource(
            "resource://test-plans", "Test Plans",
            "All JMX test plans with configurations",
        )
        self.register_resource(
            "resource://executions", "Executions",
            "Test execution history with results",
        )
        self.register_resource(
            "resource://baselines", "Performance Baselines",
            "Baseline metrics for regression detection",
        )
        self.register_resource(
            "resource://reports", "Test Reports",
            "Parsed performance test reports",
        )

    def register_all_prompts(self):
        """Register pre-built prompt templates for common workflows."""
        self.register_prompt(
            "workflow-from-api-doc",
            "Full performance test workflow from API documentation",
            [
                {"name": "api_doc", "description": "API documentation (OpenAPI/Swagger or free text)", "required": True},
                {"name": "test_types", "description": "Test types: baseline, concurrency, stress, etc.", "required": False},
                {"name": "execute", "description": "Whether to automatically execute tests", "required": False},
            ],
        )
        self.register_prompt(
            "requirements-to-plan",
            "Turn a test document into reviewed performance requirements and a matching test plan",
            [
                {"name": "project_id", "description": "Target project", "required": True},
                {"name": "document", "description": "Requirement / test plan document text", "required": True},
            ],
        )
        self.register_prompt(
            "analyze-execution",
            "Evidence-based analysis of a finished run against requirements, baseline and server metrics",
            [
                {"name": "execution_id", "description": "Finished execution to analyse", "required": True},
            ],
        )
    # ── JSON-RPC Request Handling ────────────────────────

    def _parse_request(self, raw_body):
        """MCP envelopes are objects, with object params and non-null integer/string IDs."""
        try:
            def reject_constant(value):
                raise ValueError(value)
            body = json.loads(raw_body, parse_constant=reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None, self._error_response(None, -32700, "Parse error")
        if (not isinstance(body, dict) or body.get("jsonrpc") != "2.0"
                or not isinstance(body.get("method"), str) or not body["method"]
                or ("id" in body and type(body["id"]) not in (str, int))
                or "result" in body or "error" in body):
            return None, self._error_response(None, -32600, "Invalid Request")
        if "params" in body and not isinstance(body["params"], dict):
            error = self._error_response(body.get("id"), -32602, "Invalid params")
            return None, error if "id" in body else None
        return body, None

    async def handle_request(self, raw_body: str, http_session: str = "") -> Optional[dict]:
        """Dispatch one JSON-RPC message (Streamable HTTP, and the internal path for tests).

        ``http_session`` is the client's Mcp-Session-Id, which keeps two clients' request IDs apart.
        """
        body, error = self._parse_request(raw_body)
        if body is None:
            return error
        if "id" not in body:
            # Notifications cannot invoke tools or create request responses.
            if body["method"] == "notifications/cancelled":
                request_id = body.get("params", {}).get("requestId")
                task = self._http_requests.get((http_session, request_id)) if type(request_id) in (str, int) else None
                if task:
                    task.cancel()
            return None
        key = (http_session, body["id"])
        if key in self._http_requests:
            return self._error_response(body["id"], -32600, "Request ID already in progress")
        task = asyncio.create_task(self._execute_request(body))
        self._http_requests[key] = task
        task.add_done_callback(lambda finished: self._http_requests.pop(key, None))
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the caller left; awaiting the task has already cancelled it
            return self._error_response(body["id"], -32800, "Request cancelled")

    async def _execute_request(self, body: dict) -> dict:
        req_id, method = body["id"], body["method"]
        try:
            result = await self._dispatch(method, body.get("params", {}), req_id)
            return self._success_response(req_id, result)
        except MCPError as e:
            return self._error_response(req_id, e.code, e.message)
        except Exception:
            logger.exception("MCP dispatch error: %s", method)
            return self._error_response(req_id, -32603, "Internal error")

    def create_session(self) -> MCPSession:
        if len(self._sessions) >= self.MAX_SESSIONS:
            raise MCPTransportError(429, "Too many MCP sessions")
        session = MCPSession(secrets.token_urlsafe(32), asyncio.Queue(self.RESPONSE_QUEUE_SIZE), server=self)
        self._sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> MCPSession:
        session = self._sessions.get(session_id)
        if not session or session.closed:
            raise MCPTransportError(404, "MCP session not found")
        return session

    async def close_session(self, session: MCPSession):
        session.closed = True
        self._sessions.pop(session.id, None)
        tasks = list(session.pending.values())
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            session.pending.clear()
            session.seen_ids.clear()
            while not session.queue.empty():
                session.queue.get_nowait()

    def submit(self, session: MCPSession, raw_body):
        """Accept work without awaiting a tool. Only the owning SSE stream gets replies."""
        if session.closed:
            raise MCPTransportError(404, "MCP session not found")
        body, error = self._parse_request(raw_body)
        if body is not None and "id" not in body:
            method, params = body["method"], body.get("params", {})
            if method == "notifications/initialized" and session.phase == "initialized":
                session.phase = "ready"
            elif method == "notifications/cancelled":
                req_id = params.get("requestId")
                if type(req_id) in (str, int) and session.phase != "initializing":
                    task = session.pending.get(req_id)
                    if task:
                        task.cancel()
            return
        if body is None and error is None:
            return
        # Reserve a response slot for every pending request, so no result is dropped.
        if (session.queue.qsize() + len(session.pending) >= self.RESPONSE_QUEUE_SIZE
                or len(session.pending) >= self.MAX_PENDING_REQUESTS):
            raise MCPTransportError(429, "MCP session busy; consume responses before retrying")
        if body is None:
            session.queue.put_nowait(error)
            return
        req_id, method = body["id"], body["method"]
        if req_id in session.seen_ids:
            raise MCPTransportError(409, "Request ID already used in this MCP session")
        if len(session.seen_ids) >= self.MAX_SESSION_REQUESTS:
            raise MCPTransportError(429, "MCP session request limit reached; reconnect")
        session.seen_ids.add(req_id)
        if method == "initialize":
            if session.phase != "new":
                session.queue.put_nowait(self._error_response(req_id, -32600, "Already initialized"))
                return
            session.phase = "initializing"
        elif method != "ping" and session.phase != "ready":
            session.queue.put_nowait(self._error_response(req_id, -32002, "Session not initialized"))
            return

        async def run():
            try:
                try:
                    result = await asyncio.wait_for(self._execute_request(body), self._request_timeout(body))
                except asyncio.TimeoutError:
                    result = self._error_response(req_id, -32000, "Request timed out")
                if not session.closed:
                    session.queue.put_nowait(result)
                    if method == "initialize":
                        session.phase = "initialized" if "result" in result else "new"
            finally:
                session.pending.pop(req_id, None)

        task = asyncio.create_task(run())
        session.pending[req_id] = task
        # A task cancelled before its coroutine starts never enters run's finally.
        task.add_done_callback(lambda finished: session.pending.pop(req_id, None))

    def _request_timeout(self, body: dict) -> float:
        """A request is bounded, but never shorter than the work its tool is known to do."""
        name = body.get("params", {}).get("name") if body["method"] == "tools/call" else None
        tool = self._tool_registry.get(name) if isinstance(name, str) else None
        return tool.timeout_seconds if tool and tool.long_running else self.REQUEST_TIMEOUT_SECONDS

    async def _dispatch(self, method: str, params: dict, req_id: str) -> Any:
        """Route JSON-RPC method to handler."""
        handlers = {
            "initialize": self._handle_initialize,
            "ping": self._handle_ping,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "resources/list": self._handle_resources_list,
            "resources/read": self._handle_resources_read,
            "prompts/list": self._handle_prompts_list,
            "prompts/get": self._handle_prompts_get,
        }
        handler = handlers.get(method)
        if not handler:
            raise MCPError(-32601, f"Method not found: {method}")
        return await handler(params)

    # ── Lifecycle Handlers ───────────────────────────────

    async def _handle_initialize(self, params: dict) -> dict:
        client_info = params.get("clientInfo")
        if (not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(client_info, dict)
                or not isinstance(client_info.get("name"), str)
                or not isinstance(client_info.get("version"), str)):
            raise MCPError(-32602, "Invalid initialize params")

        requested = params["protocolVersion"]
        return {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0],
            "serverInfo": {
                "name": self.name,
                "version": self.version,
            },
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "instructions": (
                "JMeter Agent MCP Server: requirement-driven performance testing.\n"
                "Workflow: extract/save performance requirements from a test document, generate and validate a "
                "JMeter plan from them, execute it, then call get_execution_evidence before analysing: it returns "
                "per-requirement verdicts, baseline/previous-run comparison and Prometheus server metrics.\n"
                "execute_test and execute_distributed return once the run has started; poll "
                "get_execution_status until it finishes.\n"
                "Tools that change or run something wait for the user to approve them in the JMeter Agent "
                "window, for up to two minutes. Tell the user to keep that window open.\n"
                "Never invent numbers that are not in the evidence."
            ),
        }

    async def _handle_ping(self, params: dict) -> dict:
        return {}

    # ── Tools ────────────────────────────────────────────

    async def _handle_tools_list(self, params: dict) -> dict:
        tools = []
        for name, tool in self._tools.items():
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema or {
                    "type": "object",
                    "properties": {},
                },
                "annotations": {
                    "readOnlyHint": tool.risk == 'read',
                    "destructiveHint": tool.risk in ('write', 'execute'),
                },
            })
        return {"tools": tools}

    async def _handle_tools_call(self, params: dict) -> dict:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if not isinstance(tool_name, str):
            raise MCPError(-32602, "Invalid tool name")
        tool = self._tools.get(tool_name)
        if not tool:
            raise MCPError(-32602, f"Unknown tool: {tool_name}")

        # Reported in the result, not as a protocol error, so the model sees it and can correct itself.
        argument_error = self._tool_registry.validate_arguments(tool_name, arguments)
        if argument_error:
            return {"content": [{"type": "text", "text": argument_error}], "isError": True}

        outcome = await self._tool_registry.execute(tool_name, arguments)
        result = outcome.data if outcome.success else {'error': outcome.error, 'data': outcome.data}
        content = json.dumps(result, ensure_ascii=False, default=str) if isinstance(result, dict) else str(result)
        return {"content": [{"type": "text", "text": content}], "isError": not outcome.success}

    # ── Resources ────────────────────────────────────────

    async def _handle_resources_list(self, params: dict) -> dict:
        resources = []
        for uri, res in self._resources.items():
            resources.append({
                "uri": res.uri,
                "name": res.name,
                "description": res.description,
                "mimeType": res.mimeType,
            })
        return {"resources": resources}

    async def _handle_resources_read(self, params: dict) -> dict:
        uri = params.get("uri", "")
        if not isinstance(uri, str):
            raise MCPError(-32602, "Invalid resource URI")

        # Resolve dynamic resources
        if uri == "resource://projects":
            from app.services.db_store import db_store
            result = await db_store.list_projects(limit=200)
            data = json.dumps(result, ensure_ascii=False, default=str)
        elif uri == "resource://test-plans":
            from app.services.db_store import db_store
            result = await db_store.list_test_plans(limit=200)
            data = json.dumps(result, ensure_ascii=False, default=str)
        elif uri == "resource://executions":
            from app.services.db_store import db_store
            result = await db_store.list_executions(limit=200)
            data = json.dumps(result, ensure_ascii=False, default=str)
        elif uri == "resource://baselines":
            from app.services.db_store import db_store
            result = await db_store.list_baselines()
            data = json.dumps(result, ensure_ascii=False, default=str)
        elif uri == "resource://reports":
            from app.services.ai.agent.tools.jmeter import list_report_dir
            result = await list_report_dir()
            data = json.dumps(result, ensure_ascii=False, default=str)
        elif uri.startswith("resource://") and self._resources.get(uri):
            data = json.dumps({"uri": uri, "name": self._resources[uri].name})
        else:
            raise MCPError(-32602, f"Unknown resource: {uri}")

        return {
            "contents": [
                {"uri": uri, "mimeType": "application/json", "text": data}
            ]
        }

    # ── Prompts ──────────────────────────────────────────

    async def _handle_prompts_list(self, params: dict) -> dict:
        prompts = []
        for name, prompt in self._prompts.items():
            prompts.append({
                "name": prompt.name,
                "description": prompt.description,
                "arguments": prompt.arguments,
            })
        return {"prompts": prompts}

    async def _handle_prompts_get(self, params: dict) -> dict:
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        if (not isinstance(name, str) or not isinstance(arguments, dict)
                or any(not isinstance(value, str) for value in arguments.values())):
            raise MCPError(-32602, "Invalid prompt params")

        prompt = self._prompts.get(name)
        if not prompt:
            raise MCPError(-32602, f"Unknown prompt: {name}")
        missing = [argument["name"] for argument in prompt.arguments
                   if argument.get("required") and not arguments.get(argument["name"])]
        if missing:
            raise MCPError(-32602, f"Missing required prompt arguments: {', '.join(missing)}")

        # Build the prompt message from the template
        if name == "workflow-from-api-doc":
            api_doc = arguments.get("api_doc", "")
            test_types = arguments.get("test_types", "baseline + concurrency")
            execute = arguments.get("execute", "false")
            text = (
                f"请完成以下端到端性能测试工作流：\n\n"
                f"测试类型: {test_types}\n"
                f"执行测试: {execute}\n\n"
                f"API文档:\n{api_doc}\n\n"
                f"请：1) 提取接口 2) 创建项目 3) 生成测试计划 4) 检查资源 5) 执行测试 6) 分析结果"
            )
        elif name == "requirements-to-plan":
            text = (
                f"项目: {arguments.get('project_id', '')}\n\n"
                "1) 调用 extract_performance_requirements 从下面文档提取性能需求草稿；"
                "逐条核对 source_verified=false 的条目并向我确认，再调用 save_performance_requirements 保存。\n"
                "2) 按需求中的 load_profile（并发、加压、时长、目标 TPS）和涉及的接口生成测试计划，并用 validate_jmx 校验。\n"
                "3) 列出无法从文档确定、需要我补充的信息。\n\n"
                f"文档：\n{arguments.get('document', '')}"
            )
        elif name == "analyze-execution":
            from app.services.ai.prompts.analysis_rules import ANALYSIS_RULES
            text = (
                f"{ANALYSIS_RULES}\n\n请分析执行 {arguments.get('execution_id', '')}：\n"
                "1) 先调用 get_execution_evidence 获取需求判定、基线/上次对比和服务端指标；必要时调用 parse_jtl_report 查看接口明细。\n"
                "2) 结论逐条引用需求编号（如 R1）说明是否达标；所有数字必须来自证据。\n"
                "3) 对不达标或退化项，结合服务端指标给出根因；证据不足时标注为【假设】并说明验证方法。\n"
                "4) 给出按优先级排序的优化建议。"
            )
        else:
            text = f"Prompt: {name}"

        return {
            "description": prompt.description,
            "messages": [
                {"role": "user", "content": {"type": "text", "text": text}}
            ],
        }

    # ── Helpers ──────────────────────────────────────────

    def _success_response(self, req_id: str, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _error_response(self, req_id: str, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }


class MCPError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# Module singleton
mcp_server = MCPServer()
