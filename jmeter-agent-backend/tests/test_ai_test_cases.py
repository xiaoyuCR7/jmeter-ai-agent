"""What generated test cases must get right: they load, they assert, and they load the system as asked."""
import json
import os
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.models.schemas import (APIEndpoint, CsvDataFile, ExtractionRule, ResponseCheck, TestConfig as LoadConfig,
                                VariableBinding)
from app.services.ai.adapters.factory import AIAdapterFactory
from app.services.jmeter import jmx_lint
from app.services.jmeter.jmx_generator import JMXGenerator, load_shape

BASE = 'https://shop.invalid'
LOGIN = APIEndpoint(name='Login', method='POST', path='/login', body={'u': 'demo'}, phase='once_per_user',
                    extractions=[ExtractionRule(ref_name='auth_token', extract_type='json_path', expression='$.token')])
BEARER = VariableBinding(variable_name='auth_token', target_location='header', target_key='Authorization',
                         template='Bearer ${auth_token}')
ORDERS = APIEndpoint(name='Orders', method='GET', path='/orders', depends_on=['Login'], bindings=[BEARER])


def build(*endpoints, **config):
    return JMXGenerator().generate_basic_jmx(list(endpoints), LoadConfig(**{'test_type': 'baseline', **config}), BASE)


def codes(content):
    return {issue['code'] for issue in jmx_lint.lint(content)}


def error_codes(content):
    return {issue['code'] for issue in jmx_lint.errors(jmx_lint.lint(content))}


@pytest.mark.parametrize('path', ['/api/login', '/api/authors', '/oauth/token'])
def test_auth_looking_paths_add_nothing_global(path):
    root = ET.fromstring(build(APIEndpoint(name='x', method='GET', path=path)))
    assert root.find('.//AuthManager') is None and 'Authorization' not in ET.tostring(root, encoding='unicode')


def test_only_bound_requests_carry_the_token():
    root = ET.fromstring(build(LOGIN, ORDERS, APIEndpoint(name='Public', method='GET', path='/public')))
    headers = [node.text for node in root.iter('stringProp') if node.get('name') == 'Header.value']
    assert headers.count('Bearer ${auth_token}') == 1


def test_status_assertion_uses_jmeters_spelling_and_decides_alone():
    root = ET.fromstring(build(APIEndpoint(name='x', method='GET', path='/gone', expected_status=404)))
    assertion = root.find('.//ResponseAssertion')
    assert assertion.find("collectionProp[@name='Asserion.test_strings']/stringProp").text == '404'
    assert assertion.find("boolProp[@name='Assertion.assume_success']").text == 'true'


def test_response_checks_become_json_assertions():
    root = ET.fromstring(build(APIEndpoint(name='x', method='GET', path='/a', response_checks=[
        ResponseCheck(json_path='$.code', expected='0'), ResponseCheck(json_path='$.data')])))
    first, second = root.findall('.//JSONPathAssertion')
    assert first.find("stringProp[@name='EXPECTED_VALUE']").text == '0'
    assert first.find("boolProp[@name='JSONVALIDATION']").text == 'true'
    assert second.find("boolProp[@name='JSONVALIDATION']").text == 'false'


def sampler_path(content, name):
    root = ET.fromstring(content)
    sampler = next(node for node in root.iter('HTTPSamplerProxy') if node.get('testname') == name)
    return sampler.find("stringProp[@name='HTTPSampler.path']").text


@pytest.mark.parametrize('path', ['/orders/{id}', '/orders/${id}'])
def test_path_binding_leaves_one_dollar(path):
    plan = build(APIEndpoint(name='Create', method='POST', path='/orders', extractions=[
        ExtractionRule(ref_name='id', extract_type='json_path', expression='$.id')]),
        APIEndpoint(name='Get', method='GET', path=path, depends_on=['Create'], bindings=[
            VariableBinding(variable_name='id', target_location='path', target_key='id', template='${id}')]))
    assert sampler_path(plan, 'Get') == '/orders/${id}'
    assert 'stray_dollar' not in codes(plan)


def test_once_per_user_keeps_the_session_and_setup_gets_its_own_group():
    root = ET.fromstring(build(LOGIN, ORDERS, APIEndpoint(name='Seed', method='POST', path='/seed', phase='setup')))
    once = root.find('.//OnceOnlyController')
    assert once.get('testname') == 'Once: Login'
    assert root.find('.//CookieManager/boolProp').text == 'false'
    setup = root.find('.//SetupThreadGroup')
    assert setup.find("stringProp[@name='ThreadGroup.num_threads']").text == '1'
    assert setup.find(".//intProp[@name='LoopController.loops']").text == '1'
    assert [node.get('testname') for node in root.iter('HTTPSamplerProxy')].count('Seed') == 1


