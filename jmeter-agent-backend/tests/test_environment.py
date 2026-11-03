"""A project's active environment decides where a run's requests go, with what headers and variables."""
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    CreateEnvironmentRequest, EnvironmentConfig, ProjectEnvironments, ProjectInfo, TestConfig, TestPlanInfo,
)
from app.services.jmeter import environment
from tests.test_execution_lifecycle import env, service  # noqa: F401  (fixture)

PLAN = b'''<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2"><hashTree>
  <TestPlan testclass="TestPlan" testname="shop" enabled="true"/>
  <hashTree>
    <ConfigTestElement testclass="ConfigTestElement" testname="HTTP Request Defaults" enabled="true">
      <stringProp name="HTTPSampler.protocol">http</stringProp>
      <stringProp name="HTTPSampler.domain">dev.shop.local</stringProp>
      <stringProp name="HTTPSampler.port">8080</stringProp>
      <stringProp name="HTTPSampler.path"/>
    </ConfigTestElement>
    <hashTree/>
    <ThreadGroup testclass="ThreadGroup" testname="users" enabled="true">
      <stringProp name="ThreadGroup.num_threads">20</stringProp>
    </ThreadGroup>
    <hashTree>
      <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="orders" enabled="true">
        <stringProp name="HTTPSampler.domain"/>
        <stringProp name="HTTPSampler.path">/v1/orders</stringProp>
      </HTTPSamplerProxy>
      <hashTree>
        <HeaderManager testclass="HeaderManager" testname="plan headers" enabled="true">
          <collectionProp name="HeaderManager.headers">
            <elementProp name="" elementType="Header"><stringProp name="Header.name">Authorization</stringProp><stringProp name="Header.value">Bearer dev</stringProp></elementProp>
            <elementProp name="" elementType="Header"><stringProp name="Header.name">Accept</stringProp><stringProp name="Header.value">application/json</stringProp></elementProp>
          </collectionProp>
        </HeaderManager>
        <hashTree/>
      </hashTree>
      <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="payments" enabled="true">
        <stringProp name="HTTPSampler.domain">pay.partner.com</stringProp>
        <stringProp name="HTTPSampler.path">/charge</stringProp>
      </HTTPSamplerProxy>
      <hashTree/>
    </hashTree>
  </hashTree>
</hashTree></jmeterTestPlan>'''

STAGING = {'base_url': 'https://staging.shop.com/v2', 'secret_keys': ['Authorization'],
           'headers': {'Authorization': 'Bearer real-token', 'X-Env': 'staging'}, 'variables': {'tenant': 'acme'}}


def props(root, name):
    return [prop.text or '' for prop in root.iter('stringProp') if prop.get('name') == name]


def test_a_run_goes_to_the_active_environment_with_its_headers_and_variables():
    content, applied = environment.apply(PLAN, STAGING, ['http://dev.shop.local:8080/v1'])
    root = ET.fromstring(content)
    assert props(root, 'HTTPSampler.domain') == ['staging.shop.com', '', 'pay.partner.com']  # The partner keeps its host.
    assert props(root, 'HTTPSampler.protocol')[0] == 'https' and props(root, 'HTTPSampler.port')[0] == ''
    assert '/v2/orders' in props(root, 'HTTPSampler.path')
    names = props(root, 'Header.name')
    assert names.count('Authorization') == 1 and 'X-Env' in names and 'Accept' in names  # The plan's own dev token is gone.
    assert props(root, 'Argument.value') == ['acme']
    top = [child.get('testname') for child in root.find('hashTree')[1] if child.tag != 'hashTree']
    assert top[0] == '[环境·敏感] 请求头' and top[-1] == '[环境] 变量'  # Variables defined last, so they win.
    assert applied['hosts_replaced'] == ['dev.shop.local'] and applied['plan_headers_overridden'] == 1

    again, _ = environment.apply(content, STAGING, ['http://dev.shop.local:8080/v1'])
    assert again.count(b'X-Env') == 1 and again.count('[环境] 变量'.encode()) == 1  # A rerun replaces, never stacks.


