import json
from typing import AsyncIterator, List, Optional

from openai import AsyncAzureOpenAI, AsyncOpenAI, BadRequestError

from app.services.ai.adapters.base import managed_request, managed_stream, BaseAIAdapter, ToolCallResponse, ToolCallRequest
from app.models.schemas import AIChatMessage
from app.services.ai import usage


def _refuses(error: BadRequestError, parameter: str) -> bool:
    """Whether a 400 says this request parameter is not accepted by the model."""
    text = str(error).lower()
    return parameter in text and any(word in text for word in (
        'unsupported', 'not supported', 'does not support', 'not allowed', 'only the default', 'unrecognized',
        'unknown', 'invalid', 'extra inputs', 'use \'max_completion_tokens\'', 'max_completion_tokens'))


class OpenAIAdapter(BaseAIAdapter):
    """OpenAI适配器 — supports native function calling.

    Serves every OpenAI-compatible provider. Models differ in what they accept: reasoning models
    want ``max_completion_tokens`` instead of ``max_tokens`` and only their default temperature,
    and some providers have no JSON mode. A refused parameter is dropped or renamed once and the
    request sent again; the adapter remembers, so later requests go right the first time.
    """

    def __init__(self, api_key: str, model: str, api_base: Optional[str] = None):
        super().__init__(api_key, model, api_base)
        self.client = AsyncOpenAI(api_key=api_key, base_url=api_base)

    @property
    def _quirks(self) -> set[str]:
        """What this model refused so far."""
        return self.__dict__.setdefault('_refused', set())

    def _shape(self, request: dict) -> dict:
        request = dict(request)
        if 'max_completion_tokens' in self._quirks and 'max_tokens' in request:
            request['max_completion_tokens'] = request.pop('max_tokens')
        if 'no_temperature' in self._quirks:
            request.pop('temperature', None)
        if 'no_json_mode' in self._quirks:
            request.pop('response_format', None)
        return request

    def _learn(self, error: BadRequestError, request: dict) -> bool:
        """Remember what this model refused; False when the error is about something else."""
        before = set(self._quirks)
        if 'max_tokens' in request and _refuses(error, 'max_tokens'):
            self._quirks.add('max_completion_tokens')
        if 'temperature' in request and _refuses(error, 'temperature'):
            self._quirks.add('no_temperature')
        if 'response_format' in request and ('response_format' in str(error) or 'json' in str(error).lower()):
            self._quirks.add('no_json_mode')
        return self._quirks != before

    async def _create(self, client, request: dict):
        for _ in range(4):  # At most one retry per parameter that can be refused.
            shaped = self._shape(request)
            try:
                return await client.chat.completions.create(**shaped)
            except BadRequestError as error:
                if not self._learn(error, shaped):
                    raise
        return await client.chat.completions.create(**self._shape(request))

    @managed_request
    async def chat(self, messages: List[AIChatMessage], **kwargs) -> str:
        """``json_mode=True`` asks for one JSON object (the prompt must say JSON); a provider that
        refuses the option is asked again without it, and not asked with it again."""
        client = self.client.with_options(max_retries=kwargs['max_retries']) if 'max_retries' in kwargs else self.client
        request = dict(model=self.model, messages=[m.to_wire() for m in messages],
                       temperature=kwargs.get("temperature", 0.2), max_tokens=kwargs.get("max_tokens", 8000))
        if kwargs.get('json_mode'):
            request['response_format'] = {'type': 'json_object'}
        response = await self._create(client, request)
        usage.record_openai(response)
        return response.choices[0].message.content or ""

    @managed_stream
    async def chat_stream(self, messages: List[AIChatMessage], **kwargs) -> AsyncIterator[str]:
        response = await self._create(self.client, dict(
            model=self.model,
            messages=[m.to_wire() for m in messages],
            temperature=kwargs.get("temperature", 0.2),
            max_tokens=kwargs.get("max_tokens", 8000),
            stream=True,
        ))
        try:
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        finally:
            await response.close()

    async def generate(self, prompt: str, **kwargs) -> str:
        return await self.chat([AIChatMessage(role="user", content=prompt)], **kwargs)

    @managed_request
    async def chat_with_tools(
        self,
        messages: List[AIChatMessage],
        tools: list[dict],
        **kwargs
    ) -> ToolCallResponse:
        """Native OpenAI function calling."""
        openai_messages = [m.to_wire() for m in messages]

        response = await self._create(self.client, dict(
            model=self.model,
            messages=openai_messages,
            tools=tools,
            tool_choice="auto",
            temperature=kwargs.get("temperature", 0.2),
            max_tokens=kwargs.get("max_tokens", 8000),
        ))

        usage.record_openai(response)
        choice = response.choices[0]
        text = choice.message.content or ""
        tool_calls = []

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = None  # Invalid JSON must fail schema validation, never become valid defaults.
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        return ToolCallResponse(text=text, tool_calls=tool_calls)


class AzureOpenAIAdapter(OpenAIAdapter):
    """Azure OpenAI: the resource endpoint and an API version; ``model`` is the deployment name."""

    def __init__(self, api_key: str, model: str, api_base: Optional[str] = None, api_version: Optional[str] = None):
        BaseAIAdapter.__init__(self, api_key, model, api_base)
        from app.services.ai.providers import AZURE_API_VERSION
        self.client = AsyncAzureOpenAI(api_key=api_key, azure_endpoint=api_base or '',
                                       api_version=api_version or AZURE_API_VERSION)
