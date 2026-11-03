"""Tests for the BaseAgent loop: each run is one ReAct step, retried while its failure is transient.

All LLM traffic is stubbed by FakeAdapter. No network access, no real model calls.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.models.schemas import AIChatMessage
from app.services.ai.adapters.base import (
    BaseAIAdapter, ToolCallRequest, ToolCallResponse,
)
from app.services.ai.agent.base import BaseAgent
from app.services.ai.agent.tool_registry import Tool, ToolRegistry
from app.services.ai.agent.types import (
    AgentState, ErrorCategory, PlanStep, ToolResult,
)


class FakeAdapter(BaseAIAdapter):
    """Deterministic adapter returning queued responses."""

    def __init__(self, chat_responses=None, tool_responses=None):
        super().__init__(api_key="fake", model="fake")
        self._chat_responses = list(chat_responses or [])
        self._tool_responses = list(tool_responses or [])
        self.chat_calls: list[list[AIChatMessage]] = []
        self.chat_kwargs = []
        self.chat_with_tools_calls = 0
        self.tool_messages = []

    async def chat(self, messages, **kwargs) -> str:
        self.chat_calls.append(messages)
        self.chat_kwargs.append(kwargs)
        if self._chat_responses:
            return self._chat_responses.pop(0)
        return "summary"

    async def chat_stream(self, messages, **kwargs):
        yield "chunk"

    async def generate(self, prompt, **kwargs) -> str:
        return "generated"

    async def chat_with_tools(self, messages, tools, **kwargs) -> ToolCallResponse:
        self.chat_with_tools_calls += 1
        self.tool_messages.append(list(messages))
        if self._tool_responses:
            return self._tool_responses.pop(0)
        return ToolCallResponse(text="step concluded")


def make_echo_tool(calls: list) -> Tool:
    async def handler(msg: str = "") -> dict:
        calls.append(msg)
        return {"echo": msg}

    return Tool(
        name="echo_tool",
        description="Echo back the message",
        parameters={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
        },
        handler=handler,
    )


def make_agent(adapter: FakeAdapter, tools: ToolRegistry | None = None,
               **kwargs) -> BaseAgent:
    return BaseAgent(
        name="test-agent",
        adapter=adapter,
        tools=tools if tools is not None else ToolRegistry(),
        **kwargs,
    )


def talking_agent(adapter: FakeAdapter | None = None, **kwargs) -> BaseAgent:
    """An agent whose task needs no tool call: the model's reply finishes it."""
    registry = ToolRegistry()
    registry.register(make_echo_tool([]))
    agent = make_agent(adapter or FakeAdapter(), registry, **kwargs)
    agent.requires_tools = False
    return agent


def pause_steps(agent: BaseAgent):
    """Hold each step until released. Returns (entered, release, steps run)."""
    entered, release, steps = asyncio.Event(), asyncio.Event(), []
    original = agent._execute_react_step

    async def paused(step):
        steps.append(step)
        entered.set()
        await release.wait()
        return await original(step)

    agent._execute_react_step = paused
    return entered, release, steps


def event_types(agent: BaseAgent) -> list[str]:
    return [e.type for e in agent._events]


@pytest.fixture
def registered_agent(monkeypatch):
    import importlib
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    provenance = importlib.import_module('app.services.ai.memory.provenance')
    adapter = FakeAdapter(tool_responses=[ToolCallResponse(text='Measured answer')])
    tools = ToolRegistry()
    tools.register(make_echo_tool([]))
    agent = AnalysisAgent(adapter, tools=tools)
    fact = FactSheet(execution_id='run-A', module='plan-A', version='unknown', test_type='baseline',
                     total_requests=10, context_scope='scope', jtl_path='fixture.jtl', jtl_sha256='digest')
    agent._build_fact_sheet = AsyncMock(return_value=fact)
    monkeypatch.setattr(provenance, 'load_execution_scope', AsyncMock(return_value='scope'))
    monkeypatch.setattr(module.fact_ledger, 'get_facts_for_module_version', AsyncMock(return_value=[]))
    evidence = dict(execution_id='run-A', jtl_path='fixture.jtl', module='plan-A', version='unknown',
                    test_type='baseline', context_scope='scope')
    return agent, evidence, fact


@pytest.mark.asyncio
async def test_registered_ledger_read_contract_does_not_change_legacy_failure_behavior(registered_agent, monkeypatch, tmp_path):
    import importlib
    from unittest.mock import Mock
    from app.services.ai.memory.fact_ledger import FactLedger
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    agent, evidence, _ = registered_agent
    ledger = FactLedger(str(tmp_path / 'unused.db'))
    ledger._ensure_table = AsyncMock()
    db = Mock()
    db.execute.side_effect = OSError('fixture')
    ledger._get_db = AsyncMock(return_value=db)
    monkeypatch.setattr(module.analysis_context, 'fact_ledger', ledger)
    result = await agent.analyze_with_rag(execution_id=evidence['execution_id'], module=evidence['module'],
                                          user_task='why?')
    assert result['success'] and result['recall'] == {}
    assert agent.adapter.chat_with_tools_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['preparation', 'validation'])
async def test_registered_analysis_manual_cancel_is_not_reset_by_inner_loop(registered_agent, monkeypatch, phase):
    import importlib
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    agent, evidence, _ = registered_agent
    if phase == 'preparation':
        original = agent._build_fact_sheet
        async def cancel_here(*args, **kwargs):
            result = await original(*args, **kwargs)
            agent.cancel()
            return result
        agent._build_fact_sheet = cancel_here
    else:
        original = module.fact_validator.validate
        def cancel_here(*args, **kwargs):
            result = original(*args, **kwargs)
            agent.cancel()
            return result
        monkeypatch.setattr(module.fact_validator, 'validate', cancel_here)
    result = await agent.run_analysis('cancel task', evidence)
    assert not result.success and result.error == 'Cancelled by user'
    assert agent.state == AgentState.CANCELLED and not agent._run_active
    assert agent.adapter.chat_with_tools_calls == (0 if phase == 'preparation' else 1)


