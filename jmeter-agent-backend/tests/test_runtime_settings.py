"""Settings that claim to change how JMeter runs must reach the run: heap, timeout, RMI and logging."""
import logging
import os

import pytest

import app.services.jmeter.executor as executor_mod
from app.core import logging_setup
from app.core.config import Settings, settings
from app.services.jmeter.executor import JMeterExecutor, jvm_environment, run_timeout

PLAN = '''<jmeterTestPlan><hashTree><TestPlan/><hashTree>
<ThreadGroup enabled="true"><stringProp name="ThreadGroup.num_threads">5</stringProp>
<boolProp name="ThreadGroup.scheduler">{scheduler}</boolProp>
<stringProp name="ThreadGroup.duration">{duration}</stringProp>
<stringProp name="ThreadGroup.delay">0</stringProp></ThreadGroup><hashTree/>
</hashTree></hashTree></jmeterTestPlan>'''


def _plan(tmp_path, duration, scheduler='true'):
    path = tmp_path / 'plan.jmx'
    path.write_text(PLAN.format(duration=duration, scheduler=scheduler), encoding='utf-8')
    return str(path)


def test_heap_settings_reach_the_jvm(monkeypatch):
    for name in ('HEAP', 'GC_ALGO', 'JVM_ARGS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, 'JMETER_HEAP_MIN', '512m')
    monkeypatch.setattr(settings, 'JMETER_HEAP_MAX', '4g')
    monkeypatch.setattr(settings, 'JMETER_EXTRA_OPTS', '-Dfoo=bar')
    env = jvm_environment()
    assert env['HEAP'].startswith('-Xms512m -Xmx4g')
    assert env['GC_ALGO'] == settings.JMETER_GC_OPTS
    assert env['JVM_ARGS'] == '-Dfoo=bar'
    # What bin/jmeter reads on POSIX.
    monkeypatch.setattr(settings, 'JMETER_EXECUTION_MODE', 'local')
    assert JMeterExecutor._process_environment()['HEAP'] == env['HEAP']


def test_environment_variables_win_over_heap_settings(monkeypatch):
    monkeypatch.setenv('HEAP', '-Xms1g -Xmx1g')
    monkeypatch.setenv('JVM_ARGS', '-Dmine=1')
    monkeypatch.setattr(settings, 'JMETER_EXTRA_OPTS', '-Dfoo=bar')
    env = jvm_environment()
    assert env['HEAP'] == '-Xms1g -Xmx1g'
    assert env['JVM_ARGS'] == '-Dfoo=bar -Dmine=1'  # Later arguments win in the JVM.


def test_container_runs_do_not_get_host_heap_settings(monkeypatch):
    monkeypatch.delenv('HEAP', raising=False)
    monkeypatch.setattr(settings, 'JMETER_EXECUTION_MODE', 'container')
    assert 'HEAP' not in JMeterExecutor._process_environment()


@pytest.mark.parametrize('value', ['2', '2gb', '-Xmx2g', '0m', '2g -XX:+Foo'])
def test_malformed_heap_sizes_are_refused(value):
    with pytest.raises(ValueError):
        Settings(JMETER_HEAP_MAX=value)


def test_a_plan_longer_than_the_timeout_gets_its_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'JMETER_TIMEOUT', 3600)
    assert run_timeout(_plan(tmp_path, 7200)) == 7200 + 720
    assert run_timeout(_plan(tmp_path, 3500)) == 3850  # Duration plus a tenth of it.
    assert run_timeout(_plan(tmp_path, 3400)) == 3740
    assert run_timeout(_plan(tmp_path, 2000)) == 3600  # Short of the timeout even with its five minutes.
    assert run_timeout(_plan(tmp_path, 60)) == 3600


def test_a_plan_without_a_fixed_duration_keeps_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'JMETER_TIMEOUT', 900)
    assert run_timeout(_plan(tmp_path, 7200, scheduler='false')) == 900
    assert run_timeout(str(tmp_path / 'missing.jmx')) == 900


@pytest.mark.asyncio
@pytest.mark.parametrize('ssl, expected', [(False, True), (True, False)])
async def test_distributed_runs_disable_rmi_ssl_unless_enabled(tmp_path, monkeypatch, ssl, expected):
    executor = object.__new__(JMeterExecutor)
    executor.system, executor.jmeter_path = 'Linux', 'jmeter'
    monkeypatch.setattr(settings, 'JMETER_REMOTE_SSL_ENABLED', ssl)
    monkeypatch.setattr(executor_mod.settings.__class__, 'REPORTS_DIR', property(lambda self: str(tmp_path)))
    seen = {}

    async def launch(cmd, execution_id, flag_file):
        seen['cmd'] = cmd
        raise RuntimeError('stop before starting a process')
    executor._launch = launch
    await executor.execute(_plan(tmp_path, 60), 'rmi-check', remote_engines=['engine:1099'])
    assert ('-Jserver.rmi.ssl.disable=true' in seen['cmd']) is expected


def test_logging_writes_info_lines_to_the_file(tmp_path, monkeypatch):
    path = tmp_path / 'logs' / 'backend.log'
    monkeypatch.setattr(settings, 'LOG_FILE', str(path))
    monkeypatch.setattr(settings, 'LOG_LEVEL', 'INFO')
    try:
        logging_setup.configure_logging()
        logging_setup.configure_logging()  # A second call must not double the handlers.
        logger = logging.getLogger('app')
        assert sum(1 for handler in logger.handlers if getattr(handler, logging_setup._MARK, False)) == 2
        logging.getLogger('app.services.example').info('heap %s', '2g')
        for handler in logger.handlers:
            handler.flush()
        assert 'INFO app.services.example: heap 2g' in path.read_text(encoding='utf-8')
    finally:
        monkeypatch.setattr(settings, 'LOG_FILE', 'off')
        logging_setup.configure_logging()
        assert logging_setup.log_file() == ''


def test_a_key_can_live_in_the_environment_instead_of_the_file(tmp_path, monkeypatch):
    import json
    from app.core import config
    monkeypatch.setattr(settings, 'AI_MODELS_CONFIG_FILE', str(tmp_path / 'ai_models_config.json'))
    monkeypatch.setenv('MY_MODEL_KEY', 'from-the-environment')
    config.save_user_model_configs([{'name': 'a', 'api_key': 'env:MY_MODEL_KEY'}, {'name': 'b', 'api_key': 'typed-in'}])
    saved = json.loads((tmp_path / 'ai_models_config.json').read_text(encoding='utf-8'))
    assert 'from-the-environment' not in json.dumps(saved)
    assert config.resolve_api_key(saved[0]['api_key']) == 'from-the-environment'
    assert config.resolve_api_key(saved[1]['api_key']) == 'typed-in'
    assert config.resolve_api_key('env:NOT_SET_ANYWHERE') == '' and config.resolve_api_key(None) == ''
    # The reference names no secret and is shown; a stored key never is.
    assert config.sanitize_model_config(saved[0])['api_key'] == 'env:MY_MODEL_KEY'
    assert config.sanitize_model_config(saved[1])['api_key'] == '***'
    assert [name for name in os.listdir(tmp_path)] == ['ai_models_config.json']  # No temporary file left behind.
