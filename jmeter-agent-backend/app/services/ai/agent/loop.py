"""One tool-calling conversation: the model calls tools, sees their results, and stops when it answers.

Agents and workflows build on ``tool_loop``; nothing here knows what the tools do.

- Tool calls run one at a time, each with its own timeout. A cancelled call or a system error stops
  the conversation at once; the calls after it in the same reply are recorded as not executed.
- A failed input the model corrects (a later call of the same tool succeeds) does not fail the loop.
- ``done`` lets the caller end the conversation as soon as the tools have produced what it needs,
  without asking the model for a closing reply.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.core.config import settings
from app.models.schemas import AIChatMessage
from app.services.ai.agent.tool_registry import ToolRegistry
from app.services.ai.agent.types import ErrorCategory, ToolResult, classify_exception

logger = logging.getLogger(__name__)

RESPONSE_TOKENS = 8000  # Thinking models spend this before they write, and a create_jmx call can be long.

Emit = Callable[[str, str, Optional[dict]], None]


@dataclass
class LoopResult:
    success: bool
    text: str = ''
    results: list[ToolResult] = field(default_factory=list)
    error: Optional[str] = None
    category: Optional[ErrorCategory] = None
    rounds: int = 0

    @property
    def any_succeeded(self) -> bool:
        return any(result.success for result in self.results)

    @property
    def data(self) -> dict:
        """Statuses aligned with the tools' raw payloads, for callers that read what was created."""
        failed = sum(not result.success for result in self.results)
        return {
            'tool_statuses': [{'tool': r.tool_name, 'success': r.success, 'error': r.error} for r in self.results],
            'tool_results': [r.data for r in self.results],
            'final_text': self.text, 'total_calls': len(self.results),
            'passed': len(self.results) - failed, 'failed': failed,
        }


