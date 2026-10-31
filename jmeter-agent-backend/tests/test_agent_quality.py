"""What the live review of generated plans, reports and agent runs asked for."""
import json
import xml.etree.ElementTree as ET
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.schemas import (APIEndpoint, DetailedTestSummary, ExtractionRule, ReportAnalysis,
                                TestConfig as LoadConfig, VariableBinding)
from app.services import run_diagnostics
from app.services.ai.adapters.base import BaseAIAdapter, ToolCallRequest, ToolCallResponse
from app.services.ai.agent.base import AGENT_SAFETY_RULES, BaseAgent
from app.services.ai.agent.tool_registry import Tool, ToolRegistry
from app.services.ai.agents.test_plan_agent import TestPlanAgent
from app.services.ai.agents.workflow_agent import WorkflowAgent
from app.services.ai.prompts.analysis_rules import ANALYSIS_RULES
from app.services.evidence import ExecutionEvidence, _format_planned_load
from app.services import requirements as rq
from app.services.jmeter import jmx_lint
from app.services.jmeter.jmx_generator import JMXGenerator, normalize_scenario
from app.services.jmeter.report_parser import ReportParser

HEADER = ('timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,'
          'bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect\n')


def row(ts, elapsed, label='A', threads=10, connect=0, latency=None):
    return (f'{ts},{elapsed},{label},200,OK,t,text,true,,100,10,{threads},{threads},http://x,'
            f'{elapsed if latency is None else latency},0,{connect}\n')


def parse(rows):
    return ReportParser()._parse_csv_jtl(StringIO(HEADER + ''.join(rows)))


def plan(*endpoints, **config):
    return JMXGenerator().generate_basic_jmx(list(endpoints), LoadConfig(**{'test_type': 'baseline', **config}),
                                             'https://shop.invalid')


GET = APIEndpoint(name='x', method='GET', path='/a')


def test_planned_load_reads_the_real_thread_groups():
    groups = jmx_lint.planned_load(plan(GET, test_type='spike', concurrent_users=10, duration_seconds=60, ramp_up_seconds=6))
    assert [(g['name'], g['threads'], g['delay_seconds'], g['duration_seconds']) for g in groups] == [
        ('SPIKE base', 1, 0, 60), ('SPIKE burst', 9, 20, 20)]
    assert 'planned_load' in _format_planned_load(groups) and _format_planned_load([]) == ''
    assert jmx_lint.planned_load('') == [] and jmx_lint.planned_load('<broken') == []


def test_the_analysis_is_told_the_planned_shape():
    from app.services.evidence import format_for_prompt
    summary = parse([row(1000, 10)])
    text = format_for_prompt(ExecutionEvidence(execution_id='x', project_id='p', requirements=rq.evaluate(None, summary),
                                               planned_load=[{'name': 'RAMP_UP step 1', 'threads': 2}]))
    assert 'RAMP_UP step 1' in text


def test_steady_leaves_out_requests_that_opened_a_connection():
    late_logins = [row(30000 + i, 1500, label='Login', connect=1000) for i in range(2)]
    summary = parse([row(1000 + i * 500, 100) for i in range(100)] + late_logins)
    steady = summary.diagnostics['steady']
    assert steady['max_response_time'] == 100 and 'Login' not in steady['labels']


def test_without_keep_alive_steady_still_exists():
    summary = parse([row(1000 + i * 500, 100, connect=20) for i in range(100)])
    assert summary.diagnostics['steady']['count'] == 100


def test_wait_time_is_latency_without_the_connect():
    summary = parse([row(1000, 1500, connect=1000, latency=1400)])
    assert summary.diagnostics['timing']['avg_wait_time'] == 400
    assert summary.diagnostics['slowest'][0]['wait'] == 400
    assert summary.timeline_segments[0].avg_wait_time == 400


def test_a_handful_of_samples_gets_no_percentiles():
    summary = parse([row(1000 + i, 100, label='Login') for i in range(3)] + [row(2000 + i, 50, label='B') for i in range(40)])
    rows = {item['label']: item for item in run_diagnostics.build(summary)['labels']}
    assert rows['Login']['p95_response_time'] is None and rows['Login']['avg_response_time'] == 100
    assert rows['B']['p95_response_time'] == 50


def test_a_short_run_gets_no_p99():
    from app.services.ai.prompts.test_analyzer import build_analysis_prompt
    assert '"p99"' not in build_analysis_prompt({'total_requests': 50, 'percentiles': {'p99': 900}}, {}, 'baseline')
    assert '"p99"' in build_analysis_prompt({'total_requests': 500, 'percentiles': {'p99': 900}}, {}, 'baseline')


