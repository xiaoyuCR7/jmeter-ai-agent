"""Native tool-call IDs and structured observations survive every boundary."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.models.schemas import AIChatMessage


@pytest.mark.asyncio
async def test_manual_model_probe_has_small_output_and_no_sdk_retry(monkeypatch):
    from app.api import routes
    adapter = SimpleNamespace(chat=AsyncMock(return_value='hello'))
    monkeypatch.setattr(routes.AIAdapterFactory, 'get_model_config', lambda _: {'id': 'fixture'})
    monkeypatch.setattr(routes.AIAdapterFactory, 'create', lambda _: adapter)
    result = await routes.test_model_config('fixture')
    assert result == {'success': True, 'message': '连接成功', 'response': 'hello'}
    assert adapter.chat.call_args.kwargs.get('max_tokens', 8000) <= 32
    assert adapter.chat.call_args.kwargs.get('max_retries') == 0


@pytest.mark.asyncio
async def test_manual_model_probe_times_out_and_cancels_request(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from app.api import routes
    cancelled = asyncio.Event()
    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(routes, 'HEALTH_CHECK_TIMEOUT_SECONDS', .01, raising=False)
    monkeypatch.setattr(routes.AIAdapterFactory, 'get_model_config', lambda _: {'id': 'fixture'})
    monkeypatch.setattr(routes.AIAdapterFactory, 'create', lambda _: SimpleNamespace(chat=blocked))
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(routes.test_model_config('fixture'), .5)
    assert error.value.status_code == 400 and cancelled.is_set()


@pytest.mark.asyncio
async def test_manual_model_probe_does_not_echo_provider_secrets(monkeypatch):
    from fastapi import HTTPException
    from app.api import routes
    monkeypatch.setattr(routes.AIAdapterFactory, 'get_model_config', lambda _: {'id': 'fixture'})
    monkeypatch.setattr(routes.AIAdapterFactory, 'create', lambda _: SimpleNamespace(
        chat=AsyncMock(side_effect=RuntimeError('private-key-in-provider-error'))))
    with pytest.raises(HTTPException) as error:
        await routes.test_model_config('fixture')
    assert error.value.status_code == 400 and 'RuntimeError' in error.value.detail
    assert 'private-key' not in error.value.detail


def tool_exchange():
    return [AIChatMessage(role='user', content='read'),
            AIChatMessage(role='assistant', content='', tool_calls=[{
                'id': 'call-A', 'type': 'function',
                'function': {'name': 'read_file', 'arguments': '{"path":"safe"}'}}]),
            AIChatMessage(role='tool', content='{"success":true}', tool_call_id='call-A')]


@pytest.mark.asyncio
async def test_text_fallback_checks_final_prompt_budget_and_keeps_tool_groups():
    from app.services.ai.adapters.base import BaseAIAdapter
    from app.services.ai.adapters.deepseek_adapter import DeepSeekAdapter
    adapter = object.__new__(DeepSeekAdapter)
    adapter.max_tokens = 8000
    adapter.chat = AsyncMock(return_value='done')
    history = tool_exchange()
    history[-1].content = 'large observation ' * 1000
    task = AIChatMessage(role='user', content='CURRENT_TASK', pinned=True)
    await BaseAIAdapter.chat_with_tools(adapter, [AIChatMessage(role='system', content='policy'), *history, task],
        [{'function': {'name': 'read', 'parameters': {'type': 'object'}}}],
        max_tokens=100, context_window_tokens=1600)
    sent = adapter.chat.call_args.args[0]
    cost = sum(adapter.estimate_tokens(json.dumps(m.to_wire(), ensure_ascii=False)) + 8 for m in sent)
    assert cost + 100 + 256 <= 1600
    assert any(m.content == 'CURRENT_TASK' for m in sent)
    assert not any('call-A' in m.content for m in sent)  # Entire old exchange is evicted together.


@pytest.mark.asyncio
async def test_text_fallback_rejects_oversized_tool_policy_without_model_call():
    from app.services.ai.adapters.base import BaseAIAdapter
    from app.services.ai.adapters.deepseek_adapter import DeepSeekAdapter
    adapter = object.__new__(DeepSeekAdapter)
    adapter.max_tokens = 8000
    adapter.chat = AsyncMock()
    with pytest.raises(ValueError, match='context budget'):
        await BaseAIAdapter.chat_with_tools(adapter, [AIChatMessage(role='user', content='task', pinned=True)],
            [{'function': {'name': 'test', 'description': 'huge ' * 2000}}],
            max_tokens=100, context_window_tokens=1000)
    adapter.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_openai_sends_native_call_and_tool_result_ids():
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    adapter = OpenAIAdapter('test-only', 'test')
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='done', tool_calls=[]))]))
    adapter.client.chat.completions.create = create
    await adapter.chat_with_tools(tool_exchange(), [])
    sent = create.call_args.kwargs['messages']
    assert sent[1]['tool_calls'][0]['id'] == 'call-A'
    assert sent[2]['role'] == 'tool' and sent[2]['tool_call_id'] == 'call-A'
    await adapter.client.close()


@pytest.mark.asyncio
async def test_anthropic_translates_calls_and_observations_to_content_blocks():
    from app.services.ai.adapters.anthropic_adapter import AnthropicAdapter
    adapter = AnthropicAdapter('test-only', 'test')
    create = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type='text', text='done')]))
    adapter.client.messages.create = create
    await adapter.chat_with_tools(tool_exchange(), [])
    sent = create.call_args.kwargs['messages']
    assert sent[1]['content'] == [{'type': 'tool_use', 'id': 'call-A', 'name': 'read_file', 'input': {'path': 'safe'}}]
    assert sent[2]['content'][0]['type'] == 'tool_result'
    assert sent[2]['content'][0]['tool_use_id'] == 'call-A'
    await adapter.client.close()


def test_memory_roundtrip_preserves_tool_message_fields():
    from app.services.ai.agent.memory import ConversationMemory
    memory = ConversationMemory()
    for message in tool_exchange():
        memory.add(message)
    restored = ConversationMemory.from_dict(memory.to_dict())
    assert restored.messages[1].tool_calls[0]['id'] == 'call-A'
    assert restored.messages[2].tool_call_id == 'call-A'


def test_tool_results_remain_json_objects_and_keep_resource_ids():
    from app.services.ai.agent.types import ToolResult
    result = ToolResult(True, data={'log': 'x' * 10000, 'execution_id': 'run-17', 'jmx_path': '/plans/run.jmx'})
    payload = json.loads(result.to_json())
    assert isinstance(payload['data'], dict)
    assert payload['data']['execution_id'] == 'run-17'
    assert payload['data']['jmx_path'] == '/plans/run.jmx'
    assert len(result.to_json()) < 3000


def test_truncation_does_not_leave_orphan_tool_results():
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    adapter = OpenAIAdapter('test-only', 'test')
    messages = [AIChatMessage(role='system', content='policy'), *tool_exchange(), AIChatMessage(role='user', content='next')]
    messages[1].content = 'large old text ' * 600
    selected = adapter._truncate_messages(messages, max_tokens=800)
    known = {call['id'] for message in selected for call in (getattr(message, 'tool_calls', None) or [])}
    assert all(message.tool_call_id in known for message in selected if message.role == 'tool')


def test_truncation_preserves_pinned_task_or_fails_explicitly():
    from app.services.ai.adapters.base import BaseAIAdapter
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    adapter = object.__new__(OpenAIAdapter)
    adapter.max_tokens = 8000
    task = AIChatMessage(role='user', content='CURRENT_TASK', pinned=True)
    messages = [AIChatMessage(role='system', content='policy'), task,
                AIChatMessage(role='user', content='old result' * 500), AIChatMessage(role='user', content='step')]
    assert task in adapter._truncate_messages(messages, max_tokens=800)
    with pytest.raises(ValueError, match='context budget'):
        adapter._truncate_messages([AIChatMessage(role='system', content='oversized' * 1000), task], max_tokens=800)


@pytest.mark.parametrize('payload', [
    {'tool_call': {'name': 'test', 'arguments': {'nested': {'key': ['a', {'b': 2}]}}}},
    {'tool_calls': [{'name': 'test', 'arguments': {'nested': [[], {'b': 2}]}}]},
])
def test_text_tool_fallback_preserves_nested_json(payload):
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    adapter = object.__new__(OpenAIAdapter)
    result = adapter._parse_tool_calls_from_text('Proceed\n```json\n' + json.dumps(payload) + '\n```')
    expected = payload.get('tool_call') or payload['tool_calls'][0]
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments == expected['arguments']
    assert 'tool_call' not in result.text


def test_memory_compaction_keeps_native_exchange_atomic():
    from app.services.ai.agent.memory import ConversationMemory
    memory = ConversationMemory(summary_keep=2)
    for message in [*tool_exchange(), AIChatMessage(role='user', content='next')]:
        memory.add(message)
    old, recent = memory.prepare_for_compression()
    assert recent[0].role != 'tool'
    assert len(recent) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai', 'deepseek', 'qwen'])
@pytest.mark.parametrize('ending', ['complete', 'close', 'error'])
async def test_model_stream_always_closes_response(provider, ending):
    from importlib import import_module
    cls = getattr(import_module(f'app.services.ai.adapters.{provider}_adapter'),
                  {'openai': 'OpenAIAdapter', 'deepseek': 'DeepSeekAdapter', 'qwen': 'QwenAdapter'}[provider])
    class Stream:
        def __init__(self):
            self.close = AsyncMock()
        async def __aiter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='one'))])
            if ending == 'error':
                raise RuntimeError('stream error')
    stream = Stream()
    adapter = object.__new__(cls)
    adapter.model = 'fixture'
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=stream))))
    iterator = adapter.chat_stream(tool_exchange())
    assert await anext(iterator) == 'one'
    if ending == 'close':
        await iterator.aclose()
    elif ending == 'error':
        with pytest.raises(RuntimeError, match='stream error'):
            await anext(iterator)
    else:
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
    stream.close.assert_awaited_once()
    if provider != 'openai':
        messages = adapter.client.chat.completions.create.call_args.kwargs['messages']
        assert messages[-1]['role'] == 'user'
        assert 'call-A' in messages[-1]['content']


@pytest.mark.asyncio
async def test_factory_invalidation_waits_for_active_call_before_closing(monkeypatch):
    import asyncio
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    from app.services.ai.adapters.factory import AIAdapterFactory
    adapter = object.__new__(OpenAIAdapter)
    adapter.model = 'fixture'
    entered, release = asyncio.Event(), asyncio.Event()
    async def complete(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='done'))])
    close = AsyncMock()
    adapter.client = SimpleNamespace(close=close, chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    monkeypatch.setattr(AIAdapterFactory, '_adapters', {'fixture': adapter})
    monkeypatch.setattr(AIAdapterFactory, '_retired', set(), raising=False)
    task = asyncio.create_task(adapter.chat([AIChatMessage(role='user', content='hello')]))
    await entered.wait()
    try:
        await AIAdapterFactory.invalidate('fixture')
        close.assert_not_awaited()
        assert 'fixture' not in AIAdapterFactory._adapters
    finally:
        release.set()
        await task
    close.assert_awaited_once()
    with pytest.raises(RuntimeError, match='configuration'):
        await adapter.chat([])
    await AIAdapterFactory.close_all()
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_factory_closes_idle_clients_and_bounds_health_checks(monkeypatch):
    import asyncio
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    import app.services.ai.adapters.factory as module
    adapter = object.__new__(OpenAIAdapter)
    adapter.client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(module.AIAdapterFactory, '_adapters', {'fixture': adapter})
    monkeypatch.setattr(module.AIAdapterFactory, '_retired', set(), raising=False)
    await module.AIAdapterFactory.close_all()
    adapter.client.close.assert_awaited_once()
    cancelled = asyncio.Event()
    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(module, 'HEALTH_CHECK_TIMEOUT_SECONDS', .01, raising=False)
    monkeypatch.setattr(module.AIAdapterFactory, 'create', lambda _: SimpleNamespace(chat=blocked))
    result = await asyncio.wait_for(module.AIAdapterFactory.health_check('fixture'), .5)
    assert not result['healthy'] and cancelled.is_set()


def test_a_four_item_requirements_draft_reaches_the_model_whole():
    # Regression: each dict value was capped at 600 chars, so the 4th requirement never arrived.
    from app.services.ai.agent.types import ToolResult
    items = [{'id': f'R{i}', 'scope': 'transaction', 'transaction': f'POST /api/endpoint-{i}', 'metric': 'p95',
              'operator': 'lte', 'value': 800, 'source': f'接口 {i} 的 P95 不超过 800 ms', 'source_verified': True}
             for i in range(1, 5)]
    draft = {'items': items, 'load_profile': {'concurrent_users': 10, 'duration_seconds': 60}, 'assumptions': []}
    result = ToolResult(True, data={'project_id': 'p1', 'draft': draft, 'saved': False, 'note': 'Draft only.'})
    payload = json.loads(result.to_json())
    assert [item['id'] for item in payload['data']['draft']['items']] == ['R1', 'R2', 'R3', 'R4']
    assert payload['data']['draft']['items'][3]['source'] == '接口 4 的 P95 不超过 800 ms'
