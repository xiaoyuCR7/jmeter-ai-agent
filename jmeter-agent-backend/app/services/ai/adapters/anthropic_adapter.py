from typing import AsyncIterator, List, Optional
import json

from anthropic import AsyncAnthropic

from app.services.ai.adapters.base import managed_request, managed_stream, BaseAIAdapter, ToolCallResponse, ToolCallRequest
from app.models.schemas import AIChatMessage
from app.services.ai import usage


class AnthropicAdapter(BaseAIAdapter):
    """Anthropic Claude适配器 — supports native tool use."""

    @staticmethod
    def _convert_messages(messages):
        system = []
        chat = []
        for message in messages:
            if message.role == 'system':
                system.append(message.content)
                continue
            if message.role == 'tool':
                role = 'user'
                blocks = [{'type': 'tool_result', 'tool_use_id': message.tool_call_id, 'content': message.content}]
            else:
                role = message.role
                blocks = [{'type': 'text', 'text': message.content}] if message.content else []
                for call in message.tool_calls or []:
                    function = call['function']
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': function['name'],
                                   'input': json.loads(function['arguments'])})
            if chat and chat[-1]['role'] == role:
                chat[-1]['content'].extend(blocks)
            elif blocks:
                chat.append({'role': role, 'content': blocks})
        return '\n\n'.join(system), chat

    def __init__(self, api_key: str, model: str, api_base: Optional[str] = None):
        super().__init__(api_key, model, api_base)
        kwargs = {"api_key": api_key}
        if api_base:
            kwargs["base_url"] = api_base
        self.client = AsyncAnthropic(**kwargs)

    @managed_request
    async def chat(self, messages: List[AIChatMessage], **kwargs) -> str:
        system_msg, chat_messages = self._convert_messages(messages)

        client = self.client.with_options(max_retries=kwargs['max_retries']) if 'max_retries' in kwargs else self.client
        response = await client.messages.create(
            model=self.model,
            system=system_msg or None,
            messages=chat_messages,
            max_tokens=kwargs.get("max_tokens", 8000),
            temperature=kwargs.get("temperature", 0.2),
        )
        usage.record_anthropic(response)
        return "".join(block.text for block in response.content if block.type == "text")

    @managed_stream
    async def chat_stream(self, messages: List[AIChatMessage], **kwargs) -> AsyncIterator[str]:
        system_msg, chat_messages = self._convert_messages(messages)

        async with self.client.messages.stream(
            model=self.model,
            system=system_msg or None,
            messages=chat_messages,
            max_tokens=kwargs.get("max_tokens", 8000),
            temperature=kwargs.get("temperature", 0.2),
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def generate(self, prompt: str, **kwargs) -> str:
        return await self.chat([AIChatMessage(role="user", content=prompt)], **kwargs)

    @managed_request
    async def chat_with_tools(
        self,
        messages: List[AIChatMessage],
        tools: list[dict],
        **kwargs
    ) -> ToolCallResponse:
        """Native Anthropic tool use."""
        # Separate system message
        system_msg, chat_messages = self._convert_messages(messages)

        # Build Anthropic tool format
        anthropic_tools = []
        for t in tools:
            func = t.get("function", t)
            anthropic_tools.append({
                "name": func.get("name", "unknown"),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", func.get("input_schema", {})),
            })

        response = await self.client.messages.create(
            model=self.model,
            system=system_msg or None,
            messages=chat_messages,
            tools=anthropic_tools,
            max_tokens=kwargs.get("max_tokens", 8000),
            temperature=kwargs.get("temperature", 0.2),
        )

        usage.record_anthropic(response)
        # Parse response content blocks
        text = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=block.id,
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                ))

        return ToolCallResponse(text=text, tool_calls=tool_calls)