@pytest.mark.asyncio
async def test_a_question_is_answered_from_the_registered_evidence_and_archives_nothing(registered_agent):
    import json
    agent, evidence, fact = registered_agent
    result = await agent.run_analysis('为什么慢？', evidence, context='REPORT')
    assert result.success and result.task == '为什么慢？' and len(result.plan) == 1
    assert result.output == 'Measured answer'
    assert result.analysis['fact']['execution_id'] == 'run-A'
    assert result.analysis['validation'] is not None
    assert 'archive' not in result.analysis and 'structured' not in result.analysis
    assert result.events[0].type == 'analysis_phase'
    json.dumps(result.analysis, allow_nan=False)
    sent = '\n'.join(message.content for message in agent.adapter.tool_messages[0])
    assert '为什么慢？' in sent and '不要重写整份报告' in sent and 'REPORT' in sent and 'scope' in sent
    assert agent.adapter.chat_with_tools_calls == 1 and not agent.adapter.chat_calls
    assert not agent._run_active


@pytest.mark.asyncio
async def test_an_empty_answer_is_not_a_success(registered_agent):
    agent, evidence, _ = registered_agent
    agent.adapter._tool_responses = [ToolCallResponse(text='')] * agent.max_react_per_step
    result = await agent.run_analysis('为什么慢？', evidence)
    assert not result.success and result.error == 'The model did not answer'
    assert result.analysis['validation'] is None


@pytest.mark.asyncio
async def test_a_number_the_facts_do_not_hold_is_reported_with_the_answer(registered_agent):
    agent, evidence, _ = registered_agent
    agent.adapter._tool_responses = [ToolCallResponse(text='错误率为 87.5%')]
    result = await agent.run_analysis('错误率多少？', evidence)
    assert result.success and result.output == '错误率为 87.5%'
    assert any(m['metric'] == 'error_rate' for m in result.analysis['validation']['mismatches'])


@pytest.mark.asyncio
async def test_a_model_that_only_announces_its_work_is_reminded_once():
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[
        ToolCallResponse(text='I will start by calling the tool.'),
        ToolCallResponse(tool_calls=[ToolCallRequest('c1', 'echo_tool', {'msg': 'hi'})]),
        ToolCallResponse(text='All done.')])
    agent = make_agent(adapter, registry)
    result = await agent.run('do it')
    assert result.success and calls == ['hi'] and result.output == 'All done.'
    assert adapter.chat_with_tools_calls == 3 and not adapter.chat_calls


@pytest.mark.asyncio
async def test_a_task_needing_tools_still_fails_without_a_tool_call():
    agent = make_agent(FakeAdapter(tool_responses=[ToolCallResponse(text='done, trust me')]))
    agent.tools.register(make_echo_tool([]))
    assert not (await agent.run('create the plan')).success


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['preparation', 'validation'])
async def test_registered_analysis_holds_the_agent_through_entire_operation(registered_agent, monkeypatch, phase):
    import importlib
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    agent, evidence, fact = registered_agent
    entered, release = asyncio.Event(), asyncio.Event()
    if phase == 'preparation':
        original = agent._build_fact_sheet
        async def pause(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        agent._build_fact_sheet = pause
    else:
        # Validation is synchronous, so hold the model step that comes just before it.
        step_entered, step_release, _ = pause_steps(agent)
        entered, release = step_entered, step_release
    running = asyncio.create_task(agent.run_analysis('first', evidence))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not (await agent.run('overlap')).success
        assert not (await agent.run_analysis('overlap', evidence)).success
        assert agent._run_active
    finally:
        release.set()
        result = await running
    assert result.success and not agent._run_active


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_source', ['scope', 'zero_samples'])
async def test_registered_analysis_never_models_changed_scope_or_empty_evidence(registered_agent, monkeypatch, bad_source):
    import importlib
    agent, evidence, fact = registered_agent
    if bad_source == 'scope':
        provenance = importlib.import_module('app.services.ai.memory.provenance')
        monkeypatch.setattr(provenance, 'load_execution_scope', AsyncMock(return_value='other-scope'))
    else:
        fact.total_requests = 0
    result = await agent.run_analysis('analyze', evidence)
    assert not result.success and result.error
    assert not agent.adapter.chat_calls and not agent.adapter.chat_with_tools_calls


@pytest.mark.parametrize('message,expected', [
    ('JMeter executable not found', ErrorCategory.SYSTEM_ERROR),
    ('command not found', ErrorCategory.SYSTEM_ERROR),
    ('permission denied; retry later', ErrorCategory.SYSTEM_ERROR),
    ('disk full while retrying', ErrorCategory.SYSTEM_ERROR),
    ('invalid arguments: 400', ErrorCategory.INPUT_ERROR),
    ('503 temporarily unavailable', ErrorCategory.RETRYABLE),
])
def test_unrecoverable_errors_take_precedence_over_retry_and_input_patterns(message, expected):
    assert BaseAgent._classify_error(message) == expected
    assert ToolRegistry._classify_error(message) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('prior_success', [True, False])
@pytest.mark.parametrize('failure', ['system', 'dangerous_timeout'])
async def test_system_tool_failure_stops_current_batch_and_later_react_rounds(prior_success, failure):
    from app.services.ai.adapters.base import message_groups
    registry = ToolRegistry()
    registry.register(Tool('action', 'action', {}, AsyncMock(), risk='write'))
    calls = []
    async def execute(name, args):
        calls.append(args['phase'])
        if args['phase'] == 'fail':
            if failure == 'dangerous_timeout':
                raise asyncio.TimeoutError()
            return ToolResult(False, error='disk full', tool_name=name, error_category=ErrorCategory.SYSTEM_ERROR)
        return ToolResult(True, data={'project_id': 'created'}, tool_name=name)
    registry.execute = execute
    phases = (['create'] if prior_success else []) + ['fail', 'must-not-run']
    adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
        ToolCallRequest(str(i), 'action', {'phase': phase}) for i, phase in enumerate(phases)]),
        ToolCallResponse(tool_calls=[ToolCallRequest('retry', 'action', {'phase': 'must-not-retry'})])])
    agent = make_agent(adapter, registry)
    result = await agent.run('act safely')
    assert not result.success and agent.state == AgentState.FAILED
    assert calls == (['create'] if prior_success else []) + ['fail']
    assert adapter.chat_with_tools_calls == 1
    data = result.plan[0].result.data
    assert data['total_calls'] == len(calls)
    assert data['passed'] == int(prior_success) and data['failed'] == 1
    group = next(g for g in message_groups(agent.memory.messages) if g[0].tool_calls)
    assert len(group) == len(phases) + 1 and 'Not executed' in group[-1].content


