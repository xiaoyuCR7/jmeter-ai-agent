"""Requests the adapters build must be ones the installed SDKs accept; fakes elsewhere never check that."""
import inspect

from anthropic.resources.messages import AsyncMessages
from openai.resources.chat.completions import AsyncCompletions

from app.models.schemas import AIChatMessage
from app.services.ai.adapters.anthropic_adapter import AnthropicAdapter

MESSAGES = [AIChatMessage(role='system', content='rules'), AIChatMessage(role='user', content='evidence'),
            AIChatMessage(role='assistant', content='ok'), AIChatMessage(role='user', content='question')]
TOOLS = [{'name': 'lookup', 'description': 'd', 'input_schema': {'type': 'object'}}]


def accepted(method, request: dict) -> None:
    inspect.signature(method).bind(None, **request)  # TypeError names any parameter the SDK refuses.


def test_the_official_anthropic_request_fits_the_sdk_and_is_cached():
    request = AnthropicAdapter('k', 'claude', None)._request(MESSAGES, temperature=0.5, max_tokens=100)
    accepted(AsyncMessages.create, request | {'tools': TOOLS})
    accepted(AsyncMessages.stream, request)
    assert 'temperature' not in request and 'extra_body' not in request
    assert request['system'][0]['cache_control'] == {'type': 'ephemeral'}
    assert request['messages'][0]['content'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert request['max_tokens'] == 100


def test_a_compatible_endpoint_still_gets_its_temperature_in_the_body():
    request = AnthropicAdapter('k', 'kimi', 'https://api.moonshot.cn/anthropic')._request(MESSAGES, temperature=0.5)
    accepted(AsyncMessages.create, request)
    assert request['extra_body'] == {'temperature': 0.5} and request['system'] == 'rules'
    assert 'cache_control' not in str(request)


def test_no_system_prompt_is_left_out_rather_than_sent_as_null():
    request = AnthropicAdapter('k', 'claude', None)._request([AIChatMessage(role='user', content='hi')])
    assert 'system' not in request


def test_the_openai_request_fits_the_sdk():
    accepted(AsyncCompletions.create, dict(model='m', messages=[], temperature=0.2, max_tokens=10,
                                           max_completion_tokens=10, response_format={'type': 'json_object'},
                                           tools=[], tool_choice='auto', stream=True))
