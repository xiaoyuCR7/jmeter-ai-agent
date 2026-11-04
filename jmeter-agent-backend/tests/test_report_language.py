"""Reports in the reader's language, and Claude reports as one structured object."""
import json
from types import SimpleNamespace

import pytest

from app.models.schemas import AIChatMessage
from app.services.ai.adapters.anthropic_adapter import AnthropicAdapter, REPORT_TOOL
from app.services.ai import analyzer as analyzer_module
from app.services.ai.prompts.test_analyzer import REPORT_SCHEMA, build_analysis_prompt
from app.services.post_run import save_analysis_markdown
from app.core.lang_utils import report_lang


def test_the_prompt_asks_for_the_report_language():
    assert 'English' in build_analysis_prompt({'total_requests': 1}, {}, 'baseline', lang='en')
    assert '用中文分析' in build_analysis_prompt({'total_requests': 1}, {}, 'baseline')


def test_report_language_falls_back_to_the_setting(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'REPORT_LANGUAGE', 'en')
    assert report_lang() == 'en' and report_lang('zh') == 'zh-CN'
    monkeypatch.setattr(settings, 'REPORT_LANGUAGE', '')
    monkeypatch.setattr(settings, 'I18N_DEFAULT_LANG', 'zh-CN')
    assert report_lang('nonsense') == 'zh-CN'


class FakeAdapter:
    context_budget = 100_000
    max_tokens = 100_000

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def prepare_chat_messages(self, messages, response_tokens):
        return messages

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def analyzer(reply):
    instance = analyzer_module.TestAnalyzer.__new__(analyzer_module.TestAnalyzer)
    instance.adapter, instance._exchange = FakeAdapter(reply), []
    return instance


@pytest.mark.asyncio
async def test_an_english_report_is_asked_for_and_marked_english():
    reply = json.dumps({'overall_assessment': 'Fine.', 'conclusion': 'Passed.'})
    instance = analyzer(reply)
    result = await instance.analyze({'total_requests': 10}, {}, 'baseline', lang='en')
    messages, kwargs = instance.adapter.calls[0]
    assert 'English' in messages[0].content and kwargs['json_schema'] is REPORT_SCHEMA
    assert result.language == 'en' and result.analysis_status == 'completed'


@pytest.mark.asyncio
async def test_a_failed_english_report_says_so_in_english():
    instance = analyzer(ValueError('boom'))
    result = await instance.analyze({'total_requests': 10}, {}, 'baseline', lang='en')
    assert result.analysis_status == 'failed' and result.language == 'en'
    assert 'unavailable' in result.conclusion


def test_markdown_follows_the_report_language(tmp_path):
    analysis = {'language': 'en', 'overall_assessment': 'Fine.', 'conclusion': 'Passed.',
                'key_findings': ['P95 was 300 ms'], 'evidence_status': 'verified', 'fact_check': {'checked': 1}}
    text = open(save_analysis_markdown(str(tmp_path), 'run-1', analysis), encoding='utf-8').read()
    assert '# AI Performance Test Analysis' in text and '## Key findings' in text and 'match the measurements' in text


class FakeMessages:
    def __init__(self, content):
        self.content, self.requests = content, []

    async def create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(content=self.content, usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def claude(content, api_base=None):
    adapter = AnthropicAdapter('key', 'claude-test', api_base)
    adapter.client = SimpleNamespace(messages=FakeMessages(content), with_options=lambda **_: adapter.client)
    return adapter


@pytest.mark.asyncio
async def test_claude_fills_in_the_report_tool():
    adapter = claude([SimpleNamespace(type='tool_use', input={'overall_assessment': 'a', 'conclusion': 'b'})])
    reply = await adapter.chat([AIChatMessage(role='user', content='go')], json_mode=True, json_schema=REPORT_SCHEMA)
    request = adapter.client.messages.requests[0]
    assert request['tool_choice'] == {'type': 'tool', 'name': REPORT_TOOL}
    assert json.loads(reply) == {'overall_assessment': 'a', 'conclusion': 'b'}


@pytest.mark.asyncio
async def test_claude_caches_the_system_prompt_and_the_evidence():
    adapter = claude([SimpleNamespace(type='text', text='ok')])
    await adapter.chat([AIChatMessage(role='system', content='rules'), AIChatMessage(role='user', content='facts'),
                        AIChatMessage(role='assistant', content='answer'), AIChatMessage(role='user', content='more')])
    request = adapter.client.messages.requests[0]
    assert request['system'][0]['cache_control'] == {'type': 'ephemeral'}
    assert request['messages'][0]['content'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert 'cache_control' not in request['messages'][-1]['content'][-1]


@pytest.mark.asyncio
async def test_other_anthropic_compatible_endpoints_get_no_cache_markers():
    adapter = claude([SimpleNamespace(type='text', text='ok')], api_base='https://api.example.com/anthropic')
    await adapter.chat([AIChatMessage(role='system', content='rules'), AIChatMessage(role='user', content='facts')])
    assert adapter.client.messages.requests[0]['system'] == 'rules'