@pytest.mark.asyncio
async def test_overlapping_stream_cannot_replace_or_detach_first_stream_queue():
    agent = talking_agent()
    _, release, steps = pause_steps(agent)
    first = agent.run_stream('first')
    second = agent.run_stream('second')
    try:
        assert (await asyncio.wait_for(anext(first), 2)).type == 'step_start'
        original_queue = agent._stream_queue
        rejected = await asyncio.wait_for(anext(second), 2)
        assert rejected.type == 'done' and rejected.data['success'] is False
        assert agent._stream_queue is original_queue
        await second.aclose()
        assert agent._stream_queue is original_queue
        release.set()
        events = await asyncio.wait_for(_collect_agent_events(first), 2)
        assert events[-1].type == 'done' and events[-1].data['success'] is True
        assert len(steps) == 1
    finally:
        release.set()
        await second.aclose()
        await first.aclose()


async def _collect_agent_events(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_stream_owns_agent_until_final_event_is_consumed_or_closed():
    adapter = FakeAdapter()
    agent = talking_agent(adapter)
    stream = agent.run_stream('first')
    try:
        while (await asyncio.wait_for(anext(stream), 2)).type != 'done':
            pass
        # The async generator is paused at its final yield, not yet cleaned up.
        result = await agent.run('overlap')
        assert not result.success and adapter.chat_with_tools_calls == 1
    finally:
        await stream.aclose()
    assert agent._stream_queue is None
    assert (await agent.run('after close')).success


@pytest.mark.asyncio
async def test_stream_rejects_active_plain_run_without_attaching_queue():
    agent = talking_agent()
    entered, release, _ = pause_steps(agent)
    running = asyncio.create_task(agent.run('plain'))
    stream = agent.run_stream('overlap')
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rejected = await asyncio.wait_for(anext(stream), 2)
        assert rejected.type == 'done' and not rejected.data['success']
        assert agent._stream_queue is None
    finally:
        release.set()
        await stream.aclose()
        await asyncio.wait_for(running, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [['', 'ok'], ['same', 'same'], ['   ', 'ok']])
async def test_invalid_native_call_ids_rejected_before_any_tool_execution(ids):
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
        ToolCallRequest(call_id, 'echo_tool', {'msg': call_id}) for call_id in ids])])
    agent = make_agent(adapter, registry)
    result = await agent._execute_react_step(PlanStep(1, 'echo'))
    assert not result.success and result.error_category == ErrorCategory.INPUT_ERROR
    assert not calls and not agent.memory.messages


@pytest.mark.asyncio
@pytest.mark.parametrize('category', [ErrorCategory.INPUT_ERROR, ErrorCategory.RETRYABLE, ErrorCategory.CANCELLED])
async def test_mixed_tool_batch_preserves_success_and_closes_cancelled_calls(category):
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    from app.services.ai.adapters.base import message_groups
    calls = []
    registry = ToolRegistry()
    async def handler(mode):
        calls.append(mode)
        if mode == 'fail':
            return {'success': False, 'jmx_path': '/plans/not-created.jmx',
                    'status': 'cancelled' if category == ErrorCategory.CANCELLED else 'failed',
                    'error': 'temporarily unavailable' if category == ErrorCategory.RETRYABLE else 'invalid input'}
        return {'project_id': 'created-project', 'jmx_path': '/plans/created.jmx'}
    registry.register(Tool('artifact', 'artifact', {'type': 'object', 'properties': {'mode': {'type': 'string'}}}, handler))
    requested = [ToolCallRequest('one', 'artifact', {'mode': 'create'}),
                 ToolCallRequest('two', 'artifact', {'mode': 'fail'})]
    if category == ErrorCategory.CANCELLED:
        requested.append(ToolCallRequest('three', 'artifact', {'mode': 'not-run'}))
    adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=requested),
                                          ToolCallResponse(text='PARTIAL_ONLY')])
    agent = make_agent(adapter, registry)
    result = await agent.run('create artifacts')
    # A step with completed work is never replayed, whatever its failure.
    assert not result.success and calls == ['create', 'fail'] and result.plan[0].retries == 0
    data = result.plan[0].result.data
    assert data['total_calls'] == 2 and data['passed'] == 1 and data['failed'] == 1
    assert data['tool_statuses'][1]['success'] is False
    extracted = WorkflowAgent._extract_workflow_results(result)
    assert extracted['project_id'] == 'created-project' and extracted['test_plan_ids'] == ['created']
    if category == ErrorCategory.CANCELLED:
        group = next(g for g in message_groups(agent.memory.messages) if g[0].tool_calls)
        assert len(group) == 4 and 'Not executed' in group[-1].content
        assert adapter.chat_with_tools_calls == 1
    else:
        assert data['final_text'] == 'PARTIAL_ONLY' and result.output == 'PARTIAL_ONLY'
        assert 'partial_results' in event_types(agent)


