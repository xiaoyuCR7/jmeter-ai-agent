from typing import AsyncIterator, List, Optional
import json
from urllib.parse import urlparse

from anthropic import AsyncAnthropic

from app.services.ai.adapters.base import managed_request, managed_stream, BaseAIAdapter, ToolCallResponse, ToolCallRequest
from app.models.schemas import AIChatMessage
from app.services.ai import usage


REPORT_TOOL = 'write_report'


class AnthropicAdapter(BaseAIAdapter):
    """Anthropic Claude适配器 — supports native tool use.

    The system prompt and the first user message (a run's facts and evidence, the same for every
    question about it) are marked for prompt caching. ``json_mode`` with a ``json_schema`` makes
    the model fill in a tool with that schema, so a report is always one well-formed object.
    """

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
        # Compatible endpoints of other vendors may refuse cache markers; Anthropic's own takes them.
        host = urlparse(api_base or 'https://api.anthropic.com').hostname or ''
        self.prompt_caching = host == 'anthropic.com' or host.endswith('.anthropic.com')

    def _request(self, messages: List[AIChatMessage], **kwargs) -> dict:
        """Model, token limit, system prompt and messages, with cache breakpoints where the reusable prefix ends.

        The Messages API no longer takes ``temperature`` and the SDK refuses it, so only a compatible endpoint of
        another vendor, which may still use it, gets it, through the request body.
        """
        system_msg, chat_messages = self._convert_messages(messages)
        request = {'model': self.model, 'max_tokens': kwargs.get('max_tokens', 8000), 'messages': chat_messages}
        if not self.prompt_caching:
            request['extra_body'] = {'temperature': kwargs.get('temperature', 0.2)}
            if system_msg:
                request['system'] = system_msg
            return request
        if system_msg:
            request['system'] = [{'type': 'text', 'text': system_msg, 'cache_control': {'type': 'ephemeral'}}]
        if chat_messages and chat_messages[0]['role'] == 'user' and len(chat_messages) > 1:
            first = chat_messages[0]['content'] = [dict(block) for block in chat_messages[0]['content']]
            if first and first[-1].get('type') == 'text':
                first[-1]['cache_control'] = {'type': 'ephemeral'}
        return request

    @managed_request
    async def chat(self, messages: List[AIChatMessage], **kwargs) -> str:
        client = self.client.with_options(max_retries=kwargs['max_retries']) if 'max_retries' in kwargs else self.client
        request = self._request(messages, **kwargs)
        schema = kwargs.get('json_schema') if kwargs.get('json_mode') else None
        if schema:
            request.update(tools=[{'name': REPORT_TOOL, 'description': 'Record the answer as structured data.',
                                   'input_schema': schema}],
                           tool_choice={'type': 'tool', 'name': REPORT_TOOL})
        response = await client.messages.create(**request)
        usage.record_anthropic(response)
        if schema:
            answer = next((block.input for block in response.content if block.type == 'tool_use'), None)
            if isinstance(answer, dict):
                return json.dumps(answer, ensure_ascii=False)
        return "".join(block.text for block in response.content if block.type == "text")

    @managed_stream
    async def chat_stream(self, messages: List[AIChatMessage], **kwargs) -> AsyncIterator[str]:
        async with self.client.messages.stream(**self._request(messages, **kwargs)) as stream:
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
        # Build Anthropic tool format
        anthropic_tools = []
        for t in tools:
            func = t.get("function", t)
            anthropic_tools.append({
                "name": func.get("name", "unknown"),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", func.get("input_schema", {})),
            })

        response = await self.client.messages.create(**self._request(messages, **kwargs), tools=anthropic_tools)

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
