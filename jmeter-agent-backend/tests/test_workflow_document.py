"""An OpenAPI document reaches the model as the endpoints the app parsed, not as raw text."""
import json

from app.services.ai import usage
from app.services.ai.agents.workflow_agent import WorkflowAgent
from app.services.openapi_importer import endpoints_from_document

SPEC = {
    'openapi': '3.0.0', 'info': {'title': 'Shop', 'version': '1'},
    'servers': [{'url': 'https://shop.example/api'}],
    'paths': {
        '/items': {'get': {'summary': 'List items', 'parameters': [
            {'name': 'page', 'in': 'query', 'schema': {'type': 'integer', 'example': 1}}],
            'responses': {'200': {'description': 'ok'}}}},
        '/items/{id}': {'delete': {'summary': 'Delete item', 'parameters': [
            {'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
            'responses': {'204': {'description': 'gone'}}}},
    },
}


def test_every_endpoint_is_listed_even_when_the_spec_lacks_an_example():
    parsed = endpoints_from_document(json.dumps(SPEC))
    assert parsed['base_url'] == 'https://shop.example/api'
    listed = {(item['method'], item['path']) for item in parsed['endpoints']}
    assert listed == {('GET', '/items'), ('DELETE', '/items/{id}')}
    delete = next(item for item in parsed['endpoints'] if item['method'] == 'DELETE')
    assert delete['parameters'] == [{'name': 'id', 'in': 'path', 'required': True}] and 'note' in delete


def test_yaml_specs_parse_and_plain_text_does_not():
    import yaml
    assert endpoints_from_document(yaml.safe_dump(SPEC))['title'] == 'Shop'
    assert endpoints_from_document('POST /login with username and password') is None
    assert endpoints_from_document('{"openapi": "3.0.0", "paths": "broken"}') is None


def test_the_task_carries_the_parsed_list_instead_of_the_document():
    task = WorkflowAgent.build_task(json.dumps(SPEC), 'load test')
    assert '<api_endpoints>' in task and '<api_documentation>' not in task
    assert '"/items/{id}"' in task and 'add no endpoint' in task
    plain = WorkflowAgent.build_task('GET /health', 'load test')
    assert '<api_documentation>\nGET /health' in plain


def test_usage_counts_only_inside_a_meter():
    usage.record(5, 5)  # outside: nowhere to count it
    with usage.metered() as used:
        usage.record(10, 2)
        usage.record(None, 3)
    usage.record(1, 1)
    assert used == {'calls': 2, 'input_tokens': 10, 'output_tokens': 5}


# ── The document workflow: code runs the steps, the model writes the plans ──

import pytest
from app.services.ai.adapters.base import ToolCallRequest, ToolCallResponse
from app.services.ai.agents import workflow_agent
from tests.test_agent_base import FakeAdapter

PLAN = {'test_name': 'Shop baseline', 'test_type': 'baseline', 'concurrent_users': 5, 'duration_seconds': 60,
        'endpoints': [{'name': 'List items', 'method': 'GET', 'path': '/items'}]}


@pytest.fixture
def saved(monkeypatch):
    from app.services import requirements as requirement_service
    from app.services.ai.agent.tools import jmx, project
    record = {'projects': [], 'plans': [], 'discarded': []}

    async def create_project(**fields):
        record['projects'].append(fields)
        return {'project_id': 'p1'}

    async def create_jmx(**plan):
        if plan['test_name'] == 'broken':
            return {'error': 'The plan was not created: concurrent_users must be at least 1'}
        record['plans'].append(plan)
        return {'test_plan_id': f"plan{len(record['plans'])}", 'test_name': plan['test_name'],
                'test_type': plan.get('test_type', 'baseline'), 'concurrent_users': plan.get('concurrent_users', 10),
                'duration_seconds': plan.get('duration_seconds', 300), 'endpoints_count': len(plan['endpoints']),
                'requests': [ep['name'] for ep in plan['endpoints']], 'warnings': []}

    async def discard(draft):
        if draft.project_id and not draft.plans:
            record['discarded'].append(draft.project_id)

    async def extract(source, adapter, name, endpoints=None):
        record['requirement_source'], record['requirement_endpoints'] = source, endpoints
        return requirement_service.PerformanceRequirements(
            load_profile=requirement_service.LoadProfile(concurrency=5, duration_seconds=60, source='5 并发 1 分钟'))

    monkeypatch.setattr(project, 'create_project', create_project)
    monkeypatch.setattr(jmx, 'create_jmx', create_jmx)
    monkeypatch.setattr(workflow_agent.WorkflowAgent, '_discard_empty', staticmethod(discard))
    monkeypatch.setattr(requirement_service, 'extract_requirements', extract)
    return record


def save(call_id, plans, project=None):
    args = {'plans': plans, **({'project': project} if project else {})}
    return ToolCallResponse(tool_calls=[ToolCallRequest(call_id, 'save_plans', args)])


async def run_workflow(adapter, document='GET /items', goal='5 并发 1 分钟'):
    items = [item async for item in workflow_agent.WorkflowAgent(adapter).stream_from_doc(document, goal)]
    assert items[-1][0] == 'result'
    return [payload for kind, payload in items if kind == 'progress'], items[-1][1]


@pytest.mark.asyncio
async def test_plans_saved_in_one_call_need_one_model_call_and_no_closing_reply(saved):
    adapter = FakeAdapter(tool_responses=[save('c1', [PLAN], {'name': 'Shop', 'base_url': 'https://shop.example'})])
    progress, result = await run_workflow(adapter)
    assert result['success'] and result['project_id'] == 'p1' and result['test_plan_ids'] == ['plan1']
    assert adapter.chat_with_tools_calls == 1
    assert saved['plans'][0]['base_url'] == 'https://shop.example' and saved['plans'][0]['project_id'] == 'p1'
    assert '已创建项目「Shop」（p1）' in result['diagnostic_summary'] and '5 并发' in result['diagnostic_summary']
    assert result['requirements_draft']['load_profile']['concurrency'] == 5
    task = adapter.tool_messages[0][1].content
    assert '"concurrency": 5' in task and '5 并发 1 分钟' in task  # The draft's load reaches the plans.
    assert [step['step'] for step in progress][:2] == ['drafting_requirements', 'writing_plans']


@pytest.mark.asyncio
async def test_a_refused_plan_is_fixed_without_creating_the_project_again(saved):
    adapter = FakeAdapter(tool_responses=[
        save('c1', [PLAN, {**PLAN, 'test_name': 'broken'}], {'name': 'Shop', 'base_url': 'https://shop.example'}),
        save('c2', [{**PLAN, 'test_name': 'fixed', 'test_type': 'stress'}])])
    progress, result = await run_workflow(adapter)
    assert result['success'] and result['test_plan_ids'] == ['plan1', 'plan2']
    assert len(saved['projects']) == 1 and adapter.chat_with_tools_calls == 2
    assert 'fixing_plans' in [step['step'] for step in progress]
    refusal = adapter.tool_messages[1][-1].content
    assert 'broken' in refusal and 'concurrent_users must be at least 1' in refusal


@pytest.mark.asyncio
async def test_a_project_left_without_plans_is_removed(saved):
    adapter = FakeAdapter(tool_responses=[
        save(f'c{i}', [{**PLAN, 'test_name': 'broken'}], {'name': 'Shop', 'base_url': 'https://shop.example'})
        for i in range(workflow_agent.PLAN_ROUNDS)])
    _, result = await run_workflow(adapter)
    assert not result['success'] and saved['discarded'] == ['p1'] and not result['project_id']


@pytest.mark.asyncio
async def test_documented_endpoints_left_out_of_every_plan_are_reported(saved):
    adapter = FakeAdapter(tool_responses=[save('c1', [PLAN], {'name': 'Shop', 'base_url': 'https://shop.example/api'})])
    _, result = await run_workflow(adapter, document=json.dumps(SPEC), goal='baseline')
    assert result['missing_endpoints'] == ['DELETE /items/{id}']
    assert 'DELETE /items/{id}' in result['diagnostic_summary']
    assert saved['requirement_source'] == 'baseline'  # A spec states endpoints, not targets.
    # ...and its endpoints are what a requirement must name, so a verdict can find the request.
    assert {item['route'] for item in saved['requirement_endpoints']} >= {'GET /items', 'DELETE /items/{id}'}


def test_coverage_matches_bound_path_variables_and_query_strings():
    parsed = endpoints_from_document(json.dumps(SPEC))
    plans = [{'endpoints': [{'method': 'GET', 'path': '/items?page=1'}, {'method': 'DELETE', 'path': '/items/{id}'}]}]
    assert workflow_agent.missing_endpoints(parsed, plans) == []
    plans = [{'endpoints': [{'method': 'GET', 'path': '/items'}, {'method': 'DELETE', 'path': '/items/42'}]}]
    assert workflow_agent.missing_endpoints(parsed, plans) == []


def test_the_model_gets_one_tool_and_no_rules_for_tools_it_lacks():
    tool = workflow_agent.save_plans_tool(workflow_agent._Draft())
    plan_fields = tool.parameters['properties']['plans']['items']['properties']
    assert 'base_url' not in plan_fields and 'project_id' not in plan_fields and 'endpoints' in plan_fields
    prompt = workflow_agent.WORKFLOW_SYSTEM_PROMPT
    for absent in ('execute_test', 'check_system_resources', 'create_jmx', 'delete'):
        assert absent not in prompt
