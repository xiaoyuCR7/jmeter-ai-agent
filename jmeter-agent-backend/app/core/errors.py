"""Errors a service raises when it refuses a request.

Services stay free of the web framework: they raise ``ServiceError`` with the HTTP status that fits,
and main.py turns it into a response. Agent tools and the MCP server see the plain message.
"""


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
