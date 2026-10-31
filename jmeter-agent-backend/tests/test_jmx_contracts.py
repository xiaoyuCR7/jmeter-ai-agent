"""JMX property contracts shared by generation, tools, tuning and self-healing."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

import pytest

from app.models.schemas import APIEndpoint, ExtractionRule, TestConfig as LoadConfig, VariableBinding
from app.services.jmeter.jmx_generator import JMXGenerator
from app.services.jmeter.jmx_parser import parse_jmx


def walk(node):
    yield node
    for child in node.get('children', []):
        yield from walk(child)


@pytest.mark.asyncio
async def test_ai_jmx_request_reserves_output_before_model(monkeypatch):
    from app.core.config import settings
    from app.services.ai.adapters.deepseek_adapter import DeepSeekAdapter
    from app.services.ai.adapters.factory import AIAdapterFactory
    adapter = object.__new__(DeepSeekAdapter)
    adapter.max_tokens = 8000
    adapter.chat = AsyncMock(return_value='{"base_url":"https://example.invalid","endpoints":[{"name":"a","method":"GET","path":"/a"}]}')
    monkeypatch.setattr(AIAdapterFactory, 'create', lambda *args: adapter)
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', 8000)
    with pytest.raises(ValueError, match='context budget'):
        await JMXGenerator().generate_from_ai('GET /fixture', [], LoadConfig(test_type='baseline'))
    adapter.chat.assert_not_awaited()
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', 32000)
    plan = await JMXGenerator().generate_from_ai('GET /fixture', [], LoadConfig(test_type='baseline'))
    assert ET.fromstring(plan).find('.//HTTPSamplerProxy').get('testname') == 'a'
    assert adapter.chat.call_args.kwargs['max_tokens'] == 8000


@pytest.mark.parametrize('base,protocol,host,port', [
    ('https://example.invalid:8443/v1/', 'https', 'example.invalid', '8443'),
    ('http://[2001:db8::1]:8080/v1', 'http', '2001:db8::1', '8080'),
])
def test_base_url_keeps_host_port_and_prefix(base, protocol, host, port):
    root = ET.fromstring(JMXGenerator().generate_basic_jmx(
        [APIEndpoint(name='api', method='GET', path='/users')], LoadConfig(test_type='baseline'), base))
    defaults = root.find('.//ConfigTestElement')
    for name, expected in [('protocol', protocol), ('domain', host), ('port', port)]:
        assert defaults.find(f'stringProp[@name="HTTPSampler.{name}"]').text == expected
    assert root.find('.//HTTPSamplerProxy/stringProp[@name="HTTPSampler.path"]').text == '/v1/users'


@pytest.mark.parametrize('body', [{'name': 'test'}, {}])
def test_query_does_not_mix_with_raw_body_and_content_type_is_present(body):
    from urllib.parse import parse_qs, urlsplit
    import json
    root = ET.fromstring(JMXGenerator().generate_basic_jmx([
        APIEndpoint(name='create', method='POST', path='/items?existing=1', body=body,
                    query_params={'q': 'a b', 'token': '${token}'}, content_type='application/problem+json',
                    expected_status=201)], LoadConfig(test_type='baseline'), 'https://example.invalid/api'))
    sampler = root.find('.//HTTPSamplerProxy')
    path = sampler.find('stringProp[@name="HTTPSampler.path"]').text
    assert parse_qs(urlsplit(path).query) == {'existing': ['1'], 'q': ['a b'], 'token': ['${token}']}
    assert '${token}' in path
    args = sampler.findall('elementProp/collectionProp/elementProp')
    assert len(args) == 1
    assert json.loads(args[0].find('stringProp[@name="Argument.value"]').text) == body
    assert sampler.find('boolProp[@name="HTTPSampler.postBodyRaw"]').text == 'true'
    headers = [(entry.find('stringProp[@name="Header.name"]').text,
                entry.find('stringProp[@name="Header.value"]').text)
               for entry in root.findall('.//HeaderManager/collectionProp/elementProp')]
    assert ('Content-Type', 'application/problem+json') in headers
    assert root.find('.//ResponseAssertion/collectionProp/stringProp').text == '201'


def test_content_type_header_explicit_override_and_raw_template_are_preserved():
    endpoint = APIEndpoint(name='raw', method='POST', path='/raw', content_type='application/json',
        headers={'content-type': 'text/plain'}, bindings=[VariableBinding(
            variable_name='payload', target_location='body', target_key='', template='${payload}')])
    root = ET.fromstring(JMXGenerator().generate_basic_jmx([endpoint], LoadConfig(test_type='baseline'),
                                                          'https://example.invalid'))
    assert root.find('.//HTTPSamplerProxy/elementProp/collectionProp/elementProp/stringProp[@name="Argument.value"]').text == '${payload}'
    values = [node.text for node in root.findall('.//HeaderManager//stringProp[@name="Header.value"]')]
    assert 'text/plain' in values and 'application/json' not in values


@pytest.mark.asyncio
async def test_agent_endpoint_constructor_keeps_expected_status(monkeypatch, tmp_path):
    from app.services.ai.agent.tools import jmx
    from app.services import execution_lifecycle
    fake = SimpleNamespace(register_path=AsyncMock(return_value=SimpleNamespace(id='registered')))
    monkeypatch.setattr(execution_lifecycle, 'execution_lifecycle', fake)
    saved = []
    def save(self, content, **kwargs):
        saved.append(content)
        return str(tmp_path / 'generated.jmx')
    monkeypatch.setattr(JMXGenerator, 'save_jmx', save)
    await jmx.create_jmx('create', [{'name': 'create', 'method': 'POST', 'path': '/',
                                   'expected_status': 204}], 'https://example.invalid')
    assert ET.fromstring(saved[0]).find('.//ResponseAssertion/collectionProp/stringProp').text == '204'


@pytest.mark.parametrize('kind', ['baseline', 'concurrency', 'stability', 'custom'])
def test_explicit_rate_generates_one_shared_rps_timer_without_losing_duration(kind):
    config = LoadConfig(**LoadConfig(test_type=kind, target_rps=100).model_dump())
    root = ET.fromstring(JMXGenerator().generate_basic_jmx(
        [APIEndpoint(name='one', method='GET', path='/1'), APIEndpoint(name='two', method='GET', path='/2')],
        config, 'https://example.invalid'))
    timers = root.findall('.//ConstantThroughputTimer')
    assert len(timers) == 1
    assert timers[0].find("intProp[@name='calcMode']").text == '3'
    assert timers[0].find('doubleProp/name').text == 'throughput'
    assert float(timers[0].find('doubleProp/value').text) == 6000
    assert root.find('.//ThreadGroup/stringProp[@name="ThreadGroup.duration"]').text == '300'
    assert root.find('.//ThreadGroup/stringProp[@name="ThreadGroup.ramp_time"]').text == '60'
    parsed_timer = next(node for node in walk(parse_jmx(ET.tostring(root, encoding='unicode')))
                        if node.get('testclass') == 'ConstantThroughputTimer')
    assert parsed_timer['properties']['throughput'] == 6000.0


def test_template_rps_converts_samples_per_second_to_per_minute():
    from app.api.routes import TEST_TEMPLATES
    for template in TEST_TEMPLATES:
        if template.id in {'ramp_up', 'spike'}:
            continue
        config = LoadConfig(**template.default_config.model_dump())
        root = ET.fromstring(JMXGenerator().generate_basic_jmx([], config, 'https://example.invalid'))
        assert float(root.find('.//ConstantThroughputTimer/doubleProp/value').text) == config.target_rps * 60


def test_default_config_does_not_cap_the_rate():
    # A throughput timer nobody asked for silently caps the load and misleads the analysis.
    assert LoadConfig(test_type='baseline').target_rps is None
    root = ET.fromstring(JMXGenerator().generate_basic_jmx([], LoadConfig(test_type='baseline'), 'https://example.invalid'))
    assert root.find('.//ConstantThroughputTimer') is None


@pytest.mark.parametrize('rate', [None, 0])
def test_no_target_rate_means_no_rate_timer(rate):
    root = ET.fromstring(JMXGenerator().generate_basic_jmx([], LoadConfig(test_type='baseline', target_rps=rate),
                                                          'https://example.invalid'))
    assert root.find('.//ConstantThroughputTimer') is None


def test_negative_target_rate_rejected():
    with pytest.raises(ValueError, match='target_rps'):
        JMXGenerator().generate_basic_jmx([], LoadConfig(test_type='baseline', target_rps=-1), 'https://example.invalid')


def test_standard_double_property_keeps_throughput_on_editor_roundtrip():
    from app.services.jmeter.jmx_builder import build_jmx
    xml = '''<jmeterTestPlan><hashTree><TestPlan testclass="TestPlan"/><hashTree>
      <ConstantThroughputTimer testclass="ConstantThroughputTimer"><doubleProp><name>throughput</name>
      <value>6000.0</value><savedValue>0.0</savedValue></doubleProp><intProp name="calcMode">3</intProp>
      </ConstantThroughputTimer><hashTree/></hashTree></hashTree></jmeterTestPlan>'''
    node = parse_jmx(xml)
    assert node['children'][0]['properties']['throughput'] == 6000.0
    rebuilt = parse_jmx(build_jmx(node))
    assert float(rebuilt['children'][0]['properties']['throughput']) == 6000
    assert int(rebuilt['children'][0]['properties']['calcMode']) == 3


def test_absolute_endpoint_does_not_gain_base_prefix_and_keeps_query():
    root = ET.fromstring(JMXGenerator().generate_basic_jmx([
        APIEndpoint(name='absolute', method='GET', path='https://other.invalid/full?x=1', query_params={'y': '2'})],
        LoadConfig(test_type='baseline'), 'https://example.invalid/base'))
    assert root.find('.//HTTPSamplerProxy/stringProp[@name="HTTPSampler.path"]').text == 'https://other.invalid/full?x=1&y=2'


@pytest.fixture
def jmx_file(tmp_path):
    xml = JMXGenerator().generate_basic_jmx(
        [APIEndpoint(name='api', method='GET', path='/')],
        LoadConfig(test_type='baseline', concurrent_users=1, duration_seconds=1, think_time_ms=5),
        'https://example.invalid')
    path = tmp_path / 'plan.jmx'
    path.write_text(xml, encoding='utf-8')
    return path


def test_json_extractor_uses_jmeter_property():
    root = ET.Element('hashTree')
    JMXGenerator()._create_json_extractor(root, ExtractionRule(
        ref_name='token', extract_type='json_path', expression='$.token'))
    assert root.find('.//stringProp[@name="JSONPostProcessor.jsonPathExprs"]').text == '$.token'


@pytest.mark.parametrize('location', ['path', 'header', 'query', 'body'])
def test_bindings_emit_single_pair_of_braces(location):
    endpoint = APIEndpoint(name='api', method='POST', path='/user/{id}', bindings=[
        VariableBinding(variable_name='id', target_location=location, target_key='id')])
    generator = JMXGenerator()
    resolved = generator._resolve_bindings(endpoint)
    actual = {'path': resolved.path, 'header': (resolved.headers or {}).get('id'),
              'query': (resolved.query_params or {}).get('id'), 'body': (resolved.body or {}).get('id')}[location]
    assert actual == ('/user/${id}' if location == 'path' else '${id}')
    if location == 'header':
        assert generator._resolve_header_bindings(endpoint)['id'] == '${id}'
    assert endpoint.path == '/user/{id}'


@pytest.mark.asyncio
async def test_update_tool_preserves_dict_property_contract(jmx_file, monkeypatch):
    from app.core import config
    from app.services.ai.agent.tools.jmx import update_jmx

    monkeypatch.setattr(config.settings, 'TEST_PLANS_DIR_DEFAULT', str(jmx_file.parent))
    monkeypatch.setattr(config, 'load_paths_config', lambda: {})
    result = await update_jmx(str(jmx_file), concurrent_users=12, duration_seconds=45,
                              ramp_up_seconds=3, think_time_ms=25)
    assert result.get('updated'), result
    nodes = list(walk(parse_jmx(str(jmx_file))))
    props = next(n['properties'] for n in nodes if n.get('testclass') == 'ThreadGroup')
    assert props['ThreadGroup.num_threads'] == '12'
    assert props['ThreadGroup.duration'] == '45'
    assert props['ThreadGroup.ramp_time'] == '3'
    assert props['ThreadGroup.scheduler'] is True
    assert any(n.get('properties', {}).get('ConstantTimer.delay') == '25' for n in nodes)


def test_selfheal_reads_and_updates_dict_extractor_properties():
    from app.services.ai.agents.selfheal_agent import SelfHealAgent, ExtractionFix

    tree = {'testclass': 'JSONPostProcessor', 'properties': {
        'JSONPostProcessor.referenceNames': 'token',
        'JSONPostProcessor.jsonPathExprs': '$.token'}, 'children': []}
    agent = SelfHealAgent()
    assert agent._find_extractions(tree)[0]['expression'] == '$.token'
    agent._apply_fixes(tree, [ExtractionFix('$.token', '$.data.token', 'token', 1, 'json_path')])
    assert tree['properties']['JSONPostProcessor.jsonPathExprs'] == '$.data.token'


EDITOR_FIXTURE = '''<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.2">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Plan" enabled="true">
      <elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
        <collectionProp name="Arguments.arguments"/>
      </elementProp>
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Users" enabled="true">
        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <intProp name="LoopController.loops">-1</intProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">10</stringProp>
      </ThreadGroup>
      <hashTree>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="GET /items" enabled="true">
          <elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
            <collectionProp name="Arguments.arguments"/>
          </elementProp>
          <stringProp name="HTTPSampler.path">/items</stringProp>
        </HTTPSamplerProxy>
        <hashTree>
          <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="200" enabled="true">
            <collectionProp name="Asserion.test_strings">
              <stringProp name="49586">200</stringProp>
            </collectionProp>
            <intProp name="Assertion.test_type">8</intProp>
          </ResponseAssertion>
          <hashTree/>
        </hashTree>
        <ResultCollector guiclass="SummaryReport" testclass="ResultCollector" testname="Summary" enabled="true">
          <objProp>
            <name>saveConfig</name>
            <value class="SampleSaveConfiguration"><time>true</time></value>
          </objProp>
        </ResultCollector>
        <hashTree/>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
'''


def _canonical(xml):
    def walk(e):
        return (e.tag, sorted(e.attrib.items()), (e.text or '').strip(), [walk(c) for c in e])
    return walk(ET.fromstring(xml.encode()))


def test_editor_roundtrip_keeps_unedited_xml_unchanged():
    import json
    from app.services.jmeter.jmx_builder import build_jmx
    # Through JSON, the way the editor sends the tree back.
    tree = json.loads(json.dumps(parse_jmx(EDITOR_FIXTURE)))
    assert _canonical(build_jmx(tree)) == _canonical(EDITOR_FIXTURE)


def test_editor_roundtrip_rewrites_only_what_was_edited():
    from app.services.jmeter.jmx_builder import build_jmx
    tree = parse_jmx(EDITOR_FIXTURE)
    group = tree['children'][0]
    group['properties']['ThreadGroup.num_threads'] = '25'
    group['properties']['loops'] = 3
    sampler = group['children'][0]
    sampler['properties']['__body_data'] = '{"a": 1}'
    sampler['properties']['HTTPSampler.postBodyRaw'] = True
    root = ET.fromstring(build_jmx(tree).encode())

    tg = root.find('.//ThreadGroup')
    assert tg.find('stringProp[@name="ThreadGroup.num_threads"]').text == '25'
    assert tg.find('stringProp[@name="ThreadGroup.on_sample_error"]').text == 'continue'
    assert tg.find('.//*[@name="LoopController.loops"]').text == '3'
    body = root.find('.//HTTPSamplerProxy//elementProp[@elementType="HTTPArgument"]/stringProp[@name="Argument.value"]')
    assert body.text == '{"a": 1}'
    assert root.find('.//ResponseAssertion/intProp[@name="Assertion.test_type"]').text == '8'
    collector = root.find('.//ResultCollector')
    assert collector.get('guiclass') == 'SummaryReport'
    assert collector.find('objProp/value/time').text == 'true'
    assert root.get('jmeter') == '5.6.2'


def test_editor_roundtrip_drops_deleted_property_and_adds_new_one():
    from app.services.jmeter.jmx_builder import build_jmx
    tree = parse_jmx(EDITOR_FIXTURE)
    group = tree['children'][0]
    del group['properties']['ThreadGroup.on_sample_error']
    group['properties']['ThreadGroup.ramp_time'] = '30'
    tg = ET.fromstring(build_jmx(tree).encode()).find('.//ThreadGroup')
    assert tg.find('stringProp[@name="ThreadGroup.on_sample_error"]') is None
    assert tg.find('stringProp[@name="ThreadGroup.ramp_time"]').text == '30'


@pytest.mark.asyncio
async def test_model_facing_tree_has_no_roundtrip_bookkeeping(tmp_path, monkeypatch):
    from app.services.ai.agent.tools import jmx as jmx_tools
    path = tmp_path / 'plan.jmx'
    path.write_text(EDITOR_FIXTURE, encoding='utf-8')
    monkeypatch.setattr('app.core.config.safe_path', lambda p: p)
    result = await jmx_tools.parse_jmx_tree(str(path))
    assert not any(key.startswith('__') and key not in ('__body_data', '__http_params')
                   for node in walk(result['tree']) for key in node['properties'])
