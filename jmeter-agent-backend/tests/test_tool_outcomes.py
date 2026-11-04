"""How a tool call's outcome is judged: what failed, and whether the model should fix, retry or stop."""
import pytest

from app.models.schemas import TestExecutionInfo, TestExecutionStatus
from app.services.ai.agent.tool_registry import Tool, ToolRegistry
from app.services.ai.agent.types import ErrorCategory, classify_error


@pytest.fixture
def runs(monkeypatch):
    from app.services.storage import store
    records = {}
    for status in ('cancelled', 'failed', 'completed'):
        records[f'run-{status}'] = TestExecutionInfo(
            id=f'run-{status}', project_id='default', test_plan_id='plan', jmx_path='plan.jmx',
            status=TestExecutionStatus(status), error_message='JMeter exited with code 1' if status == 'failed' else None)
    monkeypatch.setattr(store, '_executions', records)
    return records


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['cancelled', 'failed', 'completed'])
async def test_reading_a_finished_run_succeeds_whatever_the_run_ended_as(runs, status):
    from app.services.ai.agent.tools.jmeter import jmeter_tools
    tools = ToolRegistry()
    tools.register_many([tool for tool in jmeter_tools if tool.name == 'get_execution_status'])
    result = await tools.execute('get_execution_status', {'execution_id': f'run-{status}'})
    assert result.success and result.error_category is None
    assert result.data['status'] == status


@pytest.mark.asyncio
async def test_a_run_the_tool_itself_ran_still_reports_its_cancellation():
    async def run():
        return {'execution_id': 'x', 'status': 'cancelled'}
    tools = ToolRegistry()
    tools.register(Tool('run', 'Runs', {'properties': {}}, run, risk='draft'))
    result = await tools.execute('run', {})
    assert not result.success and result.error_category == ErrorCategory.CANCELLED


@pytest.mark.parametrize('message, category', [
    ('Execution not found: 9b4031aa0c', ErrorCategory.INPUT_ERROR),
    ('Execution not found: 7c4290ffe1', ErrorCategory.INPUT_ERROR),
    ('Plan D:/trae/jmeter-agent/test_plans/a.jmx not found', ErrorCategory.INPUT_ERROR),
    ('Invalid timeout value: -5', ErrorCategory.INPUT_ERROR),
    ('JMeter executable not found. Checked: []', ErrorCategory.SYSTEM_ERROR),
    ('HTTP 403 from the model provider', ErrorCategory.SYSTEM_ERROR),
    ('Provider answered 429', ErrorCategory.RETRYABLE),
    ('Request timeout after 30s', ErrorCategory.RETRYABLE),
])
def test_ids_and_paths_inside_a_message_do_not_decide_its_category(message, category):
    assert classify_error(message) == category


@pytest.mark.asyncio
async def test_a_bug_inside_a_tool_is_not_reported_as_the_models_wrong_arguments():
    async def buggy(count: int):
        return None + count
    tools = ToolRegistry()
    tools.register(Tool('buggy', 'Has a bug', {'properties': {'count': {'type': 'integer'}}, 'required': ['count']}, buggy))
    result = await tools.execute('buggy', {'count': 1})
    assert not result.success
    assert 'argument error' not in result.error and result.error_category != ErrorCategory.INPUT_ERROR


@pytest.mark.asyncio
async def test_arguments_the_handler_cannot_take_are_refused_before_anyone_is_asked(monkeypatch):
    from app.services import confirmations as module
    asked = []
    async def ask(name, args):
        asked.append(name)
        return 'approved'
    monkeypatch.setattr(module.confirmations, 'ask', ask)

    async def handler():
        return {}
    tools = ToolRegistry()
    # The schema allows "extra", the function does not: a mistake in the tool definition.
    tools.register(Tool('write', 'Writes', {'properties': {'extra': {'type': 'string'}}}, handler, risk='write'))
    result = await tools.execute('write', {'extra': 'x'})
    assert result.error_category == ErrorCategory.INPUT_ERROR and 'argument error' in result.error
    assert asked == []