def test_a_plan_with_one_unknown_host_is_still_pointed_at_the_environment():
    content, applied = environment.apply(PLAN.replace(b'pay.partner.com', b''), {'base_url': 'https://qa.shop.com'}, [])
    assert props(ET.fromstring(content), 'HTTPSampler.domain')[0] == 'qa.shop.com'
    assert applied['note'] is None
    _, many = environment.apply(PLAN, {'base_url': 'https://qa.shop.com'}, [])
    assert many['hosts_replaced'] == [] and '多个主机' in many['note']


def test_nothing_to_apply_runs_the_plan_byte_for_byte():
    assert environment.apply(PLAN, {'base_url': ''}, [])[0] is PLAN


def test_secrets_are_masked_once_the_run_is_over(tmp_path):
    path = tmp_path / 'snapshot.jmx'
    path.write_bytes(environment.apply(PLAN, STAGING, [])[0])
    assert b'real-token' in path.read_bytes()
    assert environment.scrub(path)
    assert b'real-token' not in path.read_bytes() and b'X-Env' in path.read_bytes()


def test_responses_mask_secrets_but_storage_and_the_fingerprint_keep_them():
    config = EnvironmentConfig(id='s', name='Staging', **STAGING, kind='staging')
    assert config.model_dump()['headers'] == {'Authorization': '******', 'X-Env': 'staging'}
    assert config.revealed()['headers']['Authorization'] == 'Bearer real-token'
    legacy = {key: config.revealed()[key] for key in ('id', 'name', 'base_url', 'variables', 'headers')}
    # Earlier runs hashed exactly these fields, so they stay comparable after the new ones arrived.
    assert config.fingerprint() == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()


def test_environment_values_cannot_smuggle_in_code():
    with pytest.raises(ValidationError):
        CreateEnvironmentRequest(name='x', variables={'token': '${__groovy(System.exit(0))}'})
    with pytest.raises(ValidationError):
        CreateEnvironmentRequest(name='x', headers={'Bad Header': 'v'})
    with pytest.raises(ValidationError):
        CreateEnvironmentRequest(name='x', base_url='https://user:pw@shop.com')
    assert CreateEnvironmentRequest(name='x', variables={'host': '${base}'}).variables == {'host': '${base}'}


async def prepared_with(env, config: EnvironmentConfig, execution_id='run', **kwargs):  # noqa: F811
    from app.services.execution_lifecycle import persist_project
    env.path.write_bytes(PLAN)
    project = ProjectInfo(id=f'shop-{execution_id}', name='Shop', environments=ProjectEnvironments(
        environments=[config, EnvironmentConfig(id='dev', name='Dev', base_url='http://dev.shop.local:8080/v1')],
        active_id=config.id))
    await persist_project(project)
    lifecycle, _ = service()
    plan = await lifecycle.register_plan(TestPlanInfo(
        id=f'plan-{execution_id}', project_id=project.id, name='Shop', jmx_path=str(env.path),
        created_at=datetime.now(), endpoints_count=2, test_config=TestConfig(test_type='baseline')))
    try:
        return await lifecycle.prepare(str(env.path), execution_id, test_plan_id=plan.id, **kwargs)
    finally:
        await lifecycle.shutdown()


@pytest.mark.asyncio
async def test_a_run_snapshots_the_environment_and_records_which_one_it_used(env):  # noqa: F811
    config = EnvironmentConfig(id='staging', name='Staging', kind='staging', **STAGING,
                               prometheus_url='http://prom.staging:9090')
    execution = await prepared_with(env, config)
    snapshot = open(execution.jmx_path, 'rb').read()
    assert b'staging.shop.com' in snapshot and env.path.read_bytes() == PLAN  # The plan file is untouched.
    assert execution.source_sha256 == hashlib.sha256(snapshot).hexdigest()
    assert execution.context_snapshot['environment_fingerprint'] == config.fingerprint()
    from app.services import server_metrics
    recorded = server_metrics.run_environment(execution.report_dir)
    assert (recorded['name'], recorded['kind'], recorded['prometheus_url']) == ('Staging', 'staging', 'http://prom.staging:9090')
    assert 'real-token' not in json.dumps(recorded)