@pytest.mark.asyncio
async def test_transient_model_failure_before_tools_still_retries_once():
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))
    adapter = FakeAdapter()
    adapter.chat_with_tools = AsyncMock(side_effect=[RuntimeError('connection unavailable'),
        ToolCallResponse(tool_calls=[ToolCallRequest('one', 'echo_tool', {'msg': 'only once'})]),
        ToolCallResponse(text='done')])
    agent = make_agent(adapter, registry)
    result = await agent.run('echo')
    assert result.success and calls == ['only once'] and result.plan[0].retries == 1
    assert len(result.plan[0].result.data['tool_results']) == 1
    assert result.iterations == 2 and 'step_retry' in event_types(agent)


@pytest.mark.asyncio
def test_document_task_keeps_the_whole_document_and_requirements():
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    task = WorkflowAgent.build_task('x' * 8100 + ' GET /TAIL_ENDPOINT', 'r' * 2100 + ' REQUIREMENT_TAIL')
    assert 'GET /TAIL_ENDPOINT' in task and 'REQUIREMENT_TAIL' in task


@pytest.mark.asyncio
async def test_oversized_workflow_stream_fails_before_model_or_tools(monkeypatch):
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    adapter = FakeAdapter()
    items = [item async for item in WorkflowAgent(adapter).stream_from_doc('x' * 100000)]
    assert items[-1][0] == 'result' and items[-1][1]['success'] is False
    assert 'budget' in items[-1][1]['error'].lower()
    assert not adapter.chat_calls and adapter.chat_with_tools_calls == 0


@pytest.mark.asyncio
async def test_generating_plans_needs_only_the_tools_that_create_them():
    from app.services.ai.agents.workflow_agent import PLAN_ONLY_ROUNDS, WorkflowAgent
    agent = WorkflowAgent(FakeAdapter())
    assert set(agent.tools._tools) == {'create_project', 'create_jmx'}
    assert agent.max_react_per_step == PLAN_ONLY_ROUNDS


@pytest.mark.asyncio
async def test_a_generated_plan_comes_from_one_conversation():
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    registry = ToolRegistry()
    calls = []
    async def create_project(name: str = ''):
        calls.append('project')
        return {'project_id': 'p1'}
    async def create_jmx(name: str = ''):
        calls.append('plan')
        return {'test_plan_id': 'plan1', 'jmx_path': 'plan1.jmx'}
    for name, handler in (('create_project', create_project), ('create_jmx', create_jmx)):
        registry.register(Tool(name=name, description=name, parameters={
            'type': 'object', 'properties': {'name': {'type': 'string'}}}, handler=handler, risk='draft'))
    adapter = FakeAdapter(tool_responses=[
        ToolCallResponse(tool_calls=[ToolCallRequest('c1', 'create_project', {'name': 'shop'})]),
        ToolCallResponse(tool_calls=[ToolCallRequest('c2', 'create_jmx', {'name': 'baseline'})]),
        ToolCallResponse(text='Created project p1 and plan plan1.')])
    agent = WorkflowAgent(adapter, tools=registry)
    items = [item async for item in agent.stream_from_doc('GET /x', 'r')]
    result = items[-1][1]
    assert calls == ['project', 'plan'] and result['success']
    assert result['project_id'] == 'p1' and result['test_plan_ids'] == ['plan1']
    assert result['diagnostic_summary'] == 'Created project p1 and plan plan1.'
    assert adapter.chat_with_tools_calls == 3 and not adapter.chat_calls


@pytest.mark.asyncio
async def test_generating_plans_asks_for_no_resource_checks():
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    task = WorkflowAgent.build_task('GET /fixture')
    policy = WorkflowAgent(FakeAdapter(), tools=ToolRegistry()).system_prompt
    assert 'CPU > 80%' not in task and '内存 > 85%' not in task
    assert 'CPU>80%' not in policy and 'mem>85%' not in policy
    assert 'check_system_resources' not in task and 'check_system_resources' not in policy
    assert 'Creating or inspecting a plan needs no resource check' in policy


@pytest.mark.asyncio
async def test_oversized_tool_batch_is_rejected_before_any_side_effect():
    calls = []
    registry = ToolRegistry(); registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
        ToolCallRequest(id=f'call-{i}', name='echo_tool', arguments={'msg': str(i)}) for i in range(9)])])
    agent = make_agent(adapter, registry)
    result = await agent._execute_react_step(PlanStep(1, 'echo'))
    assert not result.success
    assert not calls
    assert 'batch' in result.error.lower()


@pytest.mark.asyncio
async def test_dangerous_tool_timeout_is_not_automatically_retried(monkeypatch):
    registry = ToolRegistry()
    registry.register(Tool('danger', 'danger', {}, AsyncMock(), risk='write'))
    registry.execute = AsyncMock(side_effect=asyncio.TimeoutError)
    agent = make_agent(FakeAdapter(), registry)
    result = await agent._execute_tool_with_timeout('danger', {})
    assert not result.success
    assert result.error_category == ErrorCategory.SYSTEM_ERROR


