"""Trial runs: the copy that runs, reading its responses, and fixes proven by those responses."""
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import trial_run

PLAN = b'''<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan><hashTree>
  <TestPlan testclass="TestPlan" testname="P" enabled="true"/>
  <hashTree>
    <SetupThreadGroup testclass="SetupThreadGroup" testname="Setup" enabled="true">
      <stringProp name="ThreadGroup.num_threads">1</stringProp>
    </SetupThreadGroup>
    <hashTree/>
    <ThreadGroup testclass="ThreadGroup" testname="Step 1" enabled="true">
      <elementProp name="ThreadGroup.main_controller" elementType="LoopController">
        <stringProp name="LoopController.loops">-1</stringProp>
      </elementProp>
      <stringProp name="ThreadGroup.num_threads">50</stringProp>
      <stringProp name="ThreadGroup.ramp_time">30</stringProp>
      <boolProp name="ThreadGroup.scheduler">true</boolProp>
      <stringProp name="ThreadGroup.duration">600</stringProp>
    </ThreadGroup>
    <hashTree>
      <HeaderManager testclass="HeaderManager" testname="Headers" enabled="true">
        <collectionProp name="HeaderManager.headers">
          <elementProp name="" elementType="Header">
            <stringProp name="Header.name">Authorization</stringProp>
            <stringProp name="Header.value">Bearer real-secret</stringProp>
          </elementProp>
        </collectionProp>
      </HeaderManager>
      <hashTree/>
      <ConstantTimer testclass="ConstantTimer" testname="Think" enabled="true"/>
      <hashTree/>
      <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="POST /login" enabled="true">
        <stringProp name="HTTPSampler.method">POST</stringProp>
        <stringProp name="HTTPSampler.path">/login</stringProp>
      </HTTPSamplerProxy>
      <hashTree>
        <JSONPostProcessor testclass="JSONPostProcessor" testname="token" enabled="true">
          <stringProp name="JSONPostProcessor.referenceNames">token</stringProp>
          <stringProp name="JSONPostProcessor.jsonPathExprs">$.token</stringProp>
        </JSONPostProcessor>
        <hashTree/>
      </hashTree>
      <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="GET /orders" enabled="true">
        <stringProp name="HTTPSampler.method">GET</stringProp>
        <stringProp name="HTTPSampler.path">/orders/${orderId}</stringProp>
      </HTTPSamplerProxy>
      <hashTree/>
    </hashTree>
    <ThreadGroup testclass="ThreadGroup" testname="Step 2" enabled="true">
      <stringProp name="ThreadGroup.num_threads">100</stringProp>
    </ThreadGroup>
    <hashTree/>
  </hashTree>
</hashTree></jmeterTestPlan>'''

RESULT = '''<?xml version="1.0" encoding="UTF-8"?>
<testResults version="1.2">
<httpSample t="120" lt="100" s="true" lb="POST /login" rc="200" rm="OK">
  <responseHeader class="java.lang.String">HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8</responseHeader>
  <responseData class="java.lang.String">{"data": {"token": "abcdefghijklmnop", "user": {"id": 7}}, "key": "env-secret"}</responseData>
  <method class="java.lang.String">POST</method>
  <java.net.URL>https://api.example.test/login?k=env-secret</java.net.URL>
</httpSample>
<httpSample t="30" s="false" lb="GET /orders" rc="401" rm="Unauthorized">
  <assertionResult><name>Response Assertion</name><failure>true</failure><error>false</error>
    <failureMessage>Test failed: code expected to equal 200</failureMessage></assertionResult>
  <responseData class="java.lang.String">{"error": "missing token"}</responseData>
  <method class="java.lang.String">GET</method>
  <java.net.URL>https://api.example.test/orders/NOT_FOUND</java.net.URL>
</httpSample>
</testResults>'''


def groups(content):
    root = ET.fromstring(content)
    return {g.get('testname'): g for g in root.iter() if g.tag.endswith('ThreadGroup')}


def prop(element, name):
    return next(child.text for child in element.iter() if child.get('name') == name)


def test_the_trial_copy_runs_one_user_once_in_the_first_load_group_without_timers():
    copy = trial_run.trial_copy(PLAN)
    found = groups(copy)
    first = found['Step 1']
    assert prop(first, 'ThreadGroup.num_threads') == '1' and prop(first, 'LoopController.loops') == '1'
    assert prop(first, 'ThreadGroup.scheduler') == 'false' and prop(first, 'ThreadGroup.duration') in ('', None)
    assert found['Step 2'].get('enabled') == 'false'  # Later load steps do not run.
    assert found['Setup'].get('enabled') == 'true'
    assert all(t.get('enabled') == 'false' for t in ET.fromstring(copy).iter('ConstantTimer'))


