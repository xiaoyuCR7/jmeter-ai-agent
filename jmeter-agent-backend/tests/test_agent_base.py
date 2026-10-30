"""Tests for the BaseAgent plan -> react -> observe -> reflect loop.

All LLM traffic is stubbed by FakeAdapter; the planner is replaced with
AsyncMock. No network access, no real model calls.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

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


def test_planner_preserves_braces_and_escaped_quotes_inside_arguments():
    import json
    from app.services.ai.agent.planner import TaskPlanner
    data = {'steps': [{'step_id': 1, 'description': 'read template', 'tool_name': 'echo_tool',
                       'tool_args': {'msg': 'literal } and " quote text'}}]}
    plan = TaskPlanner(FakeAdapter())._parse_plan(json.dumps(data))
    assert len(plan) == 1
    assert plan[0].tool_args == data['steps'][0]['tool_args']


@pytest.mark.parametrize('payload', ['{"steps": null}', '{"steps": [null]}', '{"steps": "bad"}',
    '{"steps":[{"description":""}]}', '{"steps":[{"step_id":true,"description":"bad"}]}',
    '{"steps":[{"description":"bad","tool_args":[]}]}',
    '{"steps":[{"step_id":1,"description":"a"},{"step_id":1,"description":"b"}]}'])
def test_invalid_planner_shapes_return_no_executable_plan(payload):
    from app.services.ai.agent.planner import TaskPlanner
    assert TaskPlanner(FakeAdapter())._parse_plan(payload) == []


@pytest.mark.asyncio
async def test_thinking_step_checks_budget_after_appending_instruction():
    adapter = FakeAdapter()
    adapter.max_tokens = 1000
    agent = make_agent(adapter)
    with pytest.raises(ValueError, match='context budget'):
        await agent._execute_thinking_step(PlanStep(1, 'large step ' * 1000), 0)
    assert not adapter.chat_calls


@pytest.mark.asyncio
async def test_checkpoint_parses_nested_steps_and_keeps_safety_rules():
    import json
    adapter = FakeAdapter(chat_responses=[json.dumps({'replan': True, 'new_steps': [
        {'description': 'retry input', 'tool_name': 'echo_tool', 'tool_args': {'msg': 'literal }'}}]})])
    agent = make_agent(adapter)
    result = await agent._checkpoint_eval([PlanStep(1, 'old')], PlanStep(1, 'done'), 1)
    assert len(result) == 1 and result[0].step_id == 2
    assert adapter.chat_calls[0][0].role == 'system'


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


def event_types(agent: BaseAgent) -> list[str]:
    return [e.type for e in agent._events]


@pytest.fixture
def registered_agent(monkeypatch):
    import importlib
    from app.models.schemas import FactSheet, ValidationResult
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    from app.services.ai.agent.memory import ConversationMemory
    module = importlib.import_module('app.services.ai.agents.analysis_agent')
    provenance = importlib.import_module('app.services.ai.memory.provenance')
    adapter = FakeAdapter(chat_responses=['Measured observation', '{"overall_assessment":"Measured report"}'])
    agent = AnalysisAgent(adapter, tools=ToolRegistry(), session_id='registered-session')
    fact = FactSheet(execution_id='run-A', module='plan-A', version='unknown', test_type='baseline',
                     total_requests=10, context_scope='scope', jtl_path='fixture.jtl', jtl_sha256='digest')
    agent._build_fact_sheet = AsyncMock(return_value=fact)
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'explain measured facts')])
    monkeypatch.setattr(provenance, 'load_execution_scope', AsyncMock(return_value='scope'))
    monkeypatch.setattr(module.fact_ledger, 'get_facts_for_module_version', AsyncMock(return_value=[]))
    monkeypatch.setattr(module.hypothesis_ledger, 'list_for_module', AsyncMock(return_value=[]))
    monkeypatch.setattr(module.hypothesis_ledger, 'evaluate_module_hypotheses', AsyncMock(return_value=[]))
    monkeypatch.setattr(ConversationMemory, 'load', AsyncMock(return_value=None))
    monkeypatch.setattr(ConversationMemory, 'save', AsyncMock(return_value=True))
    agent.archiver.archive_analysis_result = AsyncMock(return_value={
        'fact_archived': True, 'validation': ValidationResult(is_valid=True),
        'hypotheses_archived': [], 'errors': []})
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
    monkeypatch.setattr(module, 'fact_ledger', ledger)
    result = await agent.analyze_with_rag(execution_id=evidence['execution_id'], module=evidence['module'])
    assert result['success'] and not result['rag']['history_found']
    assert len(agent.adapter.chat_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['preparation', 'extraction', 'archive'])
async def test_registered_analysis_manual_cancel_is_not_reset_by_inner_loop(registered_agent, phase):
    agent, evidence, _ = registered_agent
    target, method = {'preparation': (agent, '_build_fact_sheet'),
                      'extraction': (agent, '_extract_structured_analysis'),
                      'archive': (agent.archiver, 'archive_analysis_result')}[phase]
    original = getattr(target, method)
    async def cancel_here(*args, **kwargs):
        result = await original(*args, **kwargs)
        agent.cancel()
        return result
    setattr(target, method, cancel_here)
    result = await agent.run_analysis('cancel task', evidence)
    assert not result.success and result.error == 'Cancelled by user'
    assert agent.state == AgentState.CANCELLED and not agent._run_active
    if phase == 'preparation':
        assert not agent.adapter.chat_calls
    if phase == 'archive':
        assert result.analysis['archive']['fact_archived']
    else:
        agent.archiver.archive_analysis_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_analysis_runs_fact_model_archive_and_retains_task_context(registered_agent):
    import json
    agent, evidence, fact = registered_agent
    result = await agent.run_analysis('Explain the regression', evidence, context='USER_CONTEXT')
    assert result.success and result.task == 'Explain the regression' and len(result.plan) == 1
    assert result.output == '{"overall_assessment":"Measured report"}'
    assert result.analysis['fact']['execution_id'] == 'run-A'
    assert result.analysis['archive']['fact_archived']
    assert result.analysis['archive']['validation']['is_valid']
    assert result.events[0].type == 'analysis_phase'
    json.dumps(result.analysis, allow_nan=False)
    sent = agent.planner.plan.call_args.kwargs
    assert 'Explain the regression' in sent['task'] and sent['context'] == 'USER_CONTEXT'
    assert 'scope' in sent['task']
    assert len(agent.adapter.chat_calls) == 2
    assert not agent._run_active and agent.session_id not in BaseAgent._active_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['preparation', 'archive'])
async def test_registered_analysis_holds_session_through_entire_operation(registered_agent, phase):
    agent, evidence, fact = registered_agent
    entered, release = asyncio.Event(), asyncio.Event()
    target, method = (agent, '_build_fact_sheet') if phase == 'preparation' else (agent.archiver, 'archive_analysis_result')
    original = getattr(target, method)
    async def pause(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    setattr(target, method, pause)
    running = asyncio.create_task(agent.run_analysis('first', evidence))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not (await agent.run('overlap')).success
        assert not (await agent.run_analysis('overlap', evidence)).success
        other = make_agent(FakeAdapter(), session_id=agent.session_id)
        assert not (await other.run('same session')).success
        assert agent._run_active
    finally:
        release.set()
        result = await running
    assert result.success
    assert agent.session_id not in BaseAgent._active_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize('archive_fails', [False, True])
async def test_registered_analysis_stream_finishes_only_after_archival(registered_agent, archive_fails):
    import json
    agent, evidence, fact = registered_agent
    observed = []
    async def archive(**kwargs):
        observed.extend(event_types(agent))
        assert agent.state != AgentState.COMPLETED
        return {'fact_archived': True, 'errors': ['derived write failed'] if archive_fails else []}
    agent.archiver.archive_analysis_result = archive
    events = [event async for event in agent.run_analysis_stream('analyze', evidence)]
    assert 'complete' not in observed and 'done' not in observed
    assert 'analysis_ready' in observed
    assert sum(event.type == 'done' for event in events) == 1
    final = events[-1]
    assert final.type == 'done' and final.data['success'] is (not archive_fails)
    assert final.data['analysis']['archive']['fact_archived']
    assert bool(final.data['error']) is archive_fails
    assert agent.state == (AgentState.FAILED if archive_fails else AgentState.COMPLETED)
    json.dumps(final.data, allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['preparation', 'archive'])
async def test_registered_analysis_stream_close_cancels_worker_and_releases_session(registered_agent, phase):
    agent, evidence, fact = registered_agent
    entered, stopped = asyncio.Event(), asyncio.Event()
    target, method = (agent, '_build_fact_sheet') if phase == 'preparation' else (agent.archiver, 'archive_analysis_result')
    async def pause(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    setattr(target, method, pause)
    stream = agent.run_analysis_stream('analyze', evidence)
    try:
        while not entered.is_set():
            event = await asyncio.wait_for(anext(stream), 5)
            assert event.type not in ('complete', 'done')
    finally:
        await stream.aclose()
    assert stopped.is_set()
    assert agent._stream_queue is None and agent._stream_runner is None
    assert not agent._run_active and agent.session_id not in BaseAgent._active_sessions
    assert agent.state == AgentState.CANCELLED


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
    assert not agent.adapter.chat_calls
    agent.archiver.archive_analysis_result.assert_not_awaited()


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
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'act', 'action')])
    agent.planner.replan = AsyncMock()
    result = await agent.run('act safely')
    assert not result.success and agent.state == AgentState.FAILED
    assert calls == (['create'] if prior_success else []) + ['fail']
    assert adapter.chat_with_tools_calls == 1
    agent.planner.replan.assert_not_awaited()
    data = result.plan[0].result.data
    assert data['total_calls'] == len(calls)
    assert data['passed'] == int(prior_success) and data['failed'] == 1
    group = next(g for g in message_groups(agent.memory.messages) if g[0].tool_calls)
    assert len(group) == len(phases) + 1 and 'Not executed' in group[-1].content


@pytest.mark.asyncio
async def test_overlapping_stream_cannot_replace_or_detach_first_stream_queue():
    release = asyncio.Event()
    agent = make_agent(FakeAdapter())
    async def plan(**kwargs):
        await release.wait()
        return [PlanStep(1, 'think')]
    agent.planner.plan = AsyncMock(side_effect=plan)
    first = agent.run_stream('first')
    second = agent.run_stream('second')
    try:
        assert (await asyncio.wait_for(anext(first), 2)).type == 'planning'
        original_queue = agent._stream_queue
        rejected = await asyncio.wait_for(anext(second), 2)
        assert rejected.type == 'done' and rejected.data['success'] is False
        assert agent._stream_queue is original_queue
        await second.aclose()
        assert agent._stream_queue is original_queue
        release.set()
        events = await asyncio.wait_for(_collect_agent_events(first), 2)
        assert events[-1].type == 'done' and events[-1].data['success'] is True
        assert any(e.type == 'plan_ready' for e in events)
        assert agent.planner.plan.await_count == 1
    finally:
        release.set()
        await second.aclose()
        await first.aclose()


async def _collect_agent_events(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_stream_owns_agent_until_final_event_is_consumed_or_closed():
    agent = make_agent(FakeAdapter())
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'think')])
    stream = agent.run_stream('first')
    try:
        while (await asyncio.wait_for(anext(stream), 2)).type != 'done':
            pass
        # The async generator is paused at its final yield, not yet cleaned up.
        result = await agent.run('overlap')
        assert not result.success and agent.planner.plan.await_count == 1
    finally:
        await stream.aclose()
    assert agent._stream_queue is None
    assert (await agent.run('after close')).success


@pytest.mark.asyncio
async def test_stream_rejects_active_plain_run_without_attaching_queue():
    entered, release = asyncio.Event(), asyncio.Event()
    agent = make_agent(FakeAdapter())
    async def plan(**kwargs):
        entered.set()
        await release.wait()
        return [PlanStep(1, 'think')]
    agent.planner.plan = AsyncMock(side_effect=plan)
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
async def test_failed_session_load_aborts_before_work_and_does_not_save(monkeypatch):
    from app.services.ai.agent.memory import ConversationMemory
    adapter = FakeAdapter()
    agent = make_agent(adapter, session_id='unreadable')
    agent.memory.add_user('unsaved local data')
    before = agent.memory.to_dict()
    load = AsyncMock(side_effect=RuntimeError('database unavailable'))
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(ConversationMemory, 'load', load)
    monkeypatch.setattr(ConversationMemory, 'save', save)
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'think')])
    result = await agent.run('do work')
    assert not result.success and agent._state == AgentState.FAILED
    assert 'memory' in result.error.lower()
    assert agent.memory.to_dict() == before
    agent.planner.plan.assert_not_awaited()
    save.assert_not_awaited()
    assert not adapter.chat_calls and adapter.chat_with_tools_calls == 0
    assert not agent._run_active and agent.session_id not in BaseAgent._active_sessions
    load.side_effect = None
    load.return_value = ConversationMemory(session_id=agent.session_id)
    assert (await agent.run('retry after recovery')).success


@pytest.mark.asyncio
@pytest.mark.parametrize('persistent', [True, False])
async def test_missing_session_discards_stale_memory_only_when_persistent(monkeypatch, persistent):
    from app.core.config import settings
    from app.services.ai.agent.memory import ConversationMemory
    monkeypatch.setattr(settings, 'MEMORY_PERSIST_ENABLED', persistent)
    monkeypatch.setattr(ConversationMemory, 'load', AsyncMock(return_value=None))
    monkeypatch.setattr(ConversationMemory, 'save', AsyncMock(return_value=True))
    adapter = FakeAdapter()
    agent = make_agent(adapter, session_id='expired')
    agent.memory = ConversationMemory(session_id=agent.session_id, max_messages=12, summary_trigger=8, summary_keep=3)
    agent.memory.add_user('STALE_HISTORY_MUST_EXPIRE')
    agent.memory._summary = 'STALE_SUMMARY_MUST_EXPIRE'
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'think')])
    assert (await agent.run('current task')).success
    prompt = '\n'.join(m.content for m in adapter.chat_calls[0])
    assert ('STALE_HISTORY_MUST_EXPIRE' in prompt) is not persistent
    assert ('STALE_SUMMARY_MUST_EXPIRE' in prompt) is not persistent
    assert (agent.memory.max_messages, agent.memory.summary_trigger, agent.memory.summary_keep) == (12, 8, 3)


@pytest.mark.asyncio
async def test_save_exception_emits_warning_without_replaying_work(monkeypatch):
    from app.services.ai.agent.memory import ConversationMemory
    monkeypatch.setattr(ConversationMemory, 'load', AsyncMock(return_value=None))
    monkeypatch.setattr(ConversationMemory, 'save', AsyncMock(side_effect=OSError('disk full')))
    adapter = FakeAdapter()
    agent = make_agent(adapter, session_id='save-failed')
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'think')])
    result = await agent.run('task')
    assert result.success and len(adapter.chat_calls) == 2
    assert 'memory_not_saved' in event_types(agent)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{'prefix': 'measured data ' * 60, 'tail': 'TAIL_ARTIFACT_42'},
                                   ['observations ' * 60, 'TAIL_ARTIFACT_42'], 0, False])
@pytest.mark.parametrize('success', [True, False])
async def test_summary_preserves_structured_partial_and_falsy_results(payload, success):
    import json
    adapter = FakeAdapter()
    agent = make_agent(adapter)
    step = PlanStep(1, 'work', success=success,
                    result=ToolResult(success, data=payload, error=None if success else 'later validation failed'))
    await agent._generate_final_response('task', [step])
    prompt = adapter.chat_calls[0][-1].content
    assert json.dumps(payload, ensure_ascii=False, separators=(',', ':')) in prompt
    if not success:
        assert 'later validation failed' in prompt and '失败' in prompt


@pytest.mark.asyncio
async def test_structured_summary_over_budget_does_not_send_incomplete_evidence():
    adapter = FakeAdapter()
    adapter.max_tokens = 1000
    agent = make_agent(adapter)
    step = PlanStep(1, 'work', success=True, result=ToolResult(True, data={'facts': '中文证据' * 2000}))
    with pytest.raises(ValueError, match='context budget'):
        await agent._generate_final_response('task', [step])
    assert not adapter.chat_calls


@pytest.mark.asyncio
async def test_checkpoint_receives_full_structured_evidence():
    adapter = FakeAdapter(chat_responses=['{"replan": false}'])
    agent = make_agent(adapter)
    step = PlanStep(1, 'work', success=True,
                    result=ToolResult(True, data={'details': 'data ' * 130, 'status': 'TAIL_NOT_READY'}))
    await agent._checkpoint_eval([step, PlanStep(2, 'next')], step, 1)
    assert 'TAIL_NOT_READY' in adapter.chat_calls[0][-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [['', 'ok'], ['same', 'same'], ['   ', 'ok']])
async def test_invalid_native_call_ids_rejected_before_any_tool_execution(ids):
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
        ToolCallRequest(call_id, 'echo_tool', {'msg': call_id}) for call_id in ids])])
    agent = make_agent(adapter, registry)
    result = await agent._execute_react_step(PlanStep(1, 'echo', 'echo_tool'), 0)
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
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'create and validate', 'artifact')])
    agent.planner.replan = AsyncMock(return_value=[PlanStep(1, 'repeat', 'artifact')])
    result = await agent.run('create artifacts')
    assert not result.success and calls == ['create', 'fail']
    agent.planner.replan.assert_not_awaited()
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
        assert data['final_text'] == 'PARTIAL_ONLY'


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
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'echo', 'echo_tool')])
    result = await agent.run('echo')
    assert result.success and calls == ['only once'] and result.plan[0].retries == 1
    assert len(result.plan[0].result.data['tool_results']) == 1


@pytest.mark.asyncio
async def test_session_load_failure_is_a_failed_stream_terminal_event(monkeypatch):
    from app.services.ai.agent.memory import ConversationMemory
    monkeypatch.setattr(ConversationMemory, 'load', AsyncMock(side_effect=RuntimeError('unavailable')))
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(ConversationMemory, 'save', save)
    agent = make_agent(FakeAdapter(), session_id='stream-read-failed')
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'think')])
    events = [event async for event in agent.run_stream('task')]
    assert events[-1].type == 'done' and events[-1].data['success'] is False
    assert any(event.type == 'error' for event in events)
    agent.planner.plan.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_planning_failure_sets_terminal_failed_state():
    agent = make_agent(FakeAdapter())
    agent.planner.plan = AsyncMock(side_effect=RuntimeError('planner unavailable'))
    result = await agent.run('task')
    assert not result.success and agent._state == AgentState.FAILED
    assert 'error' in event_types(agent)


@pytest.mark.asyncio
async def test_thinking_result_reaches_final_summary_without_silent_tail_loss():
    report = 'Measured observations. ' * 30 + 'TAIL_EVIDENCE_DO_NOT_OMIT'
    adapter = FakeAdapter(chat_responses=[report, 'final report'])
    agent = make_agent(adapter)
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'analyze')])
    result = await agent.run('analyze supplied facts')
    assert result.success and result.plan[0].result.data == report
    assert report in adapter.chat_calls[-1][-1].content
    assert len(adapter.chat_calls) == 2 and adapter.chat_with_tools_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('response', ['', '   ', None])
async def test_empty_thinking_output_does_not_become_success(response):
    adapter = FakeAdapter(chat_responses=[response, 'no result was produced'])
    agent = make_agent(adapter)
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'analyze')])
    result = await agent.run('analyze facts')
    assert not result.success and not result.plan[0].success
    assert result.plan[0].result.error
    assert all(isinstance(m.content, str) and m.content.strip() for m in agent.memory.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['test_plan', 'workflow'])
async def test_document_generation_keeps_tail_and_rejects_oversized_input(kind):
    from app.services.ai.agents.test_plan_agent import TestPlanAgent
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    adapter = FakeAdapter(chat_responses=['{"steps":[{"description":"review"}]}', 'analysis', 'summary'])
    agent = (TestPlanAgent if kind == 'test_plan' else WorkflowAgent)(adapter, tools=ToolRegistry())
    doc = 'x' * 8100 + ' GET /TAIL_ENDPOINT'
    if kind == 'test_plan':
        await agent.generate_from_doc(doc)
    else:
        await agent.run_from_doc(doc, testing_requirements='r' * 2100 + ' REQUIREMENT_TAIL')
        assert 'REQUIREMENT_TAIL' in adapter.chat_calls[0][-1].content
    assert 'GET /TAIL_ENDPOINT' in adapter.chat_calls[0][-1].content
    adapter.chat_calls.clear()
    if kind == 'test_plan':
        result = await agent.generate_from_doc('x' * 100000)
    else:
        result = await agent.run_from_doc('x' * 100000)
    assert not result['success'] and 'budget' in result['error'].lower()
    assert not adapter.chat_calls and adapter.chat_with_tools_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('execute', [False, True])
async def test_workflow_stream_uses_same_complete_task_as_regular_workflow(monkeypatch, execute):
    from app.api import routes
    from app.models.schemas import WorkflowAgentRequest
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    from app.services.ai.agent.types import AgentEvent, AgentResult
    doc = 'x' * 8100 + ' GET /TAIL_ENDPOINT'
    requirements = 'r' * 2100 + ' REQUIREMENT_TAIL'
    agent = WorkflowAgent(FakeAdapter(), tools=ToolRegistry())
    agent.run = AsyncMock(return_value=AgentResult(True, 'fixture'))
    await agent.run_from_doc(doc, requirements, ['baseline'], execute)
    regular_task = agent.run.call_args.args[0]
    seen = []
    async def stream(self, task):
        seen.append(task)
        yield AgentEvent(type='done', message='fixture', data={'success': True})
    monkeypatch.setattr(WorkflowAgent, 'run_stream', stream)
    monkeypatch.setattr(routes.AIAdapterFactory, 'create', lambda *args: FakeAdapter())
    response = await routes.agent_workflow_stream(WorkflowAgentRequest(
        api_documentation=doc, testing_requirements=requirements, test_types=['baseline'], execute=execute))
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks and seen == [regular_task]
    assert 'GET /TAIL_ENDPOINT' in seen[0] and 'REQUIREMENT_TAIL' in seen[0]


@pytest.mark.asyncio
async def test_oversized_workflow_stream_fails_before_model_or_tools(monkeypatch):
    import json
    from app.api import routes
    from app.models.schemas import WorkflowAgentRequest
    adapter = FakeAdapter()
    monkeypatch.setattr(routes.AIAdapterFactory, 'create', lambda *args: adapter)
    response = await routes.agent_workflow_stream(WorkflowAgentRequest(api_documentation='x' * 100000))
    events = [json.loads(chunk.removeprefix('data: ')) async for chunk in response.body_iterator]
    assert events[-1]['type'] == 'done' and events[-1]['data']['success'] is False
    assert 'budget' in events[-1]['data']['error'].lower()
    assert not adapter.chat_calls and adapter.chat_with_tools_calls == 0


@pytest.mark.asyncio
async def test_workflow_resource_prompts_defer_to_tool_thresholds():
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    adapter = FakeAdapter(chat_responses=['{"steps":[{"description":"review request"}]}', 'review', 'summary'])
    agent = WorkflowAgent(adapter, tools=ToolRegistry())
    await agent.run_from_doc('GET /fixture', execute=True)  # Fake reasoning plan; no execution tools.
    task = adapter.chat_calls[0][-1].content
    policy = adapter.chat_calls[1][0].content
    assert 'CPU > 80%' not in task and '内存 > 85%' not in task
    assert 'CPU>80%' not in policy and 'mem>85%' not in policy
    assert '阈值' in task and 'returned' in policy


@pytest.mark.asyncio
@pytest.mark.parametrize('summary', [RuntimeError('summary unavailable'), '', '   '])
async def test_summary_failure_preserves_completed_effects_but_fails_run(summary):
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
        ToolCallRequest('one', 'echo_tool', {'msg': 'performed once'})]), ToolCallResponse(text='done')])
    adapter.chat = AsyncMock(side_effect=summary if isinstance(summary, Exception) else None,
                             return_value=summary if isinstance(summary, str) else '')
    agent = make_agent(adapter, registry)
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'echo', 'echo_tool')])
    events = [event async for event in agent.run_stream('echo once')]
    assert calls == ['performed once']
    assert agent.state == AgentState.FAILED
    assert events[-1].type == 'done' and not events[-1].data['success']
    assert 'summary' in events[-1].data['error'].lower()
    assert 'complete' not in [event.type for event in events]
    assert agent.planner.plan.return_value[0].success  # Actual work is not rolled back/replayed.
    adapter.chat.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['plan', 'replan', 'summary', 'checkpoint'])
async def test_auxiliary_request_rejects_unfittable_input_before_model(monkeypatch, phase):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', 2300)
    adapter = FakeAdapter()
    agent = make_agent(adapter)
    step = PlanStep(1, 'large description ' * 500)
    if phase == 'checkpoint':
        assert await agent._checkpoint_eval([step, step], step, 1) is None
    else:
        with pytest.raises(ValueError, match='context budget'):
            if phase == 'plan':
                await agent.planner.plan(step.description, '')
            elif phase == 'replan':
                await agent.planner.replan([step], step, 'failed', '')
            else:
                await agent._generate_final_response(step.description, [step])
    assert not adapter.chat_calls


@pytest.mark.asyncio
async def test_planner_format_retry_keeps_original_task_and_reserves_output(monkeypatch):
    import json
    from app.core.config import settings
    adapter = FakeAdapter(chat_responses=['x' * 500, '{"steps":[{"description":"explain"}]}'])
    planner = make_agent(adapter).planner
    await planner.plan('ORIGINAL_TASK', 'tool schema')
    retry = adapter.chat_calls[1]
    cost = lambda messages: sum(adapter.estimate_tokens(json.dumps(m.to_wire(), ensure_ascii=False)) + 8
                                for m in messages)
    # Fit the required policy/task/correction, but not the malformed response.
    total_budget = cost([m for m in retry if m.role != 'assistant']) + 2000 + 256 + 5
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', total_budget)
    adapter._chat_responses = ['x' * 500, '{"steps":[{"description":"explain"}]}']
    assert await planner.plan('ORIGINAL_TASK', 'tool schema')
    sent = adapter.chat_calls[-1]
    assert any('ORIGINAL_TASK' in m.content for m in sent)
    assert cost(sent) + adapter.chat_kwargs[-1]['max_tokens'] + 256 <= total_budget


@pytest.mark.asyncio
async def test_thinking_reserves_existing_output_limit_before_selecting_history(monkeypatch):
    import json
    from app.core.config import settings
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', 10000)
    adapter = FakeAdapter()
    agent = make_agent(adapter)
    agent.memory.add_assistant('old observation ' * 1000)
    await agent._execute_thinking_step(PlanStep(1, 'explain'), 0)
    sent = adapter.chat_calls[0]
    cost = sum(adapter.estimate_tokens(json.dumps(m.to_wire(), ensure_ascii=False)) + 8 for m in sent)
    assert cost + adapter.chat_kwargs[0].get('max_tokens', 8000) + 256 <= 10000


@pytest.mark.asyncio
async def test_extraction_uses_safety_system_and_does_not_silently_slice_report(monkeypatch):
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    adapter = FakeAdapter(chat_responses=['{"overall_assessment":"extracted"}'])
    agent = AnalysisAgent(adapter, tools=ToolRegistry())
    text = 'a' * 12001 + ' END_EVIDENCE'
    await agent._extract_structured_analysis(text, FactSheet(execution_id='fixture', module='api'))
    sent = adapter.chat_calls[0]
    assert sent[0].role == 'system' and 'untrusted' in sent[0].content
    assert 'END_EVIDENCE' in sent[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['direct', 'model'])
@pytest.mark.parametrize('missing', ['all', 'hypothesis_id', 'source_execution_id', 'module'])
async def test_structured_extraction_fills_server_metadata_without_losing_predictions(path, missing):
    import json
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    hyp = dict(hypothesis_id='provided', source_execution_id='run', module='api',
               category='recommendation', statement='A controlled retest may show lower latency.',
               confidence=0.72, supporting_facts=['p99'], testable_prediction='p99 decrease',
               falsification_design='Repeat the same workload and compare the measured percentile.')
    expected = dict(hyp)
    for field in ('hypothesis_id', 'source_execution_id', 'module'):
        if missing in ('all', field):
            hyp.pop(field)
            if field == 'hypothesis_id':
                expected[field] = ''
    payload = json.dumps({'overall_assessment': 'Original assessment', 'hypotheses': [hyp]})
    adapter = FakeAdapter(chat_responses=[payload])
    agent = AnalysisAgent(adapter, tools=ToolRegistry())
    result = await agent._extract_structured_analysis(
        payload if path == 'direct' else 'Narrative analysis', FactSheet(execution_id='run', module='api'))
    assert result.overall_assessment == 'Original assessment' and len(result.hypotheses) == 1
    saved = result.hypotheses[0].model_dump(mode='json')
    assert {key: saved[key] for key in expected} == expected
    assert result.hypotheses[0].status.value == 'untested'
    assert len(adapter.chat_calls) == (0 if path == 'direct' else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('hypotheses', [None, 'invalid', [None],
    [{'category': 'invalid', 'statement': 'Invalid category'}],
    [{'category': 'risk', 'statement': 'Invalid confidence', 'confidence': 2}]])
async def test_structured_extraction_does_not_repair_invalid_hypothesis_content(hypotheses):
    import json
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    adapter = FakeAdapter(chat_responses=[json.dumps({'hypotheses': hypotheses})])
    result = await AnalysisAgent(adapter, tools=ToolRegistry())._extract_structured_analysis(
        'Narrative analysis without hypothesis markers', FactSheet(execution_id='run', module='api'))
    assert result.overall_assessment == '见原始分析文本' and result.hypotheses == []
    assert len(adapter.chat_calls) == 1


@pytest.mark.asyncio
async def test_structured_extraction_keeps_explicit_identity_conflicts_for_archival_validation():
    import json
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    adapter = FakeAdapter()
    payload = {'execution_id': 'other', 'module': 'other-api', 'version': 'other-version',
               'overall_assessment': 'Explicit conflict must remain visible'}
    result = await AnalysisAgent(adapter, tools=ToolRegistry())._extract_structured_analysis(
        json.dumps(payload), FactSheet(execution_id='run', module='api', version='v1'))
    assert all(getattr(result, key) == value for key, value in payload.items())
    assert not adapter.chat_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['direct', 'model'])
@pytest.mark.parametrize('fence', ['```json', '```', '```JSON'])
async def test_structured_extraction_accepts_complete_json_fences_without_extra_calls(path, fence):
    import json
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    data = {'overall_assessment': 'Fenced result', 'recommendations': ['Preserve literal ``` inside a string']}
    payload = fence + '\n' + json.dumps(data) + '\n```'
    adapter = FakeAdapter(chat_responses=[payload if path == 'model' else '{"overall_assessment":"unnecessary fallback"}'])
    result = await AnalysisAgent(adapter, tools=ToolRegistry())._extract_structured_analysis(
        payload if path == 'direct' else 'Narrative analysis', FactSheet(execution_id='run', module='api'))
    assert result.overall_assessment == data['overall_assessment']
    assert result.recommendations == data['recommendations']
    assert len(adapter.chat_calls) == (0 if path == 'direct' else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['Unverified prose\n```json\n{}\n```',
                                   '```json\n{}\n```\nUnverified prose', '```python\n{}\n```'])
async def test_structured_extraction_does_not_silently_discard_text_outside_json(payload):
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    adapter = FakeAdapter(chat_responses=[payload])
    result = await AnalysisAgent(adapter, tools=ToolRegistry())._extract_structured_analysis(
        'Narrative analysis', FactSheet(execution_id='run', module='api'))
    assert result.overall_assessment == '见原始分析文本'
    assert len(adapter.chat_calls) == 1


@pytest.mark.asyncio
async def test_extraction_oversized_input_uses_existing_heuristic_without_model(monkeypatch):
    from app.core.config import settings
    from app.models.schemas import FactSheet
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', 2100)
    adapter = FakeAdapter()
    result = await AnalysisAgent(adapter, tools=ToolRegistry())._extract_structured_analysis(
        'narrative text', FactSheet(execution_id='fixture', module='api'))
    assert not adapter.chat_calls
    assert result.execution_id == 'fixture' and result.overall_assessment == '见原始分析文本'


@pytest.mark.asyncio
async def test_oversized_tool_batch_is_rejected_before_any_side_effect():
    calls = []
    registry = ToolRegistry(); registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
        ToolCallRequest(id=f'call-{i}', name='echo_tool', arguments={'msg': str(i)}) for i in range(9)])])
    agent = make_agent(adapter, registry)
    result = await agent._execute_react_step(PlanStep(1, 'echo', tool_name='echo_tool'), 0)
    assert not result.success
    assert not calls
    assert 'batch' in result.error.lower()


@pytest.mark.asyncio
async def test_dangerous_tool_timeout_is_not_automatically_retried(monkeypatch):
    from app.services.ai.agent import base as base_mod
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
    result = await make_agent(adapter, registry)._execute_react_step(PlanStep(1, 'echo', tool_name='echo_tool'), 0)
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
            chat_responses=["最终总结"],
            tool_responses=[
                ToolCallResponse(tool_calls=[
                    ToolCallRequest(id="1", name="echo_tool", arguments={"msg": "hello"})
                ]),
                ToolCallResponse(text="步骤完成"),
            ],
        )
        agent = make_agent(adapter, registry)
        agent.planner.plan = AsyncMock(return_value=[
            PlanStep(step_id=1, description="回声测试", tool_name="echo_tool"),
        ])

        result = await agent.run("执行回声测试")

        assert result.success is True
        assert result.output == "最终总结"
        assert echo_calls == ["hello"]  # tool executed with LLM-chosen args
        assert result.plan[0].success is True
        types = event_types(agent)
        for expected in ("planning", "plan_ready", "step_start",
                         "tool_call", "tool_result", "react_done", "complete"):
            assert expected in types, f"missing event {expected}"
        assert types.index("step_start") < types.index("tool_call") < types.index("complete")
        assert agent.state == AgentState.COMPLETED

    @pytest.mark.asyncio
    async def test_run_thinking_step_without_tools(self):
        adapter = FakeAdapter(chat_responses=["思考结果", "最终总结"])
        agent = make_agent(adapter)
        agent.planner.plan = AsyncMock(return_value=[
            PlanStep(step_id=1, description="纯推理步骤", tool_name=None),
        ])

        result = await agent.run("思考一下")

        assert result.success is True
        assert result.plan[0].success is True
        # thinking step chat + final response chat
        assert len(adapter.chat_calls) >= 2

    @pytest.mark.asyncio
    async def test_planner_failure_returns_error(self):
        adapter = FakeAdapter()
        agent = make_agent(adapter)
        agent.planner.plan = AsyncMock(side_effect=RuntimeError("LLM down"))

        result = await agent.run("任意任务")

        assert result.success is False
        assert "Planning failed" in result.error
        assert result.error_traceback

    @pytest.mark.asyncio
    async def test_tool_error_classified_and_replan_invoked(self):
        async def failing_handler() -> dict:
            raise FileNotFoundError("plan.jmx not found")

        registry = ToolRegistry()
        registry.register(Tool(
            name="bad_tool", description="always fails",
            parameters={"type": "object", "properties": {}},
            handler=failing_handler,
        ))
        adapter = FakeAdapter(
            chat_responses=["总结"],
            tool_responses=[
                ToolCallResponse(tool_calls=[
                    ToolCallRequest(id="1", name="bad_tool", arguments={})
                ]),
                ToolCallResponse(text="放弃该步骤"),
            ],
        )
        agent = make_agent(adapter, registry)
        agent.planner.plan = AsyncMock(return_value=[
            PlanStep(step_id=1, description="会失败的步骤", tool_name="bad_tool"),
        ])
        agent.planner.replan = AsyncMock(return_value=None)  # no better plan

        result = await agent.run("执行会失败的任务")

        # A failed plan must remain failed even if a summary can be generated.
        assert result.plan[0].success is False
        assert result.plan[0].result.error_category == ErrorCategory.INPUT_ERROR
        agent.planner.replan.assert_awaited_once()
        assert result.success is False
        assert result.error

    @pytest.mark.asyncio
    async def test_max_iterations_caps_execution(self):
        adapter = FakeAdapter(chat_responses=["x"] * 20)
        agent = make_agent(adapter, max_iterations=2)
        agent.planner.plan = AsyncMock(return_value=[
            PlanStep(step_id=i, description=f"步骤{i}", tool_name=None)
            for i in range(1, 6)
        ])

        result = await agent.run("多步任务")

        assert result.iterations == 2  # capped, not all 5 steps
        executed = [s for s in result.plan if s.success]
        assert len(executed) == 2
        assert result.success is False

    @pytest.mark.asyncio
    async def test_cancel_during_run(self):
        adapter = FakeAdapter()
        agent = make_agent(adapter)

        async def plan_then_cancel(**kwargs):
            agent.cancel()
            return [PlanStep(step_id=1, description="s", tool_name=None)]

        agent.planner.plan = AsyncMock(side_effect=plan_then_cancel)

        result = await agent.run("任务")

        assert result.success is False
        assert result.error == "Cancelled by user"


class TestRunStream:
    @pytest.mark.asyncio
    async def test_events_streamed_with_done_terminal(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        adapter = FakeAdapter(
            chat_responses=["总结"],
            tool_responses=[
                ToolCallResponse(tool_calls=[
                    ToolCallRequest(id="1", name="echo_tool", arguments={"msg": "m"})
                ]),
                ToolCallResponse(text="done"),
            ],
        )
        agent = make_agent(adapter, registry)
        agent.planner.plan = AsyncMock(return_value=[
            PlanStep(step_id=1, description="回声", tool_name="echo_tool"),
        ])

        streamed = [e async for e in agent.run_stream("任务")]

        assert len(streamed) > 0
        last = streamed[-1]
        assert last.type == "done"
        assert last.data["success"] is True
        assert last.data["output"] == "总结"
        # events arrive in execution order, not all at the end
        types = [e.type for e in streamed]
        assert types.index("plan_ready") < types.index("step_start") < types.index("done")
        # stream queue is detached after the run
        assert agent._stream_queue is None


class TestReactLoop:

    @pytest.mark.asyncio
    async def test_parallel_runs_cannot_overwrite_same_session(self):
        first = make_agent(FakeAdapter(), session_id='same-session')
        second = make_agent(FakeAdapter(), session_id='same-session')
        entered = asyncio.Event()
        release = asyncio.Event()
        async def plan(**kwargs):
            entered.set()
            await release.wait()
            return [PlanStep(1, 'explain')]
        first.planner.plan = plan
        second.planner.plan = AsyncMock(return_value=[PlanStep(1, 'explain')])
        running = asyncio.create_task(first.run('first'))
        await entered.wait()
        try:
            result = await asyncio.wait_for(second.run('second'), timeout=1)
            assert not result.success and 'session' in result.error.lower()
            second.planner.plan.assert_not_awaited()
        finally:
            release.set()
            await running

    def test_subclasses_share_agent_concurrency_budget(self, monkeypatch):
        class First(BaseAgent):
            pass
        class Second(BaseAgent):
            pass
        monkeypatch.setattr(BaseAgent, '_concurrency_semaphore', None)
        assert First._get_concurrency_semaphore() is Second._get_concurrency_semaphore()

    @pytest.mark.asyncio
    async def test_react_preserves_native_call_ids_in_observation(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[ToolCallRequest('native-1', 'echo_tool', {'msg': 'hi'})]),
                                               ToolCallResponse(text='done')])
        agent = make_agent(adapter, registry)
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'echo', 'echo_tool')])
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
        agent.planner.plan = AsyncMock(return_value=[
            PlanStep(step_id=1, description="无限调用", tool_name="echo_tool"),
        ])

        result = await agent.run("任务")

        assert len(echo_calls) == 2  # capped by max_react_per_step
        assert "react_max" in event_types(agent)
        assert result.success is False


class TestAuditRegressions:
    @pytest.mark.asyncio
    async def test_empty_plan_falls_back_to_actual_tool_execution(self):
        calls = []
        registry = ToolRegistry()
        registry.register(make_echo_tool(calls))
        adapter = FakeAdapter(tool_responses=[ToolCallResponse(tool_calls=[
            ToolCallRequest('1', 'echo_tool', {'msg': 'created'})]), ToolCallResponse(text='done')])
        agent = make_agent(adapter, registry)
        agent.planner.plan = AsyncMock(return_value=[])
        result = await agent.run('Use the echo tool')
        assert calls == ['created']
        assert result.success

    @pytest.mark.asyncio
    async def test_empty_plan_without_tools_fails(self):
        agent = make_agent(FakeAdapter())
        agent.planner.plan = AsyncMock(return_value=[])
        assert not (await agent.run('task')).success

    @pytest.mark.asyncio
    async def test_replan_replaces_failed_step_without_repeating_it(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        agent = make_agent(FakeAdapter(), registry, max_iterations=2)
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'bad', 'echo_tool')])
        agent.planner.replan = AsyncMock(return_value=[PlanStep(2, 'replacement', 'echo_tool')])
        seen = []

        async def execute(step, iteration):
            seen.append(step.description)
            return (ToolResult(False, error='bad input', error_category=ErrorCategory.INPUT_ERROR)
                    if step.description == 'bad' else ToolResult(True, data={'done': True}))

        agent._execute_react_step = execute
        result = await agent.run('task')
        assert seen == ['bad', 'replacement']
        assert result.success

    @pytest.mark.asyncio
    async def test_retry_attempts_count_toward_iteration_budget(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        agent = make_agent(FakeAdapter(), registry, max_iterations=1)
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'retry', 'echo_tool')])
        execute = AsyncMock(return_value=ToolResult(False, error='timeout', error_category=ErrorCategory.RETRYABLE))
        agent._execute_react_step = execute
        result = await agent.run('task')
        assert execute.await_count == 1
        assert result.iterations == 1
        assert not result.success

    @pytest.mark.asyncio
    async def test_react_receives_task_context_history_and_prior_results(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        adapter = FakeAdapter(tool_responses=[
            ToolCallResponse(tool_calls=[ToolCallRequest('1', 'echo_tool', {'msg': 'RESOURCE_ID_17'})]),
            ToolCallResponse(text='done'),
            ToolCallResponse(tool_calls=[ToolCallRequest('2', 'echo_tool', {'msg': 'done'})]),
            ToolCallResponse(text='done')])
        agent = make_agent(adapter, registry)
        agent.memory.add_user('OLD_SESSION_SENTINEL')
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'first', 'echo_tool'), PlanStep(2, 'second', 'echo_tool')])
        result = await agent.run('ORIGINAL_TASK_SENTINEL', context='CONTEXT_SENTINEL')
        assert result.success
        text = '\n'.join(m.content for m in adapter.tool_messages[2])
        for marker in ['OLD_SESSION_SENTINEL', 'ORIGINAL_TASK_SENTINEL', 'CONTEXT_SENTINEL', 'RESOURCE_ID_17']:
            assert marker in text
        assert sum(m.role == 'system' for m in adapter.tool_messages[2]) == 1
        assert not any(m.role == 'system' for m in agent.memory.messages)

    @pytest.mark.asyncio
    async def test_current_task_survives_memory_window_eviction(self):
        from app.services.ai.agent.memory import ConversationMemory
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        adapter = FakeAdapter(tool_responses=[
            ToolCallResponse(tool_calls=[ToolCallRequest('1', 'echo_tool', {'msg': 'first'})]),
            ToolCallResponse(text='done'),
            ToolCallResponse(tool_calls=[ToolCallRequest('2', 'echo_tool', {'msg': 'second'})]),
            ToolCallResponse(text='done')])
        agent = make_agent(adapter, registry)
        agent.memory = ConversationMemory(max_messages=3)
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'first', 'echo_tool'), PlanStep(2, 'second', 'echo_tool')])
        result = await agent.run('PINNED_CURRENT_TASK', context='PINNED_CURRENT_CONSTRAINT')
        assert result.success
        second_step = '\n'.join(m.content for m in adapter.tool_messages[2])
        assert second_step.count('PINNED_CURRENT_TASK') == 1
        assert second_step.count('PINNED_CURRENT_CONSTRAINT') == 1

    @pytest.mark.asyncio
    async def test_no_tool_action_cannot_complete_a_tool_step(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        agent = make_agent(FakeAdapter(), registry, max_iterations=1)
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'create artifact', 'echo_tool')])
        agent.planner.replan = AsyncMock(return_value=None)
        result = await agent.run('task')
        assert not result.success
        assert 'No tool' in result.plan[0].result.error

    @pytest.mark.asyncio
    async def test_cancelled_tool_is_not_replanned(self):
        registry = ToolRegistry()
        registry.register(make_echo_tool([]))
        agent = make_agent(FakeAdapter(), registry)
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'cancelled', 'echo_tool')])
        agent.planner.replan = AsyncMock(return_value=None)
        agent._execute_react_step = AsyncMock(return_value=ToolResult(
            False, error='Cancelled by user', error_category=ErrorCategory.CANCELLED))
        result = await agent.run('task')
        assert not result.success
        assert result.error == 'Cancelled by user'
        assert agent.state == AgentState.CANCELLED
        agent.planner.replan.assert_not_awaited()

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
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'task', 'echo_tool')])
        agent.planner.replan = AsyncMock(return_value=None)
        result = await agent.run('task')
        assert calls == ['first']
        assert not result.success
        assert agent.state == AgentState.CANCELLED
        agent.planner.replan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_run_summary_prompt_does_not_assert_completion(self):
        adapter = FakeAdapter()
        agent = make_agent(adapter, max_iterations=0)
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'pending')])
        result = await agent.run('task')
        assert not result.success
        assert '任务已完成' not in adapter.chat_calls[-1][-1].content

    @pytest.mark.asyncio
    async def test_final_summary_retains_shared_policy(self):
        adapter = FakeAdapter()
        agent = make_agent(adapter)
        await agent._generate_final_response('summarize', [PlanStep(1, 'read', success=True)])
        assert adapter.chat_calls[-1][0].role == 'system'
        assert 'untrusted' in adapter.chat_calls[-1][0].content

    @pytest.mark.asyncio
    async def test_session_with_only_a_summary_is_loaded(self, monkeypatch):
        from app.services.ai.agent.memory import ConversationMemory
        memory = ConversationMemory(session_id='summary-session')
        memory._summary = 'SUMMARY_ONLY_CONTEXT'
        monkeypatch.setattr(ConversationMemory, 'load', AsyncMock(return_value=memory))
        monkeypatch.setattr(ConversationMemory, 'save', AsyncMock(return_value=True))
        adapter = FakeAdapter()
        agent = make_agent(adapter, session_id='summary-session')
        agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'explain')])
        await agent.run('continue')
        assert any('SUMMARY_ONLY_CONTEXT' in m.content for m in adapter.chat_calls[0])

    def test_specialized_prompt_keeps_shared_safety_rules(self):
        agent = make_agent(FakeAdapter(), system_prompt='Only analyze results.')
        assert 'Only analyze results.' in agent.system_prompt
        assert 'NEVER delete' in agent.system_prompt
        assert 'untrusted' in agent.system_prompt

    @pytest.mark.asyncio
    async def test_valid_thinking_plan_does_not_trigger_format_retry(self):
        from app.services.ai.agent.planner import TaskPlanner
        adapter = FakeAdapter(chat_responses=['{"steps":[{"step_id":1,"description":"explain","tool_name":null}]}'])
        plan = await TaskPlanner(adapter).plan('explain', '')
        assert plan[0].description == 'explain'
        assert len(adapter.chat_calls) == 1

    def test_invalid_planner_text_is_not_a_successful_thinking_step(self):
        from app.services.ai.agent.planner import TaskPlanner
        assert TaskPlanner(FakeAdapter())._parse_plan('not JSON') == []

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
        created = {'tool_statuses': [{'tool': 'create_jmx', 'success': True, 'error': None},
                                     {'tool': 'get_jmx_content', 'success': True, 'error': None}],
                   'tool_results': [{'jmx_path': 'test_plans/p/a1.jmx', 'test_plan_id': 'plan-a'},
                                    {'jmx_path': 'test_plans/p/a1.jmx', 'content': '<jmeterTestPlan/>'}]}
        read_first = {'jmx_path': 'test_plans/p/b2.jmx'}
        result = AgentResult(True, 'task', plan=[
            PlanStep(1, 'read b', success=True, result=ToolResult(True, data=read_first)),
            PlanStep(2, 'create a', success=True, result=ToolResult(True, data=created)),
            PlanStep(3, 'create b', success=True, result=ToolResult(True, data={
                'jmx_path': 'test_plans/p/b2.jmx', 'test_plan_id': 'plan-b'}))])
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
        from app.services.ai.agent.types import AgentEvent
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
async def test_later_steps_see_the_task_before_earlier_results_and_are_told_not_to_redo_them():
    # Regression: with the task after the history, each step re-ran the whole task (duplicate projects).
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[
        ToolCallResponse(tool_calls=[ToolCallRequest('c1', 'echo_tool', {'msg': 'project p1 created'})]),
        ToolCallResponse(text='step 1 done'),
        ToolCallResponse(tool_calls=[ToolCallRequest('c2', 'echo_tool', {'msg': 'plan for p1'})]),
        ToolCallResponse(text='step 2 done'),
    ])
    agent = make_agent(adapter, registry)
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'create project', 'echo_tool'),
                                                 PlanStep(2, 'create plan', 'echo_tool')])
    result = await agent.run('TASK: create project then plan')
    assert result.success and calls == ['project p1 created', 'plan for p1']
    second_step = adapter.tool_messages[2]
    contents = [m.content or '' for m in second_step]
    task_at = next(i for i, c in enumerate(contents) if c.startswith('TASK:'))
    result_at = next(i for i, m in enumerate(second_step) if m.role == 'tool' and 'project p1 created' in (m.content or ''))
    assert task_at < result_at
    assert contents.count('TASK: create project then plan') == 1
    assert contents[-1].startswith('Current step 2: create plan') and 'Do only this step' in contents[-1]


@pytest.mark.asyncio
async def test_workflow_stream_yields_step_events_then_the_result():
    from app.services.ai.agents.workflow_agent import WorkflowAgent
    calls = []
    registry = ToolRegistry()
    registry.register(make_echo_tool(calls))
    adapter = FakeAdapter(tool_responses=[
        ToolCallResponse(tool_calls=[ToolCallRequest('c1', 'echo_tool', {'msg': 'hi'})]),
        ToolCallResponse(text='done')], chat_responses=['## Summary\nAll good'])
    agent = WorkflowAgent(adapter, tools=registry)
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'echo', 'echo_tool')])
    items = [item async for item in agent.stream_from_doc('GET /x', 'r')]
    kinds = [kind for kind, _ in items]
    assert kinds[-1] == 'result' and kinds.count('result') == 1
    assert 'step_start' in [payload.type for kind, payload in items if kind == 'event']
    result = items[-1][1]
    assert result['success'] and result['diagnostic_summary'] == '## Summary\nAll good'


@pytest.mark.asyncio
async def test_run_error_names_the_failed_step_and_its_error():
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
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'create and validate the plan', 'echo_tool')])
    result = await agent.run('task')
    assert not result.success and calls == ['created']
    assert result.error.startswith('Step 1 failed (create and validate the plan):')
    assert 'wrong.jmx' in result.error


@pytest.mark.asyncio
async def test_summary_keeps_tool_receipts_but_drops_the_step_narration():
    adapter = FakeAdapter()
    agent = make_agent(adapter)
    data = {'tool_statuses': [{'tool': 'create_jmx', 'success': True, 'error': None}],
            'final_text': 'NARRATION ' * 50, 'tool_results': [{'jmx_path': 'RECEIPT_PATH'}]}
    await agent._generate_final_response('task', [PlanStep(1, 'make plan', success=True, result=ToolResult(True, data=data))])
    prompt = adapter.chat_calls[0][-1].content
    assert 'RECEIPT_PATH' in prompt and 'NARRATION' not in prompt
    # With no receipts, the narration is all there is and stays.
    only_text = {'tool_statuses': [], 'final_text': 'ONLY_TEXT', 'message': 'ONLY_TEXT'}
    await agent._generate_final_response('task', [PlanStep(1, 'x', success=False, result=ToolResult(False, data=only_text))])
    assert 'ONLY_TEXT' in adapter.chat_calls[1][-1].content


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
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'create project', 'create_project')])
    result = await agent.run('task')
    assert calls == ['bad', 'fixed'] and result.success and result.plan[0].success


@pytest.mark.asyncio
async def test_a_failure_that_is_not_an_input_error_still_fails_the_step():
    calls = []
    agent = make_agent(FakeAdapter(tool_responses=_retry_responses()),
                       _flaky_registry(calls, 'unexpected crash while writing files'))
    agent.planner.plan = AsyncMock(return_value=[PlanStep(1, 'create project', 'create_project')])
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