@pytest.mark.asyncio
async def test_context_budget_reserves_tool_schema_and_response_before_llm(monkeypatch):
    from app.services.ai.agent.base import settings
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', 100, raising=False)
    registry = ToolRegistry(); registry.register(make_echo_tool([]))
    adapter = FakeAdapter()
    result = await make_agent(adapter, registry)._execute_react_step(PlanStep(1, 'echo'))
    assert not result.success
    assert 'budget' in result.error.lower()
    assert adapter.chat_with_tools_calls == 0


class TestRunMainFlow:
    @pytest.mark.asyncio
    async def test_run_success_with_tool_call(self):
        echo_calls = []
        registry = ToolRegistry()
        registry.register(make_echo_tool(echo_calls))

        adapter = FakeAdapter(
            tool_responses=[
                ToolCallResponse(tool_calls=[
                    ToolCallRequest(id="1", name="echo_tool", arguments={"msg": "hello"})
                ]),
                ToolCallResponse(text="步骤完成"),
            ],
        )
        agent = make_agent(adapter, registry)

        result = await agent.run("执行回声测试")

        assert result.success is True
        assert result.output == "步骤完成"  # The model's closing reply; no extra summary call.
        assert not adapter.chat_calls
        assert echo_calls == ["hello"]  # tool executed with LLM-chosen args
        assert len(result.plan) == 1 and result.plan[0].success is True
        types = event_types(agent)
        for expected in ("step_start", "tool_call", "tool_result", "react_done", "complete"):
            assert expected in types, f"missing event {expected}"
        assert types.index("step_start") < types.index("tool_call") < types.index("complete")
        assert agent.state == AgentState.COMPLETED

    @pytest.mark.asyncio
    async def test_tool_error_is_classified_and_ends_the_run(self):
        async def failing_handler() -> dict:
            raise FileNotFoundError("plan.jmx not found")

        registry = ToolRegistry()
        registry.register(Tool(
            name="bad_tool", description="always fails",
            parameters={"type": "object", "properties": {}},
            handler=failing_handler,
        ))
        adapter = FakeAdapter(
            tool_responses=[
                ToolCallResponse(tool_calls=[
                    ToolCallRequest(id="1", name="bad_tool", arguments={})
                ]),
                ToolCallResponse(text="放弃该步骤"),
            ],
        )
        agent = make_agent(adapter, registry)

        result = await agent.run("执行会失败的任务")

        assert result.plan[0].success is False
        assert result.plan[0].result.error_category == ErrorCategory.INPUT_ERROR
        assert result.success is False and 'not found' in result.error
        assert result.output == "放弃该步骤"  # What the model said it did stays visible.
        assert adapter.chat_with_tools_calls == 2 and agent.state == AgentState.FAILED

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_retried_up_to_its_limit(self):
        agent = talking_agent()
        execute = AsyncMock(return_value=ToolResult(False, error='timeout', error_category=ErrorCategory.RETRYABLE))
        agent._execute_react_step = execute
        result = await agent.run('task')
        assert execute.await_count == 1 + result.plan[0].max_retries
        assert result.iterations == execute.await_count
        assert event_types(agent).count('step_retry') == result.plan[0].max_retries
        assert not result.success and result.error == 'timeout'

    @pytest.mark.asyncio
    async def test_cancel_between_retries_stops_the_run(self):
        agent = talking_agent()

        async def fail_then_cancel(step):
            agent.cancel()
            return ToolResult(False, error='timeout', error_category=ErrorCategory.RETRYABLE)

        agent._execute_react_step = AsyncMock(side_effect=fail_then_cancel)

        result = await agent.run("任务")

        assert result.success is False
        assert result.error == "Cancelled by user"
        agent._execute_react_step.assert_awaited_once()


class TestRunStream:
    @pytest.mark.asyncio
    async def test_events_streamed_with_done_terminal(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        adapter = FakeAdapter(
            tool_responses=[
                ToolCallResponse(tool_calls=[
                    ToolCallRequest(id="1", name="echo_tool", arguments={"msg": "m"})
                ]),
                ToolCallResponse(text="done"),
            ],
        )
        agent = make_agent(adapter, registry)

        streamed = [e async for e in agent.run_stream("任务")]

        assert len(streamed) > 0
        last = streamed[-1]
        assert last.type == "done"
        assert last.data["success"] is True
        assert last.data["output"] == "done"
        # events arrive in execution order, not all at the end
        types = [e.type for e in streamed]
        assert types.index("step_start") < types.index("tool_call") < types.index("done")
        # stream queue is detached after the run
        assert agent._stream_queue is None


class TestReactLoop:

    @pytest.mark.asyncio
    async def test_overlapping_runs_of_one_agent_are_rejected(self):
        agent = talking_agent()
        entered, release, steps = pause_steps(agent)
        running = asyncio.create_task(agent.run('first'))
        await entered.wait()
        try:
            result = await asyncio.wait_for(agent.run('second'), timeout=1)
            assert not result.success and 'running task' in result.error
            assert len(steps) == 1
        finally:
            release.set()
            assert (await running).success

    def test_subclasses_share_agent_concurrency_budget(self, monkeypatch):
        class First(BaseAgent):
            pass
        class Second(BaseAgent):
            pass
        monkeypatch.setattr(BaseAgent, '_concurrency_semaphore', None)
        assert First._get_concurrency_semaphore() is Second._get_concurrency_semaphore()

    @pytest.mark.asyncio
    async def test_a_run_waits_for_a_free_agent_slot(self, monkeypatch):
        monkeypatch.setattr(BaseAgent, '_concurrency_semaphore', asyncio.Semaphore(1))
        first, second = talking_agent(), talking_agent()
        entered, release, _ = pause_steps(first)
        running = asyncio.create_task(first.run('first'))
        await entered.wait()
        waiting = asyncio.create_task(second.run('second'))
        await asyncio.sleep(0.05)
        assert not waiting.done() and second.adapter.chat_with_tools_calls == 0
        release.set()
        assert (await running).success and (await waiting).success

    @pytest.mark.asyncio
    async def test_react_preserves_native_call_ids_in_observation(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[ToolCallRequest('native-1', 'echo_tool', {'msg': 'hi'})]),
                                               ToolCallResponse(text='done')])
        agent = make_agent(adapter, registry)
        assert (await agent.run('echo')).success
        messages = adapter.tool_messages[1]
        assert messages[-2].tool_calls[0]['id'] == 'native-1'
        assert messages[-1].role == 'tool' and messages[-1].tool_call_id == 'native-1'

    @pytest.mark.asyncio
    async def test_max_react_per_step_bounds_tool_calls(self):
        echo_calls = []
        registry = ToolRegistry()
        registry.register(make_echo_tool(echo_calls))
        # LLM keeps requesting tools forever — the loop must stop at the cap
        adapter = FakeAdapter(tool_responses=[
            ToolCallResponse(tool_calls=[
                ToolCallRequest(id=str(i), name="echo_tool", arguments={"msg": str(i)})
            ])
            for i in range(10)
        ])
        agent = make_agent(adapter, registry, max_react_per_step=2)

        result = await agent.run("任务")

        assert len(echo_calls) == 2  # capped by max_react_per_step
        assert "react_max" in event_types(agent)
        assert result.success is False


