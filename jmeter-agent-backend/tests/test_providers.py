"""Model providers: presets, keyless local servers, Azure, and parameters newer models refuse."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from app.models.schemas import AIChatMessage, AIModelConfig
from app.services.ai.adapters.factory import AIAdapterFactory
from app.services.ai.adapters.openai_adapter import AzureOpenAIAdapter, OpenAIAdapter
from app.services.ai.providers import BY_KEY, PROVIDERS, catalog, needs_key


def test_every_preset_is_complete():
    assert len(PROVIDERS) >= 20 and len({p.key for p in PROVIDERS}) == len(PROVIDERS)
    for provider in PROVIDERS:
        assert provider.adapter_type in ('openai_compatible', 'anthropic', 'azure_openai')
        assert provider.api_base or provider.base_editable or provider.adapter_type == 'anthropic', provider.key
        assert provider.group in ('global', 'china', 'local', 'custom')
    keys = {p['key'] for p in catalog()}
    assert {'openai', 'anthropic', 'gemini', 'deepseek', 'qwen', 'moonshot', 'zhipu', 'doubao', 'ollama',
            'azure_openai'} <= keys


def test_local_servers_need_no_key():
    assert not needs_key({'provider': 'ollama', 'api_base': 'http://192.168.1.5:11434/v1'})
    assert not needs_key({'provider': 'custom', 'api_base': 'http://localhost:8000/v1'})
    assert needs_key({'provider': 'custom', 'api_base': 'https://llm.example.com/v1'})
    assert needs_key({'provider': 'openai', 'api_base': 'https://api.openai.com/v1'})


def make(config, monkeypatch):
    monkeypatch.setattr('app.services.ai.adapters.factory.get_all_model_configs', lambda: [config])
    AIAdapterFactory._adapters.pop(config['id'], None)
    try:
        return AIAdapterFactory.create(config['id'])
    finally:
        AIAdapterFactory._adapters.pop(config['id'], None)


def test_factory_builds_a_keyless_local_model(monkeypatch):
    adapter = make({'id': 'local', 'name': 'x', 'model': 'qwen3', 'api_key': '', 'provider': 'ollama',
                    'api_base': 'http://127.0.0.1:11434/v1', 'adapter_type': 'openai_compatible'}, monkeypatch)
    assert isinstance(adapter, OpenAIAdapter) and adapter.api_key == 'not-needed'


def test_factory_refuses_a_remote_model_without_a_key(monkeypatch):
    with pytest.raises(ValueError, match='API key'):
        make({'id': 'r', 'name': 'x', 'model': 'm', 'api_key': '', 'provider': 'openai',
              'api_base': 'https://api.openai.com/v1', 'adapter_type': 'openai_compatible'}, monkeypatch)


def test_factory_builds_azure_with_its_api_version(monkeypatch):
    adapter = make({'id': 'az', 'name': 'x', 'model': 'my-deployment', 'api_key': 'k', 'provider': 'azure_openai',
                    'api_base': 'https://res.openai.azure.com', 'adapter_type': 'azure_openai',
                    'api_version': '2025-01-01-preview'}, monkeypatch)
    assert isinstance(adapter, AzureOpenAIAdapter)
    assert adapter.client._api_version == '2025-01-01-preview'


def test_model_config_accepts_the_new_fields():
    config = AIModelConfig(id='g', name='Gemini', model='gemini-2.5-flash', adapter_type='azure_openai',
                           provider='azure_openai', api_version='2024-10-21')
    assert config.api_key == ''
    with pytest.raises(ValueError):
        AIModelConfig(id='g', name='x', model='m', adapter_type='unknown')


def refusal(message):
    return openai.BadRequestError(message, body=None, response=httpx.Response(
        400, request=httpx.Request('POST', 'https://model.example/v1/chat/completions')))


@pytest.mark.asyncio
async def test_reasoning_models_get_max_completion_tokens_and_their_default_temperature():
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='hi', tool_calls=None))], usage=None)
    create = AsyncMock(side_effect=[
        refusal("Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."),
        refusal("Unsupported value: 'temperature' does not support 0.2 with this model. Only the default (1) value is supported."),
        reply, reply])
    adapter = object.__new__(OpenAIAdapter)
    adapter.model = 'reasoner'
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    message = [AIChatMessage(role='user', content='hello')]
    assert await adapter.chat(message) == 'hi'
    assert await adapter.chat(message) == 'hi'
    last = create.await_args_list[-1].kwargs
    assert 'max_tokens' not in last and last['max_completion_tokens'] == 8000 and 'temperature' not in last
    assert create.await_count == 4  # The second request went right the first time.


@pytest.mark.asyncio
async def test_an_unrelated_bad_request_is_not_retried():
    create = AsyncMock(side_effect=[refusal('messages: content is too long')])
    adapter = object.__new__(OpenAIAdapter)
    adapter.model = 'm'
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(openai.BadRequestError):
        await adapter.chat([AIChatMessage(role='user', content='x')])
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_the_providers_endpoint_lists_the_presets():
    from app.api.model_routes import get_providers
    assert any(p['key'] == 'gemini' and p['models'] for p in await get_providers())
