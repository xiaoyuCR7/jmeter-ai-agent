import asyncio
import logging
import json
import math
from abc import ABC, abstractmethod
from typing import AsyncIterator, List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from contextlib import asynccontextmanager, aclosing
from functools import wraps

from app.models.schemas import AIChatMessage

logger = logging.getLogger(__name__)

# Approximate tokens per character for truncation estimation
# English ~4 chars/token, Chinese ~1.5 chars/token, mixed ~2.5
TOKENS_PER_CHAR = 0.35  # conservative estimate for mixed Chinese/English text
TOKENS_PER_CHAR_CN = 0.65  # Chinese-dominant text
DEFAULT_MAX_TOKENS = 8000  # Conservative context window for embed + response


def managed_request(method):
    @wraps(method)
    async def call(self, *args, **kwargs):
        async with self._operation():
            return await method(self, *args, **kwargs)
    return call


def managed_stream(method):
    @wraps(method)
    async def stream(self, *args, **kwargs):
        async with self._operation():
            async with aclosing(method(self, *args, **kwargs)) as source:
                async for chunk in source:
                    yield chunk
    return stream


def message_groups(messages: List[AIChatMessage]) -> list[list[AIChatMessage]]:
    """Tool requests and their complete results are an indivisible context unit."""
    groups = []
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        if message.role == 'tool':
            continue  # An evicted/incomplete request must not leave orphan observations.
        if message.tool_calls:
            expected = {call['id'] for call in message.tool_calls}
            results = []
            while index < len(messages) and messages[index].role == 'tool':
                results.append(messages[index])
                index += 1
            if len(results) == len(expected) and {result.tool_call_id for result in results} == expected:
                groups.append([message, *results])
        else:
            groups.append([message])
    return groups


@dataclass
class ToolCallRequest:
    """A tool call the LLM wants to make."""
    id: str
    name: str
    arguments: dict


