"""The local access token: proof a caller is the user, not just something on this machine.

Loopback binding and the Origin check stop other hosts and other websites, but anything
that speaks HTTP locally has no Origin, and that includes a test plan's own HTTP samplers.
The browser gets the token once through a link and keeps it as an HttpOnly cookie;
other clients (MCP, scripts) send it as a Bearer token.

Run ``python -m app.core.local_access`` to print the link.
"""
import hmac
import ipaddress
import os
import secrets
import tempfile

from starlette.requests import HTTPConnection

from app.core.config import settings

COOKIE_NAME = 'jmeter_agent_access'
COOKIE_MAX_AGE = 400 * 24 * 3600  # the longest browsers keep a cookie
FRONTEND_DEV_URL = 'http://localhost:3000'


def token_path() -> str:
    return os.path.join(settings.BASE_DIR, '.local_access_token')


def get_token() -> str:
    path = token_path()
    try:
        with open(path, encoding='utf-8') as file:
            token = file.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    # Replaced atomically so a concurrent reader never sees a partial token.
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.local_access_')
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        file.write(token)
    os.replace(temporary, path)
    return token


def token_matches(candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), get_token().encode())


def access_url(base: str = FRONTEND_DEV_URL) -> str:
    return f'{base}/#token={get_token()}'


def is_loopback_client(scope) -> bool:
    """The connection itself, unlike the bind setting or a Host header, cannot be misconfigured or forged."""
    client = scope.get('client')
    if not client:
        return True  # a Unix socket has no peer address
    try:
        address = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    return (getattr(address, 'ipv4_mapped', None) or address).is_loopback


def is_authorized(scope) -> bool:
    connection = HTTPConnection(scope)
    scheme, _, credentials = connection.headers.get('authorization', '').partition(' ')
    return token_matches(connection.cookies.get(COOKIE_NAME)) or (scheme.lower() == 'bearer'
                                                                  and token_matches(credentials.strip()))


if __name__ == '__main__':
    print(access_url())