@pytest.mark.asyncio
async def test_production_waits_for_a_yes_and_the_thread_cap_always_holds(env):  # noqa: F811
    production = EnvironmentConfig(id='prod', name='生产', kind='production', base_url='https://shop.com')
    with pytest.raises(environment.ProductionRunNeedsConfirmation) as refused:
        await prepared_with(env, production)
    assert refused.value.threads == 20
    assert not (env.root / 'reports' / 'run').exists()  # Nothing was set up for a run that did not start.
    assert (await prepared_with(env, production, 'yes', confirm_production=True)).id == 'yes'
    capped = EnvironmentConfig(id='qa', name='QA', base_url='https://qa.shop.com', max_threads=10)
    with pytest.raises(ValueError, match='超过环境「QA」允许的上限 10'):
        await prepared_with(env, capped, 'too-many')


@pytest.mark.asyncio
async def test_saving_a_page_that_only_saw_the_mask_keeps_the_secret(env):  # noqa: F811
    from app.api import routes
    from app.models.schemas import UpdateEnvironmentRequest
    from app.services.storage import store
    project = ProjectInfo(id='masked', name='Shop', environments=ProjectEnvironments(environments=[
        EnvironmentConfig(id='s', name='Staging', **STAGING)], active_id='s'))
    from app.services.execution_lifecycle import persist_project
    await persist_project(project)
    shown = (await routes.list_environments('masked')).model_dump()['environments'][0]
    assert shown['headers']['Authorization'] == '******'
    saved = await routes.update_environment('masked', 's', UpdateEnvironmentRequest(
        headers={**shown['headers'], 'X-Env': 'staging-2'}, kind='production', max_threads=50,
        prometheus_url='http://prom:9090'))
    stored = store.get_project('masked').environments.environments[0]
    assert stored.revealed()['headers'] == {'Authorization': 'Bearer real-token', 'X-Env': 'staging-2'}
    assert (saved.kind, saved.max_threads, saved.prometheus_url) == ('production', 50, 'http://prom:9090')
    await routes.update_environment('masked', 's', UpdateEnvironmentRequest(
        headers={'X-Env': 'staging-2'}, clear_max_threads=True))
    stored = store.get_project('masked').environments.environments[0]
    assert stored.secret_keys == [] and stored.max_threads is None  # A secret whose name is gone is forgotten.


@pytest.mark.asyncio
async def test_a_copied_environment_keeps_the_secrets_the_page_never_saw(env):  # noqa: F811
    from app.api import routes
    from app.services.execution_lifecycle import persist_project
    await persist_project(ProjectInfo(id='copy', name='Shop', environments=ProjectEnvironments(environments=[
        EnvironmentConfig(id='s', name='Staging', **STAGING)], active_id='s')))
    shown = (await routes.list_environments('copy')).model_dump()['environments'][0]
    copy = await routes.create_environment('copy', CreateEnvironmentRequest(
        name='Staging copy', headers=shown['headers'], secret_keys=shown['secret_keys'], copy_from='s'))
    assert copy.revealed()['headers']['Authorization'] == 'Bearer real-token'


@pytest.mark.asyncio
async def test_jmeter_reads_the_secret_and_the_copy_loses_it_when_jmeter_is_done(env):  # noqa: F811
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.models.schemas import TestExecutionInfo, TestExecutionStatus
    from app.services.execution_lifecycle import persist_project
    env.path.write_bytes(PLAN)
    await persist_project(ProjectInfo(id='secret-run', name='Shop', environments=ProjectEnvironments(environments=[
        EnvironmentConfig(id='s', name='Staging', **STAGING)], active_id='s')))
    lifecycle, _ = service()
    plan = await lifecycle.register_plan(TestPlanInfo(id='secret-plan', project_id='secret-run', name='Shop',
        jmx_path=str(env.path), created_at=datetime.now(), endpoints_count=2, test_config=TestConfig(test_type='baseline')))
    prepared = await lifecycle.prepare(str(env.path), 'secret', test_plan_id=plan.id)
    seen = []

    async def execute(jmx_path, execution_id, **kwargs):
        seen.append(b'real-token' in open(jmx_path, 'rb').read())
        return TestExecutionInfo(id=execution_id, test_plan_id=plan.id, status=TestExecutionStatus.FAILED,
                                 jmx_path=jmx_path, end_time=datetime.now(), error_message='stop here')
    await lifecycle.run(prepared.id, executor=SimpleNamespace(execute=AsyncMock(side_effect=execute)))
    await lifecycle.shutdown()
    assert seen == [True] and b'real-token' not in open(prepared.jmx_path, 'rb').read()