def response_budget(adapter) -> int:
    return min(RESPONSE_TOKENS, max(2000, adapter.context_budget // 4))


async def execute_tool(tools: ToolRegistry, name: str, args: dict) -> ToolResult:
    """Run one tool within its timeout; a timed-out write or run is not safe to repeat blindly."""
    tool = tools.get(name)
    timeout = tool.timeout_seconds if tool else settings.AGENT_TOOL_TIMEOUT_SECONDS
    start = time.time()
    try:
        result = await asyncio.wait_for(tools.execute(name, args), timeout=timeout)
        result.duration_ms = (time.time() - start) * 1000
        return result
    except asyncio.TimeoutError:
        logger.error("Tool '%s' timed out after %ss", name, timeout)
        return ToolResult(False, error=f"Tool '{name}' timed out after {timeout}s", tool_name=name,
                          duration_ms=(time.time() - start) * 1000,
                          error_category=(ErrorCategory.SYSTEM_ERROR if tool and tool.require_confirmation
                                          else ErrorCategory.RETRYABLE))
    except Exception as error:
        logger.exception("Tool '%s' failed:", name)
        return ToolResult(False, error=f"Tool '{name}' error: {error}", tool_name=name,
                          duration_ms=(time.time() - start) * 1000, error_category=classify_exception(error))


def _quiet(*_args) -> None:
    pass


async def tool_loop(adapter, messages: list[AIChatMessage], tools: ToolRegistry, *, max_rounds: int,
                    require_tool: bool = False, done: Optional[Callable[[list[ToolResult]], bool]] = None,
                    record: Callable[[AIChatMessage], None] = _quiet, emit: Emit = _quiet,
                    temperature: float = 0.2) -> LoopResult:
    """Talk to the model until it answers without a tool call, ``done`` is satisfied, or rounds run out.

    ``messages`` must start with the system prompt. Each assistant turn and tool result is also passed
    to ``record``, so a caller can keep the run's transcript.
    """
    schemas = tools.get_openai_schemas()
    if not schemas:
        return LoopResult(False, error='No tools are registered for this conversation',
                          category=ErrorCategory.SYSTEM_ERROR)
    reply_budget = response_budget(adapter)
    schema_cost = adapter.estimate_tokens(json.dumps(schemas, ensure_ascii=False, separators=(',', ':')))
    input_budget = min(adapter.max_tokens, adapter.context_budget - schema_cost - reply_budget - 256)
    if input_budget <= 0:
        return LoopResult(False, error='Tool schemas and reserved response exceed the context budget',
                          category=ErrorCategory.SYSTEM_ERROR)

    conversation = list(messages)
    results: list[ToolResult] = []
    text, concluded, reminded, rounds = '', False, False, 0

    def finish(success, error=None, category=None) -> LoopResult:
        return LoopResult(success, text, results, error, category, rounds)

    for rounds in range(1, max_rounds + 1):
        try:
            response = await adapter.chat_with_tools(
                adapter._truncate_messages(conversation, max_tokens=input_budget), schemas,
                temperature=temperature, max_tokens=reply_budget, context_window_tokens=adapter.context_budget)
        except ValueError as error:
            return finish(False, str(error), ErrorCategory.SYSTEM_ERROR)
        except Exception as error:
            # The model client has already retried with backoff; repeating would multiply that.
            logger.exception('Model call failed in round %s:', rounds)
            return finish(False, f'LLM communication failed: {error}', ErrorCategory.SYSTEM_ERROR)

        if response.has_tool_calls:
            calls = response.tool_calls
            if len(calls) > settings.AGENT_MAX_TOOL_CALLS_PER_RESPONSE:
                return finish(False, 'Tool batch exceeds the per-response limit; split into smaller rounds',
                              ErrorCategory.INPUT_ERROR)
            ids = [call.id for call in calls]
            if any(not isinstance(i, str) or not i.strip() for i in ids) or len(set(ids)) != len(ids):
                return finish(False, 'Tool call IDs must be nonempty and unique within a response',
                              ErrorCategory.INPUT_ERROR)
            request = AIChatMessage(role='assistant', content=response.text or '', tool_calls=[
                {'id': call.id, 'type': 'function',
                 'function': {'name': call.name, 'arguments': json.dumps(call.arguments, ensure_ascii=False)}}
                for call in calls])
            conversation.append(request)
            record(request)
            for index, call in enumerate(calls):
                emit('tool_call', f'Round {rounds}/{max_rounds}: {call.name}',
                     {'tool': call.name, 'args': call.arguments, 'round': rounds})
                result = await execute_tool(tools, call.name, call.arguments)
                results.append(result)
                observation = AIChatMessage(role='tool', content=result.to_json(), tool_call_id=call.id)
                conversation.append(observation)
                record(observation)
                emit('tool_result', result.to_message()[:500], {
                    'tool': call.name, 'success': result.success, 'error': result.error,
                    'data': str(result.data)[:500] if result.data else None,
                    'error_category': result.error_category.value if result.error_category else None})
                if result.error_category in (ErrorCategory.CANCELLED, ErrorCategory.SYSTEM_ERROR):
                    reason = ('preceding call cancelled' if result.error_category == ErrorCategory.CANCELLED
                              else 'preceding call failed with a system error')
                    for skipped in calls[index + 1:]:
                        record(AIChatMessage(role='tool', tool_call_id=skipped.id,
                                             content=json.dumps({'success': False, 'error': f'Not executed: {reason}'})))
                    return finish(False, result.error, result.error_category)
            if done is not None and done(results):
                concluded = True
                break
        elif not (response.text or '').strip() and rounds < max_rounds:
            # A thinking model that ran out of budget returns nothing; that is not a conclusion.
            logger.warning('Empty model reply in round %s', rounds)
        elif require_tool and not results and not reminded and rounds < max_rounds:
            # A model often opens by saying what it will do. Asking again costs far less than failing.
            reminded = True
            conversation.append(AIChatMessage(role='assistant', content=response.text or ''))
            conversation.append(AIChatMessage(role='user', pinned=True,
                                              content='Nothing has been done yet: no tool was called. Call the tools now.'))
        else:
            concluded = True
            text = (response.text or '').strip()
            if text:
                record(AIChatMessage(role='assistant', content=text))
            break
    else:
        emit('max_rounds', 'Reached the round limit before the model finished', {'max': max_rounds})

    if require_tool and not results:
        return finish(False, 'No tool was executed for a task that needs tools', ErrorCategory.TOOL_ERROR)

    def recovered(index: int) -> bool:
        # A rejected argument (e.g. 1000 instead of "1000") the model then fixed is not a failure.
        failure = results[index]
        return failure.error_category == ErrorCategory.INPUT_ERROR and any(
            later.success and later.tool_name == failure.tool_name for later in results[index + 1:])
    unrecovered = [i for i, result in enumerate(results) if not result.success and not recovered(i)]
    if concluded and not unrecovered:
        return finish(True)
    last = results[unrecovered[-1]] if unrecovered else None
    if last is not None:
        return finish(False, last.error, last.error_category or ErrorCategory.TOOL_ERROR)
    return finish(False, 'Stopped before the model finished', ErrorCategory.TOOL_ERROR)