def test_a_plan_without_a_standard_thread_group_cannot_be_tried():
    with pytest.raises(ValueError, match='ThreadGroup'):
        trial_run.trial_copy(b'<jmeterTestPlan><hashTree><TestPlan/><hashTree/></hashTree></jmeterTestPlan>')


def test_the_outline_lists_samplers_with_their_extractors_and_masks_credentials():
    outline = trial_run.outline(PLAN)
    assert [s['label'] for s in outline['samplers']] == ['POST /login', 'GET /orders']
    assert outline['samplers'][0]['extractors'] == [
        {'name': 'token', 'kind': 'json', 'variable': 'token', 'expression': '$.token'}]
    assert 'real-secret' not in json.dumps(outline)


def test_samples_carry_their_response_with_environment_secrets_masked(tmp_path):
    path = tmp_path / 'result.jtl'
    path.write_text(RESULT, encoding='utf-8')
    login, orders = trial_run.read_samples(str(path), ['env-secret'])
    assert login['success'] and login['code'] == '200' and login['content_type'] == 'application/json'
    assert 'env-secret' not in login['response'] + login['url']
    assert not orders['success'] and orders['failures'] == ['Test failed: code expected to equal 200']


def trial(tmp_path):
    path = tmp_path / 'result.jtl'
    path.write_text(RESULT, encoding='utf-8')
    return {'sha256': '', 'samples': trial_run.read_samples(str(path), []), 'outline': trial_run.outline(PLAN), 'fixes': []}


@pytest.mark.parametrize('fix, verified, applicable', [
    ({'sampler': 'POST /login', 'variable': 'token', 'expression': '$.data.token', 'action': 'change'}, True, True),
    ({'sampler': 'POST /login', 'variable': 'orderId', 'expression': '$.data.user.id', 'action': 'add'}, True, True),
    ({'sampler': 'POST /login', 'variable': 'token', 'expression': '$.data.jwt', 'action': 'change'}, False, False),
    ({'sampler': 'POST /login', 'variable': 'token', 'expression': '$..token', 'action': 'change'}, False, False),
    ({'sampler': 'POST /login', 'variable': 'token', 'expression': '$.data', 'action': 'change'}, False, False),
    ({'sampler': 'POST /login', 'variable': 'token', 'expression': '$.data.token', 'action': 'add'}, True, False),
    ({'sampler': 'GET /nowhere', 'variable': 'x', 'expression': '$.a', 'action': 'add'}, False, False),
])
def test_only_a_path_the_trial_response_holds_is_offered(tmp_path, fix, verified, applicable):
    checked = trial_run.check_fix(fix, trial(tmp_path))
    assert (checked['verified'], checked['applicable']) == (verified, applicable)
    if verified:
        assert 'abcdefghijklmnop' not in str(checked['preview'])  # A token is only hinted at.


def plan_on_disk(tmp_path):
    path = tmp_path / 'plan.jmx'
    path.write_bytes(PLAN)
    return SimpleNamespace(id='plan-1', project_id='p', jmx_path=str(path))


def test_applying_a_proven_fix_changes_and_adds_extractors_then_needs_a_new_trial(tmp_path):
    from app.services.jmeter.script_risk import content_hash
    plan = plan_on_disk(tmp_path)
    state = trial(tmp_path)
    state['sha256'] = content_hash(PLAN)
    state['fixes'] = [trial_run.check_fix(fix, state) for fix in (
        {'sampler': 'POST /login', 'variable': 'token', 'expression': '$.data.token', 'action': 'change'},
        {'sampler': 'POST /login', 'variable': 'orderId', 'expression': '$.data.user.id', 'action': 'add'})]
    trial_run._last[plan.id] = state
    trial_run.apply_fix(plan, 0)
    with pytest.raises(trial_run.TrialOutdated):
        trial_run.apply_fix(plan, 1)  # The plan changed; its old trial no longer describes it.
    state['sha256'] = content_hash(open(plan.jmx_path, 'rb').read())
    trial_run._last[plan.id] = state
    trial_run.apply_fix(plan, 1)
    extractors = trial_run.outline(open(plan.jmx_path, 'rb').read())['samplers'][0]['extractors']
    assert [(e['variable'], e['expression']) for e in extractors] == [('token', '$.data.token'), ('orderId', '$.data.user.id')]