class TestAuditRegressions:
    @pytest.mark.asyncio
    async def test_a_run_executes_the_task_with_its_tools(self):
        calls = []
        registry = ToolRegistry()
        registry.register(make_echo_tool(calls))
        adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
            ToolCallRequest('1', 'echo_tool', {'msg': 'created'})]), ToolCallResponse(text='done')])
        agent = make_agent(adapter, registry)
        result = await agent.run('Use the echo tool')
        assert calls == ['created']
        assert result.success

    @pytest.mark.asyncio
    async def test_a_run_without_tools_fails_before_the_model(self):
        adapter = FakeAdapter()
        result = await make_agent(adapter).run('task')
        assert not result.success and 'No tools' in result.error
        assert adapter.chat_with_tools_calls == 0 and not adapter.chat_calls

    @pytest.mark.asyncio
    async def test_react_receives_task_context_history_and_prior_results(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        adapter = FakeAdapter(tool_responses=[
            ToolCallResponse(tool_calls=[ToolCallRequest('1', 'echo_tool', {'msg': 'RESOURCE_ID_17'})]),
            ToolCallResponse(text='done')])
        agent = make_agent(adapter, registry)
        agent.memory.add_user('OLD_SESSION_SENTINEL')
        result = await agent.run('ORIGINAL_TASK_SENTINEL', context='CONTEXT_SENTINEL')
        assert result.success
        text = '\n'.join(m.content for m in adapter.tool_messages[1])
        for marker in ['OLD_SESSION_SENTINEL', 'ORIGINAL_TASK_SENTINEL', 'CONTEXT_SENTINEL', 'RESOURCE_ID_17']:
            assert marker in text
        assert sum(m.role == 'system' for m in adapter.tool_messages[1]) == 1
        assert not any(m.role == 'system' for m in agent.memory.messages)

    @pytest.mark.asyncio
    async def test_current_task_survives_memory_window_eviction(self):
        from app.services.ai.agent.memory import ConversationMemory
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        adapter = FakeAdapter(tool_responses=[
            ToolCallResponse(tool_calls=[ToolCallRequest('1', 'echo_tool', {'msg': 'first'})]),
            ToolCallResponse(tool_calls=[ToolCallRequest('2', 'echo_tool', {'msg': 'second'})]),
            ToolCallResponse(text='done')])
        agent = make_agent(adapter, registry)
        agent.memory = ConversationMemory(max_messages=3)
        result = await agent.run('PINNED_CURRENT_TASK', context='PINNED_CURRENT_CONSTRAINT')
        assert result.success
        last_round = '\n'.join(m.content for m in adapter.tool_messages[2])
        assert last_round.count('PINNED_CURRENT_TASK') == 1
        assert last_round.count('PINNED_CURRENT_CONSTRAINT') == 1

    @pytest.mark.asyncio
    async def test_no_tool_action_cannot_complete_a_tool_step(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        agent = make_agent(FakeAdapter(), registry)
        result = await agent.run('task')
        assert not result.success
        assert 'No tool' in result.plan[0].result.error

    @pytest.mark.asyncio
    async def test_a_cancelled_step_is_not_retried(self):
        agent = talking_agent()
        agent._execute_react_step = AsyncMock(return_value=ToolResult(
            False, error='Cancelled by user', error_category=ErrorCategory.CANCELLED))
        result = await agent.run('task')
        assert not result.success
        assert result.error == 'Cancelled by user'
        assert agent.state == AgentState.CANCELLED
        agent._execute_react_step.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('cancel_via_result', [False, True])
    async def test_cancel_stops_remaining_tools_in_same_response(self, cancel_via_result):
        calls = []

        async def handler(msg=''):
            calls.append(msg)
            if cancel_via_result:
                return {'status': 'cancelled'}
            agent.cancel()
            return {'message': msg}

        registry = ToolRegistry()
        registry.register(Tool('echo_tool', 'echo', {'type': 'object', 'properties': {'msg': {'type': 'string'}}}, handler))
        adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
            ToolCallRequest('1', 'echo_tool', {'msg': 'first'}),
            ToolCallRequest('2', 'echo_tool', {'msg': 'must not run'})])])
        agent = make_agent(adapter, registry)
        result = await agent.run('task')
        assert calls == ['first']
        assert not result.success
        assert agent.state == AgentState.CANCELLED
        assert adapter.chat_with_tools_calls == 1

    def test_specialized_prompt_keeps_shared_safety_rules(self):
        agent = make_agent(FakeAdapter(), system_prompt='Only analyze results.')
        assert 'Only analyze results.' in agent.system_prompt
        assert 'NEVER delete' in agent.system_prompt
        assert 'untrusted' in agent.system_prompt

    @pytest.mark.parametrize('nested', [False, True])
    def test_workflow_extracts_raw_tool_results(self, nested):
        from app.services.ai.agents.workflow_agent import WorkflowAgent
        from app.services.ai.agent.types import AgentResult
        data = {'project_id': 'project-1', 'jmx_path': 'plan-1.jmx'}
        if nested:
            data = {'react_iterations': 2, 'tool_results': [data, data]}
        result = AgentResult(True, 'task', plan=[PlanStep(1, 'create', success=True, result=ToolResult(True, data=data))])
        extracted = WorkflowAgent._extract_workflow_results(result)
        assert extracted['project_id'] == 'project-1'
        assert extracted['test_plan_ids'] == ['plan-1']

    def test_workflow_prefers_the_registered_plan_id_over_the_file_name(self):
        from app.services.ai.agents.workflow_agent import WorkflowAgent
        from app.services.ai.agent.types import AgentResult
        data = {'project_id': 'p', 'jmx_path': 'test_plans/p/237f8f15.jmx', 'test_plan_id': '8bbad6ad'}
        result = AgentResult(True, 'task', plan=[PlanStep(1, 'create', success=True, result=ToolResult(True, data=data))])
        assert WorkflowAgent._extract_workflow_results(result)['test_plan_ids'] == ['8bbad6ad']

    def test_workflow_counts_a_plan_once_even_when_other_tools_return_its_path(self):
        from app.services.ai.agents.workflow_agent import WorkflowAgent
        from app.services.ai.agent.types import AgentResult
        created = {'tool_statuses': [{'tool': 'get_jmx_content', 'success': True, 'error': None},
                                     {'tool': 'create_jmx', 'success': True, 'error': None},
                                     {'tool': 'get_jmx_content', 'success': True, 'error': None},
                                     {'tool': 'create_jmx', 'success': True, 'error': None}],
                   'tool_results': [{'jmx_path': 'test_plans/p/b2.jmx'},
                                    {'jmx_path': 'test_plans/p/a1.jmx', 'test_plan_id': 'plan-a'},
                                    {'jmx_path': 'test_plans/p/a1.jmx', 'content': '<jmeterTestPlan/>'},
                                    {'jmx_path': 'test_plans/p/b2.jmx', 'test_plan_id': 'plan-b'}]}
        result = AgentResult(True, 'task', plan=[
            PlanStep(1, 'create plans', success=True, result=ToolResult(True, data=created))])
        assert WorkflowAgent._extract_workflow_results(result)['test_plan_ids'] == ['plan-b', 'plan-a']