@pytest.mark.parametrize('expected,written', [('*', None), ('', None), (0, '0'), (True, 'True'), ('ok', 'ok')])
def test_a_wildcard_means_the_field_only_has_to_exist(expected, written):
    assert ResponseCheck(json_path='$.a', expected=expected).expected == written


def test_adjacent_requests_of_one_weight_are_one_branch():
    root = ET.fromstring(build(APIEndpoint(name='Search', method='GET', path='/s'),
                               APIEndpoint(name='Pick', method='POST', path='/p', weight=40),
                               APIEndpoint(name='Pay', method='POST', path='/c', weight=40),
                               APIEndpoint(name='Home', method='GET', path='/h'),
                               APIEndpoint(name='Other', method='GET', path='/o', weight=40)))
    assert [node.get('testname') for node in root.iter('ThroughputController')] == ['40%: Pick', '40%: Other']


def test_weight_wraps_only_a_partial_request():
    root = ET.fromstring(build(APIEndpoint(name='Buy', method='POST', path='/buy', weight=20),
                               APIEndpoint(name='Browse', method='GET', path='/browse', weight=100)))
    controllers = root.findall('.//ThroughputController')
    assert len(controllers) == 1 and controllers[0].find('FloatProperty/value').text == '20.0'


@pytest.mark.parametrize('body,kind,expected', [
    ([{'a': 1}], 'application/json', '[{"a": 1}]'),
    ({'from': 'Boston', 'to': '${city}'}, 'application/x-www-form-urlencoded', 'from=Boston&to=${city}'),
    ('raw', 'text/plain', 'raw'),
])
def test_bodies_beyond_json_objects(body, kind, expected):
    root = ET.fromstring(build(APIEndpoint(name='x', method='POST', path='/a', body=body, content_type=kind)))
    assert root.find(".//stringProp[@name='Argument.value']").text == expected


def test_timeouts_have_defaults():
    defaults = ET.fromstring(build(APIEndpoint(name='x', method='GET', path='/a'))).find('.//ConfigTestElement')
    assert defaults.find("stringProp[@name='HTTPSampler.connect_timeout']").text == '5000'
    assert defaults.find("stringProp[@name='HTTPSampler.response_timeout']").text == '30000'


def test_rows_become_a_data_file_in_test_data():
    plan = build(APIEndpoint(name='x', method='GET', path='/a?u=${user}'), csv_data_files=[
        CsvDataFile(variables=['user', 'pass'], rows=[['a', '1'], ['b', '2']])])
    dataset = ET.fromstring(plan).find('.//CSVDataSet')
    path = dataset.find("stringProp[@name='filename']").text
    assert os.path.dirname(path) == os.path.join(settings.BASE_DIR, 'test_data')
    assert open(path, encoding='utf-8').read() == 'a,1\nb,2\n'
    assert dataset.find("stringProp[@name='variableNames']").text == 'user,pass'
    assert 'undefined_variable' not in codes(plan)


def test_a_named_data_file_must_exist():
    with pytest.raises(ValueError, match='not found'):
        build(APIEndpoint(name='x', method='GET', path='/a'), csv_data_files=[CsvDataFile(filename='nope.csv', variables=['a'])])


def shape(**config):
    return load_shape(LoadConfig(**config))


def test_each_test_type_has_its_own_load_shape():
    assert [(g.users, g.delay_seconds, g.duration_seconds) for g in shape(
        test_type='ramp_up', concurrent_users=30, step_users=10, step_duration=60)] == [
        (10, 0, 180), (10, 60, 120), (10, 120, 60)]
    stress = shape(test_type='stress', concurrent_users=100, duration_seconds=300)
    assert [g.users for g in stress] == [20] * 5 and sum(g.users for g in stress) == 100
    assert stress[0].duration_seconds == 300 and stress[-1].delay_seconds == 240
    base, burst = shape(test_type='spike', concurrent_users=100, duration_seconds=120, ramp_up_seconds=10)
    assert (base.users, base.duration_seconds) == (10, 120)
    assert (burst.users, burst.delay_seconds, burst.duration_seconds) == (90, 40, 40)
    assert shape(test_type='stability', stability_hours=4)[0].duration_seconds == 14400
    assert shape(test_type='stability', duration_seconds=900)[0].duration_seconds == 900
    assert len(shape(test_type='baseline')) == 1


