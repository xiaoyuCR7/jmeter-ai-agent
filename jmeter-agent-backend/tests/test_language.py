"""Messages the backend writes follow the language of the person reading them."""
import asyncio

import pytest

from app.core import text
from app.core.config import settings
from app.core.text import RequestLanguageMiddleware, current_lang, language_of, normalize_lang, tr
from app.services.jmeter import component_registry, function_registry, jmx_lint, run_guard
from app.services.jmeter.registry_en import EN, TRANSLATED_KEYS, has_chinese, localize


@pytest.fixture
def english():
    token = text.set_lang('en')
    yield
    text.reset_lang(token)


def test_the_default_language_decides_when_no_request_says(monkeypatch):
    assert current_lang() == 'zh-CN' and tr('中文', 'English') == '中文'
    monkeypatch.setattr(settings, 'I18N_DEFAULT_LANG', 'en')
    assert current_lang() == 'en' and tr('中文', 'English') == 'English'
    assert tr('中文', 'English', lang='zh') == '中文'  # An explicit language wins.


@pytest.mark.parametrize('value, expected', [
    ('en', 'en'), ('en-US', 'en'), ('EN_gb', 'en'), ('english', 'en'), ('zh', 'zh-CN'), ('zh-CN', 'zh-CN'),
    ('zh-TW', 'zh-CN'), ('fr', None), ('', None), (None, None), (3, None)])
def test_language_spellings(value, expected):
    assert normalize_lang(value) == expected


def test_a_request_names_its_language_by_parameter_or_header():
    def scope(query=b'', header=None):
        return {'type': 'http', 'query_string': query,
                'headers': [(b'accept-language', header)] if header else []}
    assert language_of(scope(b'lang=en')) == 'en'
    assert language_of(scope(b'offset=0&lang=zh-CN', b'en-US,en;q=0.9')) == 'zh-CN'  # The parameter wins.
    assert language_of(scope(header=b'en-US,en;q=0.9,zh;q=0.8')) == 'en'
    assert language_of(scope(header=b'zh-CN,zh;q=0.9')) == 'zh-CN'
    assert language_of(scope(header=b'fr-FR')) is None and language_of(scope()) is None


@pytest.mark.asyncio
async def test_work_a_request_starts_keeps_its_language():
    seen = {}

    async def app(scope, receive, send):
        seen['during'] = tr('中文', 'English')

        async def later():
            await asyncio.sleep(0)
            seen['background'] = tr('中文', 'English')
        seen['task'] = asyncio.create_task(later())
    await RequestLanguageMiddleware(app)({'type': 'http', 'query_string': b'lang=en', 'headers': []}, None, None)
    await seen['task']
    assert seen['during'] == 'English' and seen['background'] == 'English'
    assert tr('中文', 'English') == '中文'  # The request's language does not leak past it.


def _labels(data):
    found = []

    def walk(node, key=None):
        if isinstance(node, dict):
            for name, item in node.items():
                walk(item, name)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif key in TRANSLATED_KEYS and has_chinese(node):
            found.append(node)
    walk(data)
    return found


def test_every_registry_label_has_an_english_wording():
    chinese = _labels(component_registry.get_all_components()) + _labels(function_registry.get_all_functions())
    assert len(chinese) > 400
    missing = sorted({label for label in chinese if label not in EN})
    assert missing == [], f'Add these to registry_en.EN: {missing}'
    assert not [value for value in EN.values() if has_chinese(value)]


def test_registries_are_served_in_the_readers_language():
    components = component_registry.get_all_components()
    english_components = localize(components, 'en')
    assert _labels(english_components) == [] and _labels(localize(function_registry.get_all_functions(), 'en')) == []
    by_class = {item['testclass']: item for item in english_components}
    assert by_class['ThreadGroup']['label'] == 'Thread Group'
    assert by_class['HTTPSamplerProxy']['label'] == 'HTTP Request'
    assert by_class['ThreadGroup']['properties']['ThreadGroup.num_threads']['label'] == 'Number of Threads (users)'
    # Everything that is not wording is untouched, and the registry itself is not changed.
    original = {item['testclass']: item for item in components}['ThreadGroup']
    assert by_class['ThreadGroup']['allowed_children'] == original['allowed_children']
    assert by_class['ThreadGroup']['defaults'] == original['defaults']
    assert original['label'] == '线程组' and localize(components, 'zh-CN') is components


@pytest.mark.asyncio
async def test_the_registry_routes_follow_the_request(english):
    from app.api import system_routes
    assert (await system_routes.get_components())[0]['label'] == 'Test Plan'
    assert _labels(await system_routes.get_functions()) == []
    assert _labels(await system_routes.get_component_children('TestPlan')) == []


def test_plan_checks_and_guard_reasons_read_in_english(english, monkeypatch):
    issues = jmx_lint.lint('<jmeterTestPlan><hashTree><TestPlan/><hashTree/></hashTree></jmeterTestPlan>')
    assert issues and not [issue for issue in issues if has_chinese(issue['message'])]
    assert any(issue['code'] == 'no_thread_group' and 'no enabled thread group' in issue['message'] for issue in issues)

    monkeypatch.setattr(settings, 'RUN_GUARD_MEMORY_PERCENT', 97)
    reason = run_guard.judge_machine(99)
    assert 'load generator' in reason and not has_chinese(reason)


def test_capacity_reasons_read_in_english(english):
    from app.services.experiments import CapacityStep, capacity_steps, judge_step
    previous = CapacityStep(threads=10, throughput=100, error_rate=0, requirements='pass')
    reasons = [
        judge_step(CapacityStep(threads=20, throughput=105, error_rate=0, requirements='pass'), previous, 1.0),
        judge_step(CapacityStep(threads=20, throughput=200, error_rate=4, requirements='pass'), previous, 1.0),
        judge_step(CapacityStep(threads=20, throughput=200, error_rate=0, requirements='fail'), previous, 1.0),
        judge_step(CapacityStep(threads=20, throughput=200, generator_saturated=True), previous, 1.0)]
    assert all(reason and not has_chinese(reason) for reason in reasons)
    assert 'Throughput no longer grows' in reasons[0] and 'saturated' in reasons[3]
    with pytest.raises(ValueError, match='at least 1 thread'):
        capacity_steps(0, 10, 2)