def test_reports_are_capped_and_ask_for_limitations():
    from app.services.ai.analyzer import TestAnalyzer
    analyzer = TestAnalyzer.__new__(TestAnalyzer)
    result = analyzer._parse_response(json.dumps({
        'overall_assessment': 'a', 'conclusion': 'b', 'key_findings': list('abcdefgh'), 'recommendations': list('abcde'),
        'limitations': ['没有服务端指标'], 'hypotheses': [{'statement': 's'}] * 5}))
    assert len(result.key_findings) == 5 and len(result.recommendations) == 3 and len(result.hypotheses) == 3
    assert result.limitations == ['没有服务端指标']


def test_all_analysis_paths_share_the_rules():
    from app.services.ai.agents.analysis_agent import ANALYSIS_SYSTEM_PROMPT
    from app.services.ai.prompts.test_analyzer import TEST_ANALYZER_SYSTEM_PROMPT
    assert ANALYSIS_RULES in ANALYSIS_SYSTEM_PROMPT and ANALYSIS_RULES in TEST_ANALYZER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_mcp_analysis_prompt_carries_the_rules():
    from app.services.mcp_server import MCPServer
    server = MCPServer()
    server.register_all_prompts()
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'prompts/get',
               'params': {'name': 'analyze-execution', 'arguments': {'execution_id': 'run-1'}}}
    response = await server.handle_request(json.dumps(request))
    assert 'planned_load' in response['result']['messages'][0]['content']['text']


def test_labels_come_from_the_request_not_the_models_wording():
    login = APIEndpoint(name='用户登录', method='post', path='/login?x=1',
                        extractions=[ExtractionRule(ref_name='token', extract_type='json_path', expression='$.t')])
    orders = APIEndpoint(name='订单', method='GET', path='/orders', depends_on=['用户登录'], bindings=[
        VariableBinding(variable_name='token', target_location='header', target_key='Authorization', template='Bearer ${token}')])
    again = APIEndpoint(name='再看订单', method='GET', path='/orders')
    first, second, third = normalize_scenario([login, orders, again])
    assert (first.name, second.name, third.name) == ('POST /login', 'GET /orders', 'GET /orders #2')
    assert second.depends_on == ['POST /login'] and first.extractions


def test_values_nothing_reads_are_not_extracted():
    create = APIEndpoint(name='a', method='POST', path='/a', extractions=[
        ExtractionRule(ref_name='unused', extract_type='json_path', expression='$.id'),
        ExtractionRule(ref_name='kept', extract_type='json_path', expression='$.k')])
    read = APIEndpoint(name='b', method='GET', path='/b/${kept}')
    assert [rule.ref_name for rule in normalize_scenario([create, read])[0].extractions] == ['kept']


def test_think_time_spreads_around_its_mean():
    timer = ET.fromstring(plan(GET, think_time_ms=1000)).find('.//UniformRandomTimer')
    assert timer.find("stringProp[@name='ConstantTimer.delay']").text == '750'
    assert timer.find("stringProp[@name='RandomTimer.range']").text == '500.0'


def test_a_failed_login_stops_only_that_user():
    root = ET.fromstring(plan(APIEndpoint(name='Login', method='POST', path='/login', phase='once_per_user'), GET))
    actions = root.findall('.//ResultAction')
    assert len(actions) == 1 and actions[0].find("intProp[@name='OnError.action']").text == '1'


def test_planning_agents_keep_to_their_job():
    agent = TestPlanAgent(FakeAdapter())
    names = set(agent.tools._tools) if hasattr(agent.tools, '_tools') else set()
    assert 'create_jmx' in names and not names & {'delete_file', 'write_file', 'execute_test', 'read_file'}
    workflow = WorkflowAgent(FakeAdapter())
    assert 'execute_test' in workflow.tools and 'delete_file' not in workflow.tools


def test_resource_checks_are_for_runs_and_planners_see_the_domain():
    assert 'Creating or inspecting a plan needs no resource check' in AGENT_SAFETY_RULES
    assert '编写压测用例的规则' in TestPlanAgent(FakeAdapter()).planner.system_prompt


class FakeAdapter(BaseAIAdapter):
    def __init__(self, tool_replies=(), chat_replies=()):
        super().__init__(api_key='fake', model='fake')
        self.tool_replies, self.chat_replies = list(tool_replies), list(chat_replies)
        self.chat_count = self.tool_count = 0

    async def chat(self, messages, **kwargs):
        self.chat_count += 1
        return self.chat_replies.pop(0) if self.chat_replies else 'summary'

    async def chat_stream(self, messages, **kwargs):
        yield 'x'

    async def generate(self, prompt, **kwargs):
        return 'x'

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.tool_count += 1
        self.budget = kwargs.get('max_tokens')
        return self.tool_replies.pop(0)