class TestToolResultContract:
    @pytest.mark.asyncio
    @pytest.mark.parametrize('payload', [
        {'error': 'failed'}, {'success': False}, {'valid': False},
        {'status': 'failed', 'error_message': 'failed'}, {'status': 'cancelled'},
    ])
    async def test_explicit_business_failure_is_not_success(self, payload):
        registry = ToolRegistry()
        registry.register(Tool('result', 'result', {'type': 'object'}, AsyncMock(return_value=payload)))
        result = await registry.execute('result', {})
        assert not result.success
        assert result.error
        assert result.data == payload
        if payload.get('status') == 'cancelled':
            assert result.error_category == ErrorCategory.CANCELLED

    @pytest.mark.asyncio
    async def test_null_error_is_not_a_failure(self):
        registry = ToolRegistry()
        registry.register(Tool('result', 'result', {'type': 'object'}, AsyncMock(return_value={'success': True, 'error': None})))
        assert (await registry.execute('result', {})).success


class TestToolTimeout:
    @pytest.mark.asyncio
    async def test_closing_stream_awaits_runner_cleanup(self):
        agent = make_agent(FakeAdapter())
        cleaned = []
        async def run(*args):
            agent._emit('started', 'started')
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(True)
        agent.run = run
        stream = agent.run_stream('task')
        assert (await anext(stream)).type == 'started'
        await stream.aclose()
        assert cleaned == [True]
        assert agent._stream_queue is None and agent._stream_runner is None

    @pytest.mark.asyncio
    async def test_timeout_classified_retryable(self, monkeypatch):
        from app.core.config import settings
        monkeypatch.setattr(settings, "AGENT_TOOL_TIMEOUT_SECONDS", 0.05)

        async def slow_handler() -> dict:
            await asyncio.sleep(5)
            return {}

        registry = ToolRegistry()
        registry.register(Tool(
            name="slow_tool", description="never finishes",
            parameters={"type": "object", "properties": {}},
            handler=slow_handler,
        ))
        agent = make_agent(FakeAdapter(), registry)

        tr = await agent._execute_tool_with_timeout("slow_tool", {})

        assert tr.success is False
        assert tr.error_category == ErrorCategory.RETRYABLE
        assert "timed out" in tr.error


