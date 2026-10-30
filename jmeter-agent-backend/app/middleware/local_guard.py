"""Local-mode request guards: loopback peer, trusted Host, same-site Origin and the access token.

There is no login. These checks keep other machines, other websites open in the same
browser (cross-site requests, DNS rebinding) and local programs without the access
token, such as a test plan's HTTP samplers, from driving the local API.
"""
import re
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from app.core import local_access
from app.core.config import settings

SESSION_PATH = '/api/v1/session'
_PROTECTED = re.compile(r'/(api|ws|reports)(/|$)')
# The rendered dashboard: metrics and labels only, reached by run ID. Its sandbox gives it an opaque
# origin, so the browser leaves the same-site cookie off the page's own scripts and styles.
_REPORT_DASHBOARD = re.compile(r'/reports/[^/]+/html(/|$)')
_REPORT_FONT = re.compile(r'/html/.*\.(woff2?|ttf|eot)$')
REPORT_POLICY = 'sandbox allow-scripts allow-popups allow-downloads'


async def _deny(scope, receive, send, status, detail, code=None):
    if scope['type'] == 'websocket':
        return await send({'type': 'websocket.close', 'code': 1008})
    body = {'detail': detail, **({'code': code} if code else {})}
    return await JSONResponse(body, status_code=status)(scope, receive, send)


class TrustedHostMiddleware:
    """Explicit hosts, including IPv6; WS denial uses a WS frame, not HTTP ASGI events."""
    def __init__(self, app, allowed_hosts):
        if not allowed_hosts or any('*' in host for host in allowed_hosts):
            raise ValueError('ALLOWED_HOSTS must list explicit trusted hostnames')
        self.app = app
        self.allowed_hosts = {host.lower() for host in allowed_hosts}

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            if not local_access.is_loopback_client(scope):
                return await _deny(scope, receive, send, 403, 'Only this machine may connect')
            raw_hosts = [v.decode('latin-1') for k, v in scope.get('headers', []) if k.lower() == b'host']
            try:
                parsed = urlsplit('//' + raw_hosts[0]) if len(raw_hosts) == 1 else None
                valid = parsed is not None and parsed.hostname in self.allowed_hosts and not (
                    parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment)
                if parsed is not None:
                    parsed.port  # Reject malformed ports as well.
            except ValueError:
                valid = False
            if not valid:
                return await _deny(scope, receive, send, 400, 'Invalid host header')
        return await self.app(scope, receive, send)


class OriginGuardMiddleware:
    """Reject state-changing requests and WebSocket handshakes from foreign origins."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            connection = HTTPConnection(scope)
            origin = connection.headers.get('origin')
            method = scope.get('method', 'GET')
            same_origin = str(connection.base_url).rstrip('/').replace('ws://', 'http://').replace('wss://', 'https://')
            check = method not in ('GET', 'HEAD', 'OPTIONS') or scope['type'] == 'websocket'                 or scope.get('path', '').startswith('/api/v1/mcp/')
            if check and origin and origin != same_origin and origin not in settings.CORS_ORIGINS:
                return await _deny(scope, receive, send, 403, 'Origin not allowed')
        return await self.app(scope, receive, send)


class LocalAccessMiddleware:
    """Require the access token for the API, live updates and stored reports; the UI shell stays public."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            path = scope.get('path', '')
            exempt = path == SESSION_PATH or _REPORT_DASHBOARD.match(path)
            if _PROTECTED.match(path) and not exempt and not local_access.is_authorized(scope):
                return await _deny(scope, receive, send, 401, 'Local access token required',
                                   code='local_access_required')
        return await self.app(scope, receive, send)


class ReportSandboxMiddleware:
    """Serve stored reports as untrusted content: they share the app's origin but hold test data."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/reports/'):
            return await self.app(scope, receive, send)

        async def send_sandboxed(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['Content-Security-Policy'] = REPORT_POLICY
                headers['X-Content-Type-Options'] = 'nosniff'
                if _REPORT_FONT.search(scope['path']):
                    headers['Access-Control-Allow-Origin'] = '*'  # fonts need CORS, and a sandboxed page has no origin
            await send(message)
        return await self.app(scope, receive, send_sandboxed)
