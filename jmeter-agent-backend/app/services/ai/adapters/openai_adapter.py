import json
from typing import AsyncIterator, List, Optional

from openai import AsyncOpenAI, BadRequestError

from app.services.ai.adapters.base import managed_request, managed_stream, BaseAIAdapter, ToolCallResponse, ToolCallRequest
from app.models.schemas import AIChatMessage
from app.services.ai import usage


class OpenAIAdapter(BaseAIAdapter):
    """OpenAI适配器 — supports native function calling."""

    def __init__(self, api_key: str, model: str, api_base: Optional[str] = None):
        super().__init__(api_key, model, api_base)
        self.client = AsyncOpenAI(api_key=api_key, base_url=api_base)

    @managed_request
    async def chat(self, messages: List[AIChatMessage], **kwargs) -> str:
        """``json_mode=True`` asks for one JSON object (the prompt must say JSON); a provider that
        refuses the option is asked again without it, and not asked with it again."""
        client = self.client.with_options(max_retries=kwargs['max_retries']) if 'max_retries' in kwargs else self.client
        request = dict(model=self.model, messages=[m.to_wire() for m in messages],
                       temperature=kwargs.get("temperature", 0.2), max_tokens=kwargs.get("max_tokens", 8000))
        if kwargs.get('json_mode') and not getattr(self, '_json_mode_refused', False):
            try:
                response = await client.chat.completions.create(**request, response_format={'type': 'json_object'})
            except BadRequestError as error:
                if 'response_format' not in str(error) and 'json' not in str(error).lower():
                    raise
                self._json_mode_refused = True
                response = await client.chat.completions.create(**request)
        else:
            response = await client.chat.completions.create(**request)
        usage.record_openai(response)
        return response.choices[0].message.content or ""

    @managed_stream
    async def chat_stream(self, messages: List[AIChatMessage], **kwargs) -> AsyncIterator[str]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[m.to_wire() for m in messages],
            temperature=kwargs.get("temperature", 0.2),
            max_tokens=kwargs.get("max_tokens", 8000),
            stream=True,
        )
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

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            tools=tools,
            tool_choice="auto",
            temperature=kwargs.get("temperature", 0.2),
            max_tokens=kwargs.get("max_tokens", 8000),
        )

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