@dataclass
class ToolCallResponse:
    """Result of a chat_with_tools call — may contain text, tool calls, or both."""
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class BaseAIAdapter(ABC):
    """AI模型适配器基类"""

    def __init__(self, api_key: str, model: str, api_base: Optional[str] = None,
                 max_retries: int = 3, max_tokens: int = DEFAULT_MAX_TOKENS):
        self.api_key = api_key
        self.model = model
        self.api_base = api_base
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.context_window: Optional[int] = None

    def set_context_window(self, tokens: Optional[int]) -> None:
        """Use the model's real context size instead of the conservative global defaults."""
        if tokens:
            self.context_window = int(tokens)
            self.max_tokens = int(tokens)

    @property
    def context_budget(self) -> int:
        """Total tokens one request may use: input, tool schemas and reserved output."""
        from app.core.config import settings
        return getattr(self, 'context_window', None) or settings.AGENT_CONTEXT_TOKEN_BUDGET

    @asynccontextmanager
    async def _operation(self):
        if getattr(self, '_retired', False):
            raise RuntimeError('AI model configuration changed; create a new adapter')
        self._active_calls = getattr(self, '_active_calls', 0) + 1
        try:
            yield
        finally:
            self._active_calls -= 1
            if getattr(self, '_retired', False) and self._active_calls == 0:
                await self._close_client()

    async def _close_client(self):
        if not getattr(self, '_closed', False):
            try:
                await self.client.close()
                self._closed = True
            except Exception as error:
                logger.warning('AI client close failed: %s', type(error).__name__)

    async def retire(self):
        """Stop new requests; let active calls release the shared client last."""
        self._retired = True
        if not getattr(self, '_active_calls', 0):
            await self._close_client()

    async def _retry_api_call(self, call: Callable, name: str = "API call") -> Any:
        """Execute an API call with exponential backoff retry on 429/5xx errors."""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                return await call()
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                is_retryable = any(kw in msg for kw in (
                    "rate", "429", "too many", "timeout", "server error",
                    "500", "502", "503", "504", "connection", "overloaded"
                ))
                if not is_retryable or attempt == self.max_retries - 1:
                    raise
                wait = min(2 ** attempt, 30)
                logger.warning(
                    f"{name} attempt {attempt + 1}/{self.max_retries} failed: {e}. "
                    f"Retrying in {wait}s..."
                )
                await asyncio.sleep(wait)
        raise last_error

    def _truncate_messages(self, messages: List[AIChatMessage],
                           max_tokens: int = None, *, text_fallback: bool = False) -> List[AIChatMessage]:
        """Bound estimated input size without dropping policy/task or splitting calls."""
        budget = self.max_tokens if max_tokens is None else max_tokens
        groups = message_groups(messages)
        if not groups:
            return []
        costs = [sum(self.estimate_tokens(json.dumps(m.to_wire(), ensure_ascii=False)) + 8
                     for m in (self._text_messages(group) if text_fallback else group))
                 for group in groups]
        required = {i for i, group in enumerate(groups) if any(m.role == 'system' or m.pinned for m in group)}
        required.add(len(groups) - 1)
        used = sum(costs[i] for i in required)
        if used > budget:
            raise ValueError('Current task/policy/tool exchange exceeds the context budget; reduce input size')
        selected = set(required)
        for index in reversed(range(len(groups))):
            if index not in selected and used + costs[index] <= budget:
                selected.add(index)
                used += costs[index]
        return [message for index, group in enumerate(groups) if index in selected for message in group]

    def prepare_chat_messages(self, messages: List[AIChatMessage], *, response_tokens: int) -> List[AIChatMessage]:
        """Reserve the existing output allowance before selecting plain-chat input."""
        input_budget = min(self.max_tokens, self.context_budget - response_tokens - 256)
        if input_budget <= 0:
            raise ValueError('Response allowance exceeds the context budget; review token limits')
        return self._truncate_messages(messages, max_tokens=input_budget)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        chinese = sum(1 for char in text if '\u4e00' <= char <= '\u9fff')
        return math.ceil(chinese * TOKENS_PER_CHAR_CN + (len(text) - chinese) * TOKENS_PER_CHAR)

    @staticmethod
    def _text_messages(messages: List[AIChatMessage]) -> list[AIChatMessage]:
        result = []
        for message in messages:
            if message.tool_calls:
                result.append(AIChatMessage(role='assistant', content=message.content + '\n' + json.dumps(
                    {'tool_calls': message.tool_calls}, ensure_ascii=False)))
            elif message.role == 'tool':
                result.append(AIChatMessage(role='user', content=f'[Tool result {message.tool_call_id}]\n{message.content}'))
            else:
                result.append(message)
        return result

    @abstractmethod
    async def chat(self, messages: List[AIChatMessage], **kwargs) -> str:
        """非流式对话"""
        pass

    @abstractmethod
    async def chat_stream(self, messages: List[AIChatMessage], **kwargs) -> AsyncIterator[str]:
        """流式对话"""
        pass

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        """单轮生成"""
        pass

    async def chat_with_tools(
        self,
        messages: List[AIChatMessage],
        tools: list[dict],
        **kwargs
    ) -> ToolCallResponse:
        """Send messages with tool definitions, return text + tool calls.

        Default implementation: fall back to prompt-based tool calling.
        Override in adapter subclasses for native tool support.
        """
        # Build a prompt that describes available tools
        tools_desc = self._format_tools_for_prompt(tools)
        system_msg = f"""你是一个AI助手，可以使用以下工具来完成任务：

{tools_desc}

当需要使用工具时，请严格按以下JSON格式输出工具调用：
{{"tool_call": {{"name": "工具名", "arguments": {{"arg1": "value1", ...}}}}}}

你可以在一条消息中调用多个工具。工具调用结果会以 {{"tool_result": ...}} 格式返回给你。
如果不需要使用工具，直接输出你的回答。"""

        # Insert/replace system message
        msgs = list(messages)
        if msgs and msgs[0].role == "system":
            msgs[0] = AIChatMessage(role="system", content=msgs[0].content + "\n\n" + system_msg)
        else:
            msgs.insert(0, AIChatMessage(role="system", content=system_msg))

        total_budget = kwargs.pop('context_window_tokens', self.context_budget)
        input_budget = total_budget - kwargs.get('max_tokens', 8000) - 256
        # Measure the final text representation, but select complete native groups.
        selected = self._truncate_messages(msgs, max_tokens=input_budget, text_fallback=True)
        response = await self.chat(self._text_messages(selected), **kwargs)
        return self._parse_tool_calls_from_text(response)

    def _format_tools_for_prompt(self, tools: list[dict]) -> str:
        """Format tool definitions for prompt-based fallback."""
        import json
        lines = []
        for t in tools:
            func = t.get("function", t)
            name = func.get("name", "unknown")
            desc = func.get("description", "")
            params = func.get("parameters", func.get("input_schema", {}))
            params_str = json.dumps(params, ensure_ascii=False, separators=(',', ':'))
            lines.append(f"- {name}: {desc}\n  Schema: {params_str}")
        return "\n".join(lines)

    def _parse_tool_calls_from_text(self, text: str) -> ToolCallResponse:
        """Extract tool calls from text response (prompt-based fallback).

        Handles both formats:
        - Singular: {"tool_call": {"name": "...", "arguments": {...}}}
        - Plural:   {"tool_calls": [{"name": "...", "arguments": {...}}]}
        """
        import re, uuid

        tool_calls = []
        decoder = json.JSONDecoder()
        spans = []
        consumed = 0
        batch_id = uuid.uuid4().hex
        # Regex locates the envelope only; JSONDecoder handles nested arrays,
        # escaped quotes and braces inside argument strings.
        for match in re.finditer(r'\{\s*"tool_calls?"\s*:', text):
            if match.start() < consumed:
                continue
            try:
                data, length = decoder.raw_decode(text[match.start():])
                items = data.get('tool_calls', [data.get('tool_call')])
                if not isinstance(items, list) or not items or any(
                    not isinstance(item, dict) or not isinstance(item.get('name'), str) for item in items
                ):
                    continue
                for item in items:
                    tool_calls.append(ToolCallRequest(id=f'call_{batch_id}_{len(tool_calls)}',
                        name=item['name'], arguments=item.get('arguments', {})))
                consumed = match.start() + length
                spans.append((match.start(), consumed))
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        remaining = text
        for start, end in reversed(spans):
            remaining = remaining[:start] + remaining[end:]
        remaining = re.sub(r'```(?:json)?\s*```', '', remaining)

        # Clean up remaining text
        clean_text = remaining.strip()
        # Remove surrounding code blocks
        if clean_text.startswith("```"):
            lines = clean_text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_text = "\n".join(lines).strip()

        return ToolCallResponse(text=clean_text, tool_calls=tool_calls)