def test_shapes_repeat_the_flow_with_correct_paths_and_keep_the_input():
    endpoint = APIEndpoint(name='a', method='GET', path='/a')
    plan = JMXGenerator().generate_basic_jmx([endpoint], LoadConfig(test_type='stress', concurrent_users=10, duration_seconds=50),
                                             BASE + '/v1')
    paths = {node.text for node in ET.fromstring(plan).iter('stringProp') if node.get('name') == 'HTTPSampler.path' and node.text}
    assert paths == {'/v1/a'} and endpoint.path == '/a'
    assert len(ET.fromstring(plan).findall('.//ThreadGroup')) == 5 and error_codes(plan) == set()


def plan_with(*replacements):
    plan = build(LOGIN, ORDERS)
    for old, new in replacements:
        plan = plan.replace(old, new)
    return plan


def test_a_generated_plan_is_clean():
    assert codes(build(LOGIN, ORDERS)) == set()


@pytest.mark.parametrize('replacements,code', [
    ([('Asserion.test_strings', 'Assertion.test_strings')], 'dead_assertion'),
    ([('ref_name', 'ref_name'), ('JSONPostProcessor.referenceNames">auth_token', 'JSONPostProcessor.referenceNames">other')], 'undefined_variable'),
    ([('<stringProp name="HTTPSampler.path">/orders', '<stringProp name="HTTPSampler.path">/orders/{id}')], 'unresolved_path'),
    ([('<stringProp name="HTTPSampler.path">/orders', '<stringProp name="HTTPSampler.path">/orders/$${id}')], 'stray_dollar'),
])
def test_lint_catches_plans_that_only_look_right(replacements, code):
    assert code in error_codes(plan_with(*replacements))


def test_lint_catches_what_jmeter_cannot_load_or_run():
    assert error_codes('<nope') == {'invalid_xml'}
    root = ET.fromstring(build(ORDERS.model_copy(update={'bindings': [], 'depends_on': []})))
    hash_tree = root.find('hashTree/hashTree')
    hash_tree.remove(list(hash_tree)[1])
    assert 'broken_tree' in error_codes(ET.tostring(root, encoding='unicode'))


def test_lint_catches_argument_type_that_fails_every_request():
    plan = build(APIEndpoint(name='x', method='POST', path='/a', body={'a': 1})).replace(
        'elementType="HTTPArgument"', 'elementType="Argument"')
    assert 'wrong_argument' in error_codes(plan)


def test_lint_warns_about_repeated_login_and_missing_assertions():
    plan = build(LOGIN.model_copy(update={'phase': 'iteration'}), ORDERS)
    assert 'repeated_login' in codes(plan) and not error_codes(plan)
    root = ET.fromstring(build(APIEndpoint(name='x', method='GET', path='/a')))
    for parent in root.iter():
        for child in list(parent):
            if child.tag == 'ResponseAssertion':
                following = list(parent)[list(parent).index(child) + 1]
                parent.remove(child)
                parent.remove(following)
    assert 'no_assertion' in codes(ET.tostring(root, encoding='unicode'))


def test_a_variable_from_a_later_request_is_not_available():
    plan = build(ORDERS.model_copy(update={'order_hint': 0, 'depends_on': []}), LOGIN.model_copy(update={'order_hint': 1}))
    assert 'undefined_variable' in error_codes(plan)


def test_jmeter_functions_and_scripts_are_not_undefined_variables():
    assert 'undefined_variable' not in codes(build(APIEndpoint(name='x', method='POST', path='/a', body={'u': 'user_${__UUID()}'})))


class Replies:
    def __init__(self, *replies):
        self.chat = AsyncMock(side_effect=list(replies))

    def prepare_chat_messages(self, messages, response_tokens):
        return list(messages)


GOOD = json.dumps({'base_url': BASE, 'endpoints': [{'name': 'a', 'method': 'GET', 'path': '/a'}]})
BAD = json.dumps({'base_url': BASE, 'endpoints': [{'name': 'a', 'method': 'GET', 'path': '/orders/{id}'}]})


@pytest.mark.asyncio
async def test_the_model_gets_its_mistakes_back_and_fixes_them(monkeypatch):
    adapter = Replies('no json here', BAD, GOOD)
    monkeypatch.setattr(AIAdapterFactory, 'create', lambda *args: adapter)
    plan = await JMXGenerator().generate_from_ai('doc', [], LoadConfig(test_type='baseline'))
    assert sampler_path(plan, 'a') == '/a' and adapter.chat.await_count == 3
    assert '{id}' in adapter.chat.await_args.args[0][-1].content


@pytest.mark.asyncio
async def test_a_model_that_never_gets_it_right_produces_no_plan(monkeypatch):
    adapter = Replies(BAD, BAD, BAD)
    monkeypatch.setattr(AIAdapterFactory, 'create', lambda *args: adapter)
    with pytest.raises(ValueError, match='valid plan'):
        await JMXGenerator().generate_from_ai('doc', [], LoadConfig(test_type='baseline'))