@pytest.mark.asyncio
async def test_a_retry_sees_the_task_before_its_earlier_results_and_is_told_not_to_redo_them():
    # Regression: with the task after the history, the model re-ran the whole task (duplicate projects).
    calls = []

    async def flaky(msg: str = '') -> dict:
        calls.append(msg)
        if len(calls) == 1:
            raise ConnectionError('connection refused')
        return {'echo': msg}

    registry = ToolRegistry()
    registry.register(Tool('echo_tool', 'echo', {'type': 'object', 'properties': {'msg': {'type': 'string'}}}, flaky))
    adapter = FakeAdapter(tool_responses=[
        ToolCallResponse(tool_calls=[ToolCallRequest('c1', 'echo_tool', {'msg': 'first try'})]),
        ToolCallResponse(text='the tool was unreachable'),
        ToolCallResponse(tool_calls=[ToolCallRequest('c2', 'echo_tool', {'msg': 'second try'})]),
        ToolCallResponse(text='done'),
    ])
    agent = make_agent(adapter, registry)
    result = await agent.run('TASK: create the project')
    assert result.success and calls == ['first try', 'second try'] and result.plan[0].retries == 1
    retry = adapter.tool_messages[2]
    contents = [m.content or '' for m in retry]
    task_at = next(i for i, c in enumerate(contents) if c.startswith('TASK:'))
    failed_at = next(i for i, m in enumerate(retry) if m.role == 'tool' and 'connection refused' in (m.content or ''))
    assert task_at < failed_at
    assert contents.count('TASK: create the project') == 1
    assert contents[-1].startswith('Complete the task above') and 'instead of repeating those calls' in contents[-1]


@pytest.mark.asyncio
async def test_workflow_stream_yields_step_events_then_the_result():
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[
        ToolCallResponse(tool_calls=[ToolCallRequest('c1', 'echo_tool', {'msg': 'hi'})]),
        ToolCallResponse(text='## Summary\nAll good')])
    agent = WorkflowAgent(adapter, tools=registry)
    items = [item async for item in agent.stream_from_doc('GET /x', 'r')]
    kinds = [kind for kind, _ in items]
    assert kinds[-1] == 'result' and kinds.count('result') == 1
    assert 'step_start' in [payload.type for kind, payload in items if kind == 'event']
    result = items[-1][1]
    assert result['success'] and result['diagnostic_summary'] == '## Summary\nAll good'


@pytest.mark.asyncio
async def test_run_error_carries_the_failed_tool_error():
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))

    async def broken(path: str = '') -> dict:
        raise FileNotFoundError(f'no such plan: {path}')
    registry.register(Tool(name='validate', description='validate', handler=broken,
                           parameters={'type': 'object', 'properties': {'path': {'type': 'string'}}}))
    adapter = FakeAdapter(tool_responses=[
        ToolCallResponse(tool_calls=[ToolCallRequest('a', 'echo_tool', {'msg': 'created'}),
                                     ToolCallRequest('b', 'validate', {'path': 'wrong.jmx'})]),
        ToolCallResponse(text='done')])
    agent = make_agent(adapter, registry)
    result = await agent.run('task')
    assert not result.success and calls == ['created']
    assert 'wrong.jmx' in result.error and result.output == 'done'


def _flaky_registry(calls, first_error):
    async def create(value: str = '') -> dict:
        calls.append(value)
        if len(calls) == 1:
            raise ValueError(first_error)
        return {'project_id': 'p1'}
    registry = ToolRegistry()
    registry.register(Tool(name='create_project', description='create', handler=create,
                           parameters={'type': 'object', 'properties': {'value': {'type': 'string'}}}))
    return registry


def _retry_responses():
    return [ToolCallResponse(tool_calls=[ToolCallRequest('a', 'create_project', {'value': 'bad'})]),
            ToolCallResponse(tool_calls=[ToolCallRequest('b', 'create_project', {'value': 'fixed'})]),
            ToolCallResponse(text='project created')]


@pytest.mark.asyncio
async def test_an_input_error_the_model_fixes_in_the_same_step_does_not_fail_it():
    # Seen with DeepSeek: variables sent as 1000/false, rejected, resent as strings, project created.
    calls = []
    agent = make_agent(FakeAdapter(tool_responses=_retry_responses()),
                       _flaky_registry(calls, '1 validation error for ProjectEnvironment'))
    result = await agent.run('task')
    assert calls == ['bad', 'fixed'] and result.success and result.plan[0].success


@pytest.mark.asyncio
async def test_a_failure_that_is_not_an_input_error_still_fails_the_step():
    calls = []
    agent = make_agent(FakeAdapter(tool_responses=_retry_responses()),
                       _flaky_registry(calls, 'unexpected crash while writing files'))
    result = await agent.run('task')
    assert calls == ['bad', 'fixed'] and not result.success


@pytest.mark.asyncio
async def test_project_variables_from_a_model_are_stored_as_strings(monkeypatch):
    from app.services.ai.agent.tools import project as project_tools
    from app.services.db_store import db_store
    saved = {}
    monkeypatch.setattr(db_store, 'create_project', AsyncMock())  # never touch the real database
    monkeypatch.setattr(project_tools.store, 'set_project', lambda pid, project: saved.update(project=project))
    monkeypatch.setattr(project_tools.os, 'makedirs', lambda *a, **k: None)
    await project_tools.create_project(
        'QuickPizza', variables={'maxCalories': 1000, 'vegetarian': False, 'excluded': [], 'name': 'x'},
        headers={'X-Retry': 3})
    project = saved['project']
    assert project.environment.variables == {'maxCalories': '1000', 'vegetarian': 'false', 'excluded': '[]', 'name': 'x'}
    assert project.environment.headers == {'X-Retry': '3'}
