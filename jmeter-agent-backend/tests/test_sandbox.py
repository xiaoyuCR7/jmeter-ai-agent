"""Execution isolation: the local script gate and the container launcher's boundaries."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.services.jmeter.executor as executor_mod
from app.services.jmeter import script_risk
from app.services.jmeter.executor import JMeterExecutor


PLAN = b'''<?xml version="1.0"?><jmeterTestPlan><hashTree>
<TestPlan testclass="TestPlan" testname="Plan"/><hashTree>
<HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="GET ${__groovy(new File('/etc/passwd').text,)}"/><hashTree/>
<JSR223PostProcessor testclass="JSR223PostProcessor" testname="Parse token"/><hashTree/>
<SystemSampler testclass="SystemSampler" testname="Run shell"/><hashTree/>
<BeanShellSampler testclass="BeanShellSampler" testname="Old script" enabled="false"/><hashTree/>
<IncludeController testclass="IncludeController" testname="Shared steps"/><hashTree/>
</hashTree></hashTree></jmeterTestPlan>'''


def test_scan_finds_script_elements_functions_and_includes_but_skips_disabled():
    findings = script_risk.scan(PLAN)
    assert findings == [
        {'name': 'Parse token', 'kind': 'JSR223PostProcessor'},
        {'name': 'Run shell', 'kind': 'SystemSampler'},
        {'name': 'Shared steps', 'kind': 'IncludeController'},
        {'name': '${__groovy(...)}', 'kind': '__groovy'},
    ]
    assert 'Run shell (SystemSampler)' in str(script_risk.ScriptApprovalRequired(findings, 'digest'))


def test_plain_http_plans_and_unparseable_files():
    plain = b'<jmeterTestPlan><hashTree><HTTPSamplerProxy testclass="HTTPSamplerProxy" testname="${__Random(1,9,)}"/></hashTree></jmeterTestPlan>'
    assert script_risk.scan(plain) == []
    assert script_risk.scan(b'<not xml')[0]['kind'] == 'unparsed'


def plan_with(body: str) -> bytes:
    return f'<jmeterTestPlan><hashTree>{body}</hashTree></jmeterTestPlan>'.encode('utf-8')


@pytest.mark.parametrize('body, kind', [
    # Decoded by the XML parser before JMeter evaluates it, so a byte search never sees these.
    ('<stringProp name="x">$&#123;__groovy(1)}</stringProp>', '__groovy'),
    ('<stringProp name="x"><![CDATA[${__groovy(1)}]]></stringProp>', '__groovy'),
    # Read and write files with no script at all.
    ('<stringProp name="x">${__FileToString(/etc/passwd)}</stringProp>', '__FileToString'),
    ('<stringProp name="x">${__StringToFile(/tmp/a,b)}</stringProp>', '__StringToFile'),
    ('<stringProp name="x">${__CSVRead(/etc/passwd,0)}</stringProp>', '__CSVRead'),
    # __eval runs a function assembled from variables at run time.
    ('<stringProp name="x">${__eval(${a}${b})}</stringProp>', '__eval'),
    ('<JavaSampler testclass="JavaSampler" testname="Java"/>', 'JavaSampler'),
    ('<JUnitSampler testclass="JUnitSampler" testname="JUnit"/>', 'JUnitSampler'),
    # JMeter accepts fully qualified class names as well as its aliases.
    ('<x testclass="org.apache.jmeter.protocol.java.sampler.JSR223Sampler" testname="s"/>', 'JSR223Sampler'),
    ('<TestPlan testclass="TestPlan"><stringProp name="TestPlan.user_define_classpath">/tmp/evil.jar</stringProp></TestPlan>',
     'TestPlan.user_define_classpath'),
])
def test_scan_sees_what_jmeter_will_evaluate(body, kind):
    assert kind in [item['kind'] for item in script_risk.scan(plan_with(body))]


@pytest.mark.parametrize('element', [
    '<CSVDataSet testclass="CSVDataSet"><stringProp name="filename">{path}</stringProp></CSVDataSet>',
    '<HTTPSamplerProxy testclass="HTTPSamplerProxy"><elementProp elementType="HTTPFileArg">'
    '<stringProp name="File.path">{path}</stringProp></elementProp></HTTPSamplerProxy>',
    '<ResultCollector testclass="ResultCollector"><stringProp name="filename">{path}</stringProp></ResultCollector>',
    '<ResultSaver testclass="ResultSaver"><stringProp name="FileSaver.base_filename">{path}</stringProp></ResultSaver>',
])
@pytest.mark.parametrize('path, flagged', [
    ('C:/Users/me/.ssh/id_rsa', True), ('/etc/passwd', True), ('../../secrets.csv', True),
    ('..\\secrets.csv', True), ('${path}', True), ('users.csv', False), ('data/users.csv', False),
])
def test_file_paths_must_stay_in_the_plan_folder(tmp_path, monkeypatch, element, path, flagged):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'TEST_PLANS_DIR_DEFAULT', str(tmp_path))
    content = plan_with(element.replace('{path}', path))
    assert bool(script_risk.scan(content, str(tmp_path))) is flagged


def test_files_in_the_app_folders_and_harmless_properties_pass(tmp_path, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, 'TEST_PLANS_DIR_DEFAULT', str(tmp_path / 'plans'))
    monkeypatch.setattr(settings, 'BASE_DIR', str(tmp_path))
    inside = str(tmp_path / 'test_data' / 'project' / 'users.csv')
    content = plan_with(f'<CSVDataSet testclass="CSVDataSet"><stringProp name="filename">{inside}</stringProp>'
                        '<stringProp name="fileEncoding">UTF-8</stringProp></CSVDataSet>'
                        '<ResultCollector testclass="ResultCollector"><stringProp name="filename"></stringProp></ResultCollector>'
                        '<elementProp elementType="HTTPFileArg"><stringProp name="File.mimetype">text/csv</stringProp></elementProp>')
    assert script_risk.scan(content) == []


def test_generated_plans_have_no_scripts():
    from app.services.jmeter.jmx_generator import JMXGenerator
    from app.models.schemas import ExtractionRule, TestConfig, APIEndpoint
    content = JMXGenerator().generate_basic_jmx([
        APIEndpoint(name='Login', method='POST', path='/login', body={'user': 'u'},
                    extractions=[ExtractionRule(ref_name='token', extract_type='json_path', expression='$.token')]),
        APIEndpoint(name='List', method='GET', path='/api/items', headers={'Authorization': 'Bearer ${token}'}),
    ], TestConfig(test_type='baseline'), 'https://example.test')
    assert script_risk.scan(content.encode('utf-8')) == []


# ── Agent and Web entry points ─────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize('approved', [True, False])
async def test_agent_runs_scripts_only_after_a_second_explicit_approval(monkeypatch, approved):
    from app.services.ai.agent.tools import jmeter as tools
    from app.services import confirmations as confirmation_module
    finding = [{'name': 'Run shell', 'kind': 'SystemSampler'}]
    done = SimpleNamespace(id='x')
    execute = AsyncMock(side_effect=[script_risk.ScriptApprovalRequired(finding, 'digest'), done])
    monkeypatch.setattr(tools.execution_lifecycle, 'execute', execute)
    request = AsyncMock(return_value=approved)
    monkeypatch.setattr(confirmation_module.confirmations, 'request', request)
    if approved:
        assert await tools.execute_with_script_approval('execute_test', 'plan.jmx', 'run') is done
        assert execute.await_args.kwargs['approved_sha256'] == 'digest'
    else:
        with pytest.raises(ValueError, match='did not approve'):
            await tools.execute_with_script_approval('execute_test', 'plan.jmx', 'run')
        assert execute.await_count == 1
    request.assert_awaited_once_with('execute_test', {'jmx_path': 'plan.jmx', 'runs_scripts_on_this_machine': finding})


@pytest.mark.asyncio
async def test_web_run_returns_the_scripts_for_the_ui_to_confirm(monkeypatch):
    from fastapi import HTTPException
    from app.api import routes
    finding = [{'name': 'Parse token', 'kind': 'JSR223PostProcessor'}]
    plan = SimpleNamespace(jmx_path='plan.jmx', test_config=SimpleNamespace(model_dump=lambda: {}))
    monkeypatch.setitem(routes.store.test_plans, 'scripted', plan)
    submit = AsyncMock(side_effect=script_risk.ScriptApprovalRequired(finding, 'digest'))
    monkeypatch.setattr(routes.execution_lifecycle, 'submit', submit)
    with pytest.raises(HTTPException) as refused:
        await routes.execute_test_plan('scripted', None, None, None)
    assert refused.value.status_code == 409
    assert refused.value.detail['code'] == 'scripts_need_approval' and refused.value.detail['findings'] == finding
    submit.side_effect = None
    submit.return_value = 'started'
    assert await routes.execute_test_plan('scripted', None, None, 'digest') == 'started'
    assert submit.await_args.kwargs['approved_sha256'] == 'digest'


# ── Container launcher ─────────────────────────────────────────

@pytest.fixture
def container(tmp_path, monkeypatch):
    plans = tmp_path / 'plans'
    project = plans / 'proj'
    project.mkdir(parents=True)
    outputs = tmp_path / 'reports' / 'run'
    outputs.mkdir(parents=True)
    jmx = outputs / 'snapshot.jmx'
    jmx.write_text('<jmeterTestPlan/>')
    for name, value in (('TEST_PLANS_DIR_DEFAULT', str(plans)), ('JMETER_EXECUTION_MODE', 'container'),
                        ('JMETER_CONTAINER_IMAGE', 'test-image@sha256:' + 'a' * 64), ('JMETER_CONTAINER_NETWORK', '')):
        monkeypatch.setattr(executor_mod.settings, name, value)
    monkeypatch.setattr(executor_mod.shutil, 'which', lambda name: '/mock/docker' if name == 'docker' else None)
    monkeypatch.setattr('app.core.config.load_paths_config', lambda: {})
    ex = JMeterExecutor.__new__(JMeterExecutor)
    ex.system, ex.jmeter_path, ex._running_processes = 'Linux', 'jmeter', {}

    def argv(working_directory):
        entry = {'working_directory': str(working_directory)}
        command, _, _, _ = ex._build_command(str(jmx), str(outputs))
        for stale in ('result.jtl', 'jmeter.log'):
            (outputs / stale).unlink(missing_ok=True)
        return ex._container_command(command, entry, 'run-42')
    return SimpleNamespace(argv=argv, plans=plans, project=project, outputs=outputs)


def _mount_sources(argv):
    return [argv[i + 1] for i, item in enumerate(argv) if item == '--mount']


def test_container_is_labelled_reaped_and_shares_no_ipc(container):
    argv = container.argv(container.project)
    assert '--init' in argv and '--ipc=none' in argv
    assert 'jmeter-agent=true' in argv and 'jmeter-agent.execution=run-42' in argv
    assert '--network' not in argv
    assert any(f'source={container.project},target=/work,readonly' in m for m in _mount_sources(argv))


def test_configured_network_is_used_and_namespace_sharing_refused(container, monkeypatch):
    monkeypatch.setattr(executor_mod.settings, 'JMETER_CONTAINER_NETWORK', 'jmeter-egress')
    argv = container.argv(container.project)
    assert argv[argv.index('--network') + 1] == 'jmeter-egress'
    for bad in ('host', 'container:db', 'bad name'):
        monkeypatch.setattr(executor_mod.settings, 'JMETER_CONTAINER_NETWORK', bad)
        with pytest.raises(ValueError, match='JMETER_CONTAINER_NETWORK'):
            container.argv(container.project)


def test_plan_at_the_root_does_not_expose_other_projects(container):
    mounts = _mount_sources(container.argv(container.plans))
    work = next(m for m in mounts if 'target=/work' in m)
    assert work.startswith(f"type=bind,source={container.outputs / 'work'},")
    assert not any(f'source={container.plans},' in m for m in mounts)


def test_docker_variables_reach_only_the_docker_cli(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'tcp://daemon.example.test:2376')
    monkeypatch.setattr(executor_mod.settings, 'JMETER_EXECUTION_MODE', 'local')
    assert 'DOCKER_HOST' not in JMeterExecutor._process_environment()
    monkeypatch.setattr(executor_mod.settings, 'JMETER_EXECUTION_MODE', 'container')
    assert JMeterExecutor._process_environment()['DOCKER_HOST'] == 'tcp://daemon.example.test:2376'


@pytest.mark.parametrize('listed, removed', [('abc\ndef\n', ['abc', 'def']), ('', None)])
def test_startup_removes_containers_left_by_a_previous_backend(monkeypatch, listed, removed):
    monkeypatch.setattr(executor_mod.settings, 'JMETER_EXECUTION_MODE', 'container')
    monkeypatch.setattr(executor_mod.shutil, 'which', lambda name: '/mock/docker')
    run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout=listed), SimpleNamespace(returncode=0, stdout='')])
    monkeypatch.setattr(executor_mod.subprocess, 'run', run)
    assert executor_mod.remove_orphan_containers() == len(removed or [])
    assert run.call_args_list[0].args[0] == ['/mock/docker', 'ps', '--all', '--quiet',
                                             '--filter', 'label=jmeter-agent=true']
    if removed:
        assert run.call_args_list[1].args[0] == ['/mock/docker', 'rm', '--force', *removed]
    else:
        assert run.call_count == 1


def test_orphan_sweep_is_skipped_in_local_mode(monkeypatch):
    monkeypatch.setattr(executor_mod.settings, 'JMETER_EXECUTION_MODE', 'local')
    run = Mock()
    monkeypatch.setattr(executor_mod.subprocess, 'run', run)
    assert executor_mod.remove_orphan_containers() == 0
    run.assert_not_called()


# ── Output limit ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runs_whose_output_outgrows_the_limit_are_stopped(tmp_path, monkeypatch):
    ex = JMeterExecutor.__new__(JMeterExecutor)
    finished = asyncio.Event()
    async def never_finishes(*args, **kwargs):
        await finished.wait()
    monkeypatch.setattr(ex, '_wait_for_completion', never_finishes, raising=False)
    monkeypatch.setattr(executor_mod, 'OUTPUT_CHECK_SECONDS', 0.01)
    monkeypatch.setattr(executor_mod.settings, 'JMETER_MAX_OUTPUT_MB', 1)
    (tmp_path / 'result.jtl').write_bytes(b'x' * (2 * 1024 * 1024))
    with pytest.raises(RuntimeError, match='JMETER_MAX_OUTPUT_MB'):
        await ex._wait_within_output_limit('flag', None, str(tmp_path / 'result.jtl'), str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize('limit_mb', [0, 100])
async def test_runs_within_the_limit_or_without_one_finish_normally(tmp_path, monkeypatch, limit_mb):
    ex = JMeterExecutor.__new__(JMeterExecutor)
    async def finishes(*args, **kwargs):
        await asyncio.sleep(0.05)
        return 'done'
    monkeypatch.setattr(ex, '_wait_for_completion', finishes, raising=False)
    monkeypatch.setattr(executor_mod, 'OUTPUT_CHECK_SECONDS', 0.01)
    monkeypatch.setattr(executor_mod.settings, 'JMETER_MAX_OUTPUT_MB', limit_mb)
    (tmp_path / 'result.jtl').write_bytes(b'x' * 1024)
    assert await ex._wait_within_output_limit('flag', None, None, str(tmp_path)) == 'done'
