"""Per-model context windows: reported by the provider, else documented, else the global default."""
import httpx
import pytest

from app.services.ai import model_limits


def provider(routes):
    def handler(request):
        for path, body in routes.items():
            if request.url.path == path:
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_openai_compatible_listing_reports_the_exact_models_window():
    # Shape returned by DeepSeek's /models today.
    transport = provider({'/models': {'data': [
        {'id': 'deepseek-flash', 'context_window': 1048576},
        {'id': 'some-other', 'context_window': 8192}]}})
    config = {'model': 'deepseek-flash', 'api_key': 'k', 'api_base': 'https://api.example.test'}
    assert await model_limits.detect_context_window(config, transport=transport) == 1048576


@pytest.mark.asyncio
async def test_anthropic_models_api_reports_max_input_tokens():
    transport = provider({'/v1/models/claude-opus-5-5': {'id': 'claude-opus-5-5', 'max_input_tokens': 1000000}})
    config = {'model': 'claude-opus-5-5', 'api_key': 'k', 'adapter_type': 'anthropic'}
    assert await model_limits.detect_context_window(config, transport=transport) == 1000000


@pytest.mark.asyncio
async def test_unreported_size_falls_back_to_documented_value_then_to_none():
    silent = provider({'/v1/models': {'data': [{'id': 'qwen-max'}]}})
    known = {'model': 'qwen-max', 'api_key': 'k', 'api_base': 'https://dash.example.test/v1'}
    assert await model_limits.detect_context_window(known, transport=silent) == 32768
    down = httpx.MockTransport(lambda request: httpx.Response(500))
    unknown = {'model': 'my-local-model', 'api_key': 'k', 'api_base': 'http://llm.example.test/v1'}
    assert await model_limits.detect_context_window(unknown, transport=down) is None


def test_adapter_budget_follows_the_configured_window(monkeypatch):
    from app.core.config import settings
    from app.models.schemas import AIChatMessage
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', 16384)
    adapter = OpenAIAdapter(api_key='k', model='m', api_base='https://api.example.test/v1')
    big = [AIChatMessage(role='user', content='x' * 60000)]  # ~21K estimated tokens
    with pytest.raises(ValueError, match='context budget'):
        adapter.prepare_chat_messages(big, response_tokens=2000)
    adapter.set_context_window(1048576)
    assert adapter.context_budget == 1048576
    assert adapter.prepare_chat_messages(big, response_tokens=2000) == big
    adapter.set_context_window(None)  # an empty value keeps what is set
    assert adapter.context_budget == 1048576


@pytest.mark.asyncio
async def test_saving_a_model_keeps_a_manual_window_and_fills_a_missing_one(monkeypatch):
    from app.api import routes
    calls = []

    async def detect(config):
        calls.append(config['model'])
        return 1048576
    monkeypatch.setattr(model_limits, 'detect_context_window', detect)
    manual = await routes._fill_context_window({'model': 'a', 'api_key': 'k', 'context_window': 65536})
    filled = await routes._fill_context_window({'model': 'b', 'api_key': 'k', 'context_window': None})
    redacted = await routes._fill_context_window({'model': 'c', 'api_key': '***', 'context_window': None})
    assert (manual['context_window'], filled['context_window'], redacted['context_window']) == (65536, 1048576, None)
    assert calls == ['b']