def registry(handler):
    tools = ToolRegistry()
    tools.register(Tool(name='create_jmx', description='d', parameters={'type': 'object', 'properties': {}}, handler=handler))
    return tools


@pytest.mark.asyncio
async def test_generating_a_plan_takes_one_tool_call_and_no_extra_model_calls():
    calls = []

    async def create():
        calls.append(1)
        return {'test_plan_id': 'p'}
    adapter = FakeAdapter([ToolCallResponse(tool_calls=[ToolCallRequest('1', 'create_jmx', {})]),
                           ToolCallResponse(text='计划 p 已创建')])
    result = await TestPlanAgent(adapter, tools=registry(create)).run('写一个计划')
    assert result.success and result.output == '计划 p 已创建' and calls == [1]
    assert adapter.tool_count == 2 and adapter.chat_count == 0  # No planner and no summary call.


@pytest.mark.asyncio
async def test_an_empty_model_reply_is_not_a_conclusion():
    async def create():
        return {'test_plan_id': 'p'}
    adapter = FakeAdapter([ToolCallResponse(text=''), ToolCallResponse(tool_calls=[ToolCallRequest('1', 'create_jmx', {})]),
                           ToolCallResponse(text='完成')])
    result = await TestPlanAgent(adapter, tools=registry(create)).run('写一个计划')
    assert result.success and adapter.tool_count == 3


@pytest.mark.asyncio
async def test_tool_calls_have_room_for_a_long_scenario():
    adapter = FakeAdapter([ToolCallResponse(tool_calls=[ToolCallRequest('1', 'create_jmx', {})]), ToolCallResponse(text='完成')])
    async def create():
        return {}
    agent = TestPlanAgent(adapter, tools=registry(create))
    await agent.run('写一个计划')
    assert adapter.budget == min(8000, max(2000, adapter.context_budget // 4))


@pytest.mark.asyncio
async def test_report_hypotheses_join_the_ledger_once(monkeypatch):
    from app.api import routes
    from app.services.ai import memory
    added = []

    async def add(hypothesis, if_absent=False):
        added.append((hypothesis, if_absent))
    monkeypatch.setattr(memory.hypothesis_ledger, 'add', add)
    analysis = ReportAnalysis(overall_assessment='a', conclusion='b', hypotheses=[
        {'statement': '首页慢在建连', 'category': 'bottleneck', 'testable_prediction': 'p99 should decrease by >20%'},
        {'statement': '', 'category': 'risk'}, {'statement': '分类写错了', 'category': 'nonsense'}])
    await routes._record_hypotheses(SimpleNamespace(id='run', ai_analysis=analysis),
                                    SimpleNamespace(module='plan', version='unknown', test_type='baseline'))
    assert len(added) == 2 and all(flag for _, flag in added)
    assert added[0][0].testable_prediction == 'p99 should decrease by >20%' and added[0][0].status.value == 'untested'
    assert added[1][0].category.value == 'bottleneck'


@pytest.mark.asyncio
async def test_reports_see_what_earlier_runs_left_open(monkeypatch):
    from app.api import routes
    from app.models.schemas import Hypothesis, HypothesisCategory
    from app.services.ai import memory
    found = [Hypothesis(hypothesis_id='h', source_execution_id='old', module='plan', category=HypothesisCategory.RISK,
                        statement='连接池可能不够')]
    monkeypatch.setattr(memory.hypothesis_ledger, 'list_for_module', AsyncMock(return_value=found))
    assert '连接池可能不够' in await routes._open_hypotheses(SimpleNamespace(test_plan_id='plan'))
    monkeypatch.setattr(memory.hypothesis_ledger, 'list_for_module', AsyncMock(return_value=[]))
    assert await routes._open_hypotheses(SimpleNamespace(test_plan_id='plan')) == ''


def test_an_average_of_one_request_phase_is_a_real_number():
    from app.services.ai.memory.fact_ledger import FactLedger
    from app.services.ai.memory.validators import FactValidator
    summary = parse([row(1000, 1500, connect=1000, latency=1400), row(1100, 300)])
    fact = FactLedger.compute_from_summary(summary, 'x')
    assert FactValidator().validate('新建连接平均耗时 1000ms', fact).mismatches == []
    assert FactValidator().validate('平均耗时 98765ms', fact).mismatches
