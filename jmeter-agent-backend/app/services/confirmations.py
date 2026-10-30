"""Short-lived, one-shot user decisions for exact agent tool invocations.

Tools with a ``write`` or ``execute`` risk wait here until the user approves or
denies them in the UI, or until the request times out (treated as denied).
"""
import asyncio
import contextvars
import copy
import time
import uuid
from pathlib import Path

# Set by the ToolRegistry running the call: 'agent' (this app's agents) or 'mcp' (an outside client).
confirmation_source = contextvars.ContextVar('confirmation_source', default='agent')


def _project_of(arguments: dict) -> str | None:
    """The project a call acts on, so the user can tell which one is being asked about."""
    from app.services.storage import store
    project_id = arguments.get('project_id')
    jmx_path = arguments.get('jmx_path')
    if not project_id and isinstance(jmx_path, str):
        target = Path(jmx_path).resolve()
        project_id = next((plan.project_id for plan in store.test_plans.values()
                           if Path(plan.jmx_path).resolve() == target), None)
    project = store.get_project(project_id) if isinstance(project_id, str) else None
    return project.name if project else project_id


class ConfirmationManager:
    def __init__(self, timeout_seconds=120.0, max_pending=128):
        self.timeout_seconds = timeout_seconds
        self.max_pending = max_pending
        self._pending = {}

    async def request(self, tool_name: str, arguments: dict) -> bool:
        return await self.ask(tool_name, arguments) == 'approved'

    async def ask(self, tool_name: str, arguments: dict) -> str:
        """'approved', 'denied', or 'unanswered' when no decision came in time."""
        if len(self._pending) >= self.max_pending:
            return 'unanswered'
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = {
            'id': request_id, 'tool': tool_name, 'arguments': copy.deepcopy(arguments),
            'source': confirmation_source.get(), 'project': _project_of(arguments),
            'expires_at': time.time() + self.timeout_seconds, 'future': future,
        }
        try:
            approved = await asyncio.wait_for(future, timeout=self.timeout_seconds)
            return 'approved' if approved else 'denied'
        except asyncio.TimeoutError:
            return 'unanswered'
        finally:
            self._pending.pop(request_id, None)

    def list_pending(self) -> list[dict]:
        return [copy.deepcopy({k: v for k, v in item.items() if k != 'future'})
                for item in self._pending.values()
                if not item['future'].done() and item['expires_at'] > time.time()]

    def decide(self, request_id: str, approved: bool) -> None:
        item = self._pending.get(request_id)
        if item is None or item['future'].done() or item['expires_at'] <= time.time():
            raise KeyError('Confirmation expired or already consumed')
        item['future'].set_result(approved)


confirmations = ConfirmationManager()
