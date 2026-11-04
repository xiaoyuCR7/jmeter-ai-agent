"""Agent and MCP tools that change or list plans, and the document workflow's plan saving."""
import asyncio
import time

import pytest
import pytest_asyncio

from app.services.ai.agent.tools import jmx as jmx_tools


@pytest_asyncio.fixture(autouse=True)
async def app_data(tmp_path, monkeypatch):
    """A database and folders of the test's own."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.core.config import settings
    from app.models import db
    from app.services.storage import store
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'state.db'))
    monkeypatch.setattr(db, '_engine', engine)
    monkeypatch.setattr(db, '_async_session_factory', async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, 'TEST_PLANS_DIR_DEFAULT', str(tmp_path / 'plans'))
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(tmp_path / 'reports'))
    monkeypatch.setattr('app.core.config.load_paths_config', lambda: {})
    for key in ('_projects', '_test_plans', '_executions'):
        monkeypatch.setattr(store, key, {})
    await db.init_db()
    yield
    await engine.dispose()


ENDPOINT = {'name': 'List users', 'method': 'GET', 'path': '/users'}


async def _plan(name='Users'):
    created = await jmx_tools.create_jmx(name, [ENDPOINT], 'http://127.0.0.1:9', concurrent_users=5,
                                         duration_seconds=60)
    assert 'error' not in created, created
    return created


@pytest.mark.asyncio
async def test_a_changed_plan_keeps_its_saved_settings_in_step():
    from app.services.storage import store
    created = await _plan()
    result = await jmx_tools.update_jmx(created['jmx_path'], concurrent_users=40, duration_seconds=120)
    assert result['updated'] and result['applied'] == {'concurrent_users': 40, 'duration_seconds': 120}
    config = store.get_test_plan(created['test_plan_id']).test_config
    assert (config.concurrent_users, config.duration_seconds) == (40, 120)


@pytest.mark.asyncio
async def test_a_change_that_breaks_the_plan_leaves_the_file_and_record_as_they_were(monkeypatch):
    from app.services.jmeter import jmx_lint
    from app.services.storage import store
    created = await _plan()
    before = open(created['jmx_path'], encoding='utf-8').read()
    refusal = [{'severity': 'error', 'code': 'jmeter_load', 'message': 'JMeter 无法加载：fixture'}]
    monkeypatch.setattr(jmx_lint, 'check', lambda content, expected=(): (refusal, refusal))
    result = await jmx_tools.update_jmx(created['jmx_path'], concurrent_users=40)
    assert 'fixture' in result['error']
    assert open(created['jmx_path'], encoding='utf-8').read() == before
    assert store.get_test_plan(created['test_plan_id']).test_config.concurrent_users == 5


@pytest.mark.asyncio
async def test_lists_show_only_what_the_app_has_with_ids_other_tools_take(tmp_path):
    import os
    from app.core.config import settings
    from app.services.ai.agent.tools import project as project_tools
    created = await _plan()
    # A folder left on disk without a project behind it is not a project.
    os.makedirs(os.path.join(settings.TEST_PLANS_DIR, 'leftover'), exist_ok=True)
    projects = (await project_tools.list_projects())['projects']
    assert 'leftover' not in {project['id'] for project in projects}
    assert (await project_tools.get_project_info('leftover'))['error']
    plans = (await jmx_tools.list_test_plans(project_id='default'))['test_plans']
    assert {'test_plan_id': created['test_plan_id'], 'name': 'Users', 'project_id': 'default',
            'jmx_path': created['jmx_path'], 'test_type': 'baseline'} in plans
    info = await project_tools.get_project_info('default')
    assert created['test_plan_id'] in {plan['test_plan_id'] for plan in info['test_plans']}


@pytest.mark.asyncio
async def test_save_plans_checks_plans_side_by_side_and_answers_in_order(monkeypatch):
    from app.services.ai.agents import workflow_agent
    from app.services.ai.agent.tools import project as project_tools
    running, peak = 0, 0

    async def create_jmx(test_name, **_):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.2)  # Stands in for JMeter loading the plan.
        running -= 1
        if test_name == 'bad':
            return {'error': 'refused'}
        return {'test_plan_id': f'id-{test_name}', 'test_name': test_name}

    async def create_project(**fields):
        return {'project_id': 'p1'}

    monkeypatch.setattr(jmx_tools, 'create_jmx', create_jmx)
    monkeypatch.setattr(project_tools, 'create_project', create_project)
    draft = workflow_agent._Draft()
    tool = workflow_agent.save_plans_tool(draft)
    plans = [{'test_name': name, 'endpoints': [ENDPOINT]} for name in ('a', 'bad', 'c', 'd', 'e', 'f')]
    started = time.monotonic()
    reply = await tool.handler(plans=plans, project={'name': 'Shop', 'base_url': 'http://127.0.0.1:9'})
    elapsed = time.monotonic() - started
    assert peak == workflow_agent.PARALLEL_PLAN_CHECKS
    assert elapsed < 6 * 0.2
    assert [result['test_name'] for result in reply['results']] == ['a', 'bad', 'c', 'd', 'e', 'f']
    assert [plan['test_plan_id'] for plan in draft.plans] == ['id-a', 'id-c', 'id-d', 'id-e', 'id-f']
    assert '1 of 6' in reply['error']


@pytest.mark.asyncio
async def test_one_plan_crashing_does_not_lose_the_plans_saved_beside_it(monkeypatch):
    from app.services.ai.agents import workflow_agent
    from app.services.ai.agent.tools import project as project_tools

    async def create_jmx(test_name, **_):
        if test_name == 'crash':
            raise OSError('disk went away')
        await asyncio.sleep(0.05)
        return {'test_plan_id': f'id-{test_name}', 'test_name': test_name}

    async def create_project(**fields):
        return {'project_id': 'p1'}

    monkeypatch.setattr(jmx_tools, 'create_jmx', create_jmx)
    monkeypatch.setattr(project_tools, 'create_project', create_project)
    draft = workflow_agent._Draft()
    reply = await workflow_agent.save_plans_tool(draft).handler(
        plans=[{'test_name': 'crash', 'endpoints': []}, {'test_name': 'ok', 'endpoints': []}],
        project={'name': 'Shop', 'base_url': 'http://127.0.0.1:9'})
    assert [plan['test_plan_id'] for plan in draft.plans] == ['id-ok']
    assert 'disk went away' in reply['results'][0]['error']
