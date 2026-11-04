"""Dangerous calls must wait for the user's server-side decision."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from app.services.ai.agent.tool_registry import Tool, ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize('args', [{'path': 'x', 'confirmed': True}, {'path': 3}, {}, {'path': 'x', 'skip_resource_check': True}])
async def test_invalid_or_undeclared_arguments_never_reach_handler(args):
    handler = AsyncMock(return_value={'ok': True})
    registry = ToolRegistry()
    registry.register(Tool('test', 'test', {'type': 'object', 'properties': {'path': {'type': 'string'}},
                                           'required': ['path']}, handler))
    result = await registry.execute('test', args)
    assert not result.success
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_dangerous_tool_without_a_decision_is_denied(monkeypatch):
    from app.services.confirmations import confirmations
    monkeypatch.setattr(confirmations, 'timeout_seconds', 0.01)
    handler = AsyncMock(return_value={})
    registry = ToolRegistry()
    registry.register(Tool('danger', 'danger', {}, handler, risk='write'))
    result = await registry.execute('danger', {})
    assert not result.success and 'Nobody answered' in result.error
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_uses_same_parameter_and_business_failure_contract():
    import json
    from app.services.mcp_server import MCPServer
    handler = AsyncMock(return_value={'success': False, 'error': None})
    server = MCPServer()
    server.register_tool('test', 'test', {'type': 'object', 'properties': {}}, handler)
    invalid = await server.handle_request(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                                     'params': {'name': 'test', 'arguments': {'bypass': True}}}))
    assert invalid['result']['isError'] and 'bypass' in invalid['result']['content'][0]['text']
    handler.assert_not_awaited()
    failed = await server.handle_request(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                                    'params': {'name': 'test', 'arguments': {}}}))
    assert failed['result']['isError']


def test_general_tool_paths_cannot_read_backend_configuration(tmp_path, monkeypatch):
    from app.core.config import safe_path, settings
    monkeypatch.setattr(settings, 'BASE_DIR', str(tmp_path))
    with pytest.raises(ValueError, match='Access denied'):
        safe_path(str(tmp_path / 'ai_models_config.json'))


def test_composite_execution_tools_require_confirmation():
    from app.services.ai.agent.tools.resource import resource_tools
    tools = {tool.name: tool for tool in resource_tools}
    assert tools['import_openapi_spec'].require_confirmation


@pytest.mark.asyncio
async def test_analysis_paths_are_checked_before_parser_access(tmp_path, monkeypatch):
    from app.services.ai.agent.tools import resource
    from app.services.jmeter.report_parser import ReportParser
    from unittest.mock import Mock
    parser = Mock(side_effect=AssertionError('must not open untrusted path'))
    monkeypatch.setattr(ReportParser, 'parse_jtl', parser)
    result = await resource.compare_runs(str(tmp_path / 'outside.jtl'), str(tmp_path / 'other.jtl'))
    assert 'Access denied' in result['error']
    parser.assert_not_called()
    with pytest.raises(ValueError):
        await resource.detect_anomalies('../outside')


@pytest.mark.asyncio
async def test_fact_analysis_rejects_outside_jtl_before_parsing(tmp_path, monkeypatch):
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    from app.services.ai.agents import analysis_agent
    outside = tmp_path / 'outside.jtl'; outside.write_text('untrusted')
    compute = AsyncMock()
    monkeypatch.setattr(analysis_agent.fact_ledger, 'compute_from_jtl', compute)
    with pytest.raises(ValueError, match='Access denied'):
        await object.__new__(AnalysisAgent)._build_fact_sheet(str(outside), 'safe-id', None, 'api', 'v1', 'load')
    compute.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_execution_cannot_claim_a_different_jtl_as_its_evidence(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.db_store import db_store
    from app.services.ai.agents.analysis_agent import AnalysisAgent
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(tmp_path))
    monkeypatch.setattr(db_store, 'get_execution', AsyncMock(return_value={'report_dir': str(tmp_path / 'registered')}))
    with pytest.raises(ValueError, match='registered execution'):
        await object.__new__(AnalysisAgent)._build_fact_sheet(
            str(tmp_path / 'different.jtl'), 'registered', None, 'api', 'v1', 'load')


def test_every_built_in_tool_has_a_deliberate_risk():
    """Pinned so a new tool that changes something cannot arrive as a default 'read'."""
    from app.services.ai.agent.tools import get_all_builtin_tools
    levels = {}
    for tool in get_all_builtin_tools():
        levels.setdefault(tool.risk, set()).add(tool.name)
    assert 'get_execution_status' in levels['read']
    assert levels['execute'] == {'execute_test', 'execute_distributed', 'import_openapi_spec', 'start_capacity_search',
                                 'start_verification_run'}
    assert levels['write'] == {'update_jmx', 'update_project_env', 'cancel_execution',
                               'save_performance_requirements', 'promote_execution_to_baseline'}
    assert levels['draft'] == {'create_jmx', 'import_jmx', 'create_project'}
    assert all(tool.require_confirmation == (tool.risk in ('write', 'execute')) for tool in get_all_builtin_tools())


def test_questions_about_a_run_get_only_read_tools_bound_to_that_run():
    from types import SimpleNamespace
    from app.services.ai.agent.tools.run_scope import run_tools
    tools = run_tools(SimpleNamespace(id='run-1', project_id='p', test_plan_id='plan', report_dir='', log_path=None,
                                      jmx_path='plan.jmx'))
    assert {tool.risk for tool in tools} == {'read'}
    names = {tool.name for tool in tools}
    assert names == {'find_samples', 'read_run_log', 'describe_plan', 'detect_anomalies', 'list_runs', 'compare_with_run'}
    # Nothing that reaches other projects or files, waits for minutes, or calls another model.
    for schema in (tool.parameters for tool in tools):
        assert not {'path', 'jmx_path', 'jtl_path', 'execution_id', 'project_id'} & set(schema['properties'])


@pytest.mark.asyncio
async def test_mcp_clients_are_told_each_tools_risk():
    import json
    from app.services.mcp_server import MCPServer
    server = MCPServer()
    server.register_all_agent_tools()
    response = await server.handle_request(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}))
    hints = {tool['name']: tool['annotations'] for tool in response['result']['tools']}
    assert hints['get_execution_evidence'] == {'readOnlyHint': True, 'destructiveHint': False}
    assert hints['create_jmx'] == {'readOnlyHint': False, 'destructiveHint': False}
    assert hints['update_jmx'] == {'readOnlyHint': False, 'destructiveHint': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['agent', 'mcp'])
async def test_confirmations_show_who_is_asking_and_for_which_project(source, monkeypatch):
    from app.services.confirmations import confirmations
    from app.services.storage import store
    from types import SimpleNamespace
    monkeypatch.setattr(store, 'get_project', lambda project_id: SimpleNamespace(name='Checkout'))
    registry = ToolRegistry(source=source)
    registry.register(Tool('danger', 'danger', {'properties': {'project_id': {'type': 'string'}}},
                           AsyncMock(return_value={}), risk='write'))
    call = asyncio.create_task(registry.execute('danger', {'project_id': 'perm-project'}))
    while not confirmations.list_pending():
        await asyncio.sleep(0)
    [request] = confirmations.list_pending()
    assert request['source'] == source and request['project'] == 'Checkout'
    confirmations.decide(request['id'], False)
    result = await call
    assert not result.success and 'denied' in result.error


@pytest.mark.asyncio
async def test_a_plan_with_scripts_asks_for_approval_instead_of_failing(monkeypatch):
    from app.services.ai.agent.tools import jmeter
    from app.services.jmeter.script_risk import ScriptApprovalRequired
    finding = [{'name': 'Shell', 'kind': 'SystemSampler'}]
    execute = AsyncMock(side_effect=[ScriptApprovalRequired(finding, 'digest'), 'finished'])
    monkeypatch.setattr(jmeter.execution_lifecycle, 'execute', execute)
    from app.services.confirmations import confirmations
    monkeypatch.setattr(confirmations, 'request', AsyncMock(return_value=True))
    assert await jmeter.execute_with_script_approval('execute_test', 'a.jmx', 'run') == 'finished'
    assert execute.await_args.kwargs['approved_sha256'] == 'digest'