def test_an_unproven_fix_is_never_written(tmp_path):
    from app.services.jmeter.script_risk import content_hash
    plan = plan_on_disk(tmp_path)
    state = trial(tmp_path)
    state['sha256'] = content_hash(PLAN)
    state['fixes'] = [trial_run.check_fix({'sampler': 'POST /login', 'variable': 'token', 'expression': '$.data.jwt'}, state)]
    trial_run._last[plan.id] = state
    with pytest.raises(ValueError, match='证实'):
        trial_run.apply_fix(plan, 0)
    assert open(plan.jmx_path, 'rb').read() == PLAN


@pytest.mark.asyncio
async def test_a_trial_runs_the_copy_and_leaves_nothing_behind(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.jmeter import executor as executor_module
    from app.services.storage import store
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(tmp_path / 'reports'))
    monkeypatch.setattr(store, 'get_project', lambda project_id: None)
    plan = plan_on_disk(tmp_path)
    ran = {}

    async def execute(jmx_path, execution_id, *, working_directory=None, trial=False):
        ran.update(content=open(jmx_path, 'rb').read(), trial=trial)
        with open(tmp_path / 'reports' / execution_id / 'result.jtl', 'w', encoding='utf-8') as out:
            out.write(RESULT)
        return SimpleNamespace(error_message=None)
    monkeypatch.setattr(executor_module, 'get_executor', lambda: SimpleNamespace(execute=execute))
    result = await trial_run.run(plan)
    assert ran['trial'] and prop(groups(ran['content'])['Step 1'], 'ThreadGroup.num_threads') == '1'
    assert result['status'] == 'failed' and (result['passed'], result['failed']) == (1, 1)
    assert '_full' not in result['samples'][0]
    assert list((tmp_path / 'reports').iterdir()) == []


@pytest.mark.asyncio
async def test_a_plan_with_scripts_needs_approval_before_its_trial(tmp_path, monkeypatch):
    from app.services.jmeter.script_risk import ScriptApprovalRequired
    monkeypatch.setattr(trial_run, 'scan_scripts', lambda content, base: [{'name': 'Shell', 'kind': 'SystemSampler'}])
    with pytest.raises(ScriptApprovalRequired):
        await trial_run.run(plan_on_disk(tmp_path))


@pytest.mark.asyncio
async def test_a_diagnosis_keeps_only_checked_fixes(tmp_path):
    from app.services.jmeter.script_risk import content_hash
    plan = plan_on_disk(tmp_path)
    state = trial(tmp_path)
    state['sha256'] = content_hash(PLAN)
    trial_run._last[plan.id] = state
    reply = json.dumps({'summary': 'GET /orders 401：token 没取到', 'advice': ['检查账号'], 'fixes': [
        {'sampler': 'POST /login', 'variable': 'token', 'expression': '$.data.token', 'action': 'change', 'reason': 'r'},
        {'sampler': 'POST /login', 'variable': 'token', 'expression': '$.made.up', 'action': 'change', 'reason': 'r'}]})
    adapter = SimpleNamespace(chat=AsyncMock(return_value=reply))
    diagnosis = await trial_run.diagnose(plan, adapter)
    assert [f['applicable'] for f in diagnosis['fixes']] == [True, False]
    sent = adapter.chat.await_args.args[0][1].content
    assert 'real-secret' not in sent and '_full' not in sent


def test_the_trial_copy_lets_every_request_through_its_controllers():
    plan = PLAN.replace(b'<HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="GET /orders"', b'''<ThroughputController testclass="ThroughputController" testname="30%" enabled="true">
        <intProp name="ThroughputController.style">1</intProp>
        <FloatProperty><name>ThroughputController.percentThroughput</name><value>30.0</value></FloatProperty>
      </ThroughputController>
      <hashTree>
        <RandomController testclass="RandomController" guiclass="RandomControlGui" testname="one of" enabled="true"/>
        <hashTree/>
      </hashTree>
      <HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="GET /orders"''')
    root = ET.fromstring(trial_run.trial_copy(plan))
    assert root.find('.//ThroughputController/FloatProperty/value').text == '100.0'
    assert root.find('.//RandomController') is None and root.find('.//GenericController').get('testclass') == 'GenericController'