@pytest.mark.asyncio
async def test_create_jmx_saves_nothing_when_the_plan_is_wrong(monkeypatch):
    from app.services.ai.agent.tools import jmx
    saved = []
    monkeypatch.setattr(JMXGenerator, 'save_jmx', lambda self, *args, **kwargs: saved.append(args))
    result = await jmx.create_jmx('x', [{'name': 'a', 'method': 'GET', 'path': '/orders/{id}'}], BASE)
    assert 'not created' in result['error'] and result['issues'][0]['code'] == 'unresolved_path' and saved == []


@pytest.mark.asyncio
async def test_create_jmx_refuses_what_jmeter_refuses(monkeypatch):
    from app.services.ai.agent.tools import jmx
    monkeypatch.setattr(jmx_lint, 'load_check', lambda content: {'checked': True, 'ok': False, 'error': 'Problem loading XML'})
    result = await jmx.create_jmx('x', [{'name': 'a', 'method': 'GET', 'path': '/a'}], BASE)
    assert 'Problem loading XML' in result['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('test_type,expected', [('baseline', 1000), ('stress', 0)])
async def test_create_jmx_passes_the_new_controls_through(monkeypatch, test_type, expected):
    from app.services.ai.agent.tools import jmx
    configs = []
    original = JMXGenerator.generate_basic_jmx

    def spy(self, endpoints, test_config, base_url, test_name='x'):
        configs.append(test_config)
        return original(self, endpoints, test_config, base_url, test_name)
    monkeypatch.setattr(JMXGenerator, 'generate_basic_jmx', spy)
    monkeypatch.setattr(JMXGenerator, 'save_jmx', lambda self, *args, **kwargs: 'x.jmx')
    from app.services import execution_lifecycle
    from types import SimpleNamespace
    monkeypatch.setattr(execution_lifecycle, 'execution_lifecycle',
                        SimpleNamespace(register_path=AsyncMock(return_value=SimpleNamespace(id='p'))))
    await jmx.create_jmx('x', [{'name': 'a', 'method': 'GET', 'path': '/a', 'phase': 'setup', 'weight': 50,
                                'response_checks': [{'json_path': '$.ok'}]}], BASE, test_type=test_type, step_users=2)
    assert configs[0].think_time_ms == expected and configs[0].step_users == 2


@pytest.mark.asyncio
async def test_generate_route_saves_nothing_jmeter_cannot_load(monkeypatch):
    from fastapi import HTTPException
    from app.api import routes
    from app.models.schemas import GenerateTestPlanRequest, ProjectInfo
    from app.services.storage import store
    monkeypatch.setattr(store, '_projects', {'p': ProjectInfo(id='p', name='P')})
    monkeypatch.setattr(jmx_lint, 'load_check', lambda content: {'checked': True, 'ok': False, 'error': 'Problem loading XML'})
    monkeypatch.setattr(JMXGenerator, 'save_jmx', lambda *args, **kwargs: pytest.fail('saved'))
    request = GenerateTestPlanRequest(api_doc='d', base_url=BASE, test_config=LoadConfig(test_type='baseline'),
                                      endpoints=[APIEndpoint(name='a', method='GET', path='/a')])
    with pytest.raises(HTTPException) as caught:
        await routes.generate_test_plan(request, 'p')
    assert caught.value.status_code == 422


def test_reports_name_what_is_wrong_with_the_plan_that_ran(tmp_path):
    from app.services import evidence
    path = tmp_path / 'old.jmx'
    path.write_text(build(APIEndpoint(name='x', method='GET', path='/a')).replace('Asserion.', 'Assertion.'), encoding='utf-8')
    issues = evidence._plan_issues(str(path))
    assert issues[0]['code'] == 'dead_assertion' and '测试计划本身的问题' in evidence._format_plan_issues(issues)
    assert evidence._plan_issues(str(tmp_path / 'gone.jmx')) == []


def test_scenario_rules_reach_every_generation_path():
    from app.services.ai.agents.test_plan_agent import TEST_PLAN_SYSTEM_PROMPT
    from app.services.ai.agents.workflow_agent import WORKFLOW_SYSTEM_PROMPT
    from app.services.ai.prompts.jmx_generator import SCENARIO_RULES, SCENARIO_SYSTEM_PROMPT
    for prompt in (TEST_PLAN_SYSTEM_PROMPT, WORKFLOW_SYSTEM_PROMPT, SCENARIO_SYSTEM_PROMPT):
        assert SCENARIO_RULES in prompt
