"""Questions about one run: its bound tools, the streamed answer, and JSON replies from the model."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from app.services.ai.agent.tools.run_scope import run_tools

HEADER = 'timeStamp,elapsed,label,responseCode,responseMessage,success,failureMessage,Connect,Latency,allThreads\n'


@pytest.fixture
def run(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(tmp_path))
    report_dir = tmp_path / 'run-1'
    report_dir.mkdir()
    rows = ['1000,120,GET /a,200,OK,true,,5,100,2', '1500,900,POST /b,500,Server Error,false,boom,0,880,2',
            '4000,130,GET /a,200,OK,true,,0,110,4', '9000,950,POST /b,200,OK,true,,300,900,4']
    (report_dir / 'result.jtl').write_text(HEADER + '\n'.join(rows) + '\n', encoding='utf-8')
    execution = SimpleNamespace(id='run-1', project_id='p', test_plan_id='plan', report_dir=str(report_dir),
                                log_path=None, jmx_path='plan.jmx')
    return {tool.name: tool.handler for tool in run_tools(execution)}


@pytest.mark.asyncio
async def test_samples_can_be_filtered_by_request_failure_speed_and_time(run):
    failed = await run['find_samples'](failed_only=True)
    assert failed['matched'] == 1 and failed['samples'][0]['message'] == 'boom' and failed['samples'][0]['code'] == '500'
    slow = await run['find_samples'](min_elapsed_ms=800)
    assert [s['second'] for s in slow['samples']] == [0.5, 8.0]
    window = await run['find_samples'](label='GET /a', from_second=1, to_second=5)
    assert window['matched'] == 1 and window['samples'][0]['elapsed_ms'] == 130
    assert (await run['find_samples'](limit=1000))['shown'] == 4  # capped, never the whole file


@pytest.mark.asyncio
async def test_a_run_without_a_log_says_so(run):
    assert 'no JMeter log' in (await run['read_run_log']())['error']


@pytest.mark.asyncio
async def test_runs_of_other_projects_cannot_be_compared(run, monkeypatch):
    from app.services.storage import store
    other = SimpleNamespace(id='other', project_id='elsewhere', status=SimpleNamespace(value='completed'))
    monkeypatch.setattr(store, 'get_execution', lambda execution_id: other)
    assert 'same project' in (await run['compare_with_run']('other'))['error']


@pytest.mark.asyncio
async def test_a_question_streams_its_progress_then_the_saved_answer(monkeypatch):
    from app.api import execution_routes
    from app.models.schemas import RunQuestion
    from app.services.ai.agent.base import BaseAgent
    from app.services.ai.agent.types import AgentResult

    class Agent(BaseAgent):
        async def run_analysis(self, question, evidence, context=None):
            self.emit('analysis_phase', 'x', {'phase': 'preparation'})
            self.emit('tool_call', 'x', {'tool': 'find_samples'})
            self.emit('analysis_phase', 'x', {'phase': 'postprocessing'})
            return AgentResult(True, question, output='Slowest is POST /b.', analysis={'validation': {'mismatches': []}})

    agent = Agent('questions', adapter=None)
    monkeypatch.setattr(execution_routes, '_question_setup', AsyncMock(return_value=({}, 'REPORT', agent)))
    saved = []

    async def save(execution_id, question, result, used):
        saved.append(result.output)
        return {'question': question, 'answer': result.output}
    monkeypatch.setattr(execution_routes, '_save_answer', save)
    response = await execution_routes.ask_about_execution_stream('run-1', RunQuestion(question='Which is slowest?'), lang='en')
    body = ''.join([chunk async for chunk in response.body_iterator])
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]
    assert [e['message'] for e in events[:-1]] == [
        "Gathering this run's evidence", 'Looking at individual samples', 'Checking the numbers in the answer']
    assert events[-1] == {'step': 'answer', 'answer': {'question': 'Which is slowest?', 'answer': 'Slowest is POST /b.'}}
    assert saved == ['Slowest is POST /b.']


@pytest.mark.asyncio
async def test_json_mode_is_dropped_once_for_a_provider_that_refuses_it():
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    from app.models.schemas import AIChatMessage
    refusal = openai.BadRequestError(
        'response_format is not supported', body=None,
        response=httpx.Response(400, request=httpx.Request('POST', 'https://model.example/v1/chat/completions')))
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"a": 1}'))], usage=None)
    create = AsyncMock(side_effect=[refusal, reply, reply])
    adapter = object.__new__(OpenAIAdapter)
    adapter.model = 'fixture'
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    message = [AIChatMessage(role='user', content='Answer in JSON')]
    assert await adapter.chat(message, json_mode=True) == '{"a": 1}'
    assert await adapter.chat(message, json_mode=True) == '{"a": 1}'
    formats = [call.kwargs.get('response_format') for call in create.await_args_list]
    assert formats == [{'type': 'json_object'}, None, None]  # Asked once; not asked again.
