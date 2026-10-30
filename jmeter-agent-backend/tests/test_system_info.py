"""The system page reports where JMeter really is, and reports follow the configured folder."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.core import config
from app.services.jmeter.executor import JMeterExecutor


@pytest.mark.parametrize('name, home', [('bin', 'bin'), ('root', '.'), ('sibling', 'lib')])
def test_jmeter_is_found_from_its_bin_folder_or_install_root(tmp_path, monkeypatch, name, home):
    bin_dir = tmp_path / 'jmeter' / 'bin'
    bin_dir.mkdir(parents=True)
    (tmp_path / 'jmeter' / 'lib').mkdir()
    for script in ('jmeter.bat', 'jmeter'):
        (bin_dir / script).write_text('')
    monkeypatch.setattr(config.settings, 'JMETER_HOME', str(tmp_path / 'jmeter' / home))
    monkeypatch.setattr(config.settings, 'JMETER_EXECUTION_MODE', 'local')
    assert JMeterExecutor().jmeter_path.startswith(str(bin_dir))


@pytest.mark.asyncio
async def test_info_shows_the_jmeter_found_on_path_not_the_configured_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, 'JMETER_EXECUTION_MODE', 'local')
    monkeypatch.setattr(config.settings, 'JMETER_HOME', str(tmp_path / 'missing'))
    monkeypatch.setattr(routes, 'get_executor', lambda: SimpleNamespace(jmeter_path='jmeter.bat'))
    found = str(tmp_path / 'tools' / 'jmeter.bat')
    monkeypatch.setattr(routes.shutil, 'which', lambda name: found)
    info = await routes.system_info()
    assert info['jmeter_executable'] == found and info['jmeter_home'] == str(tmp_path / 'tools')
    assert info['execution_mode'] == 'local' and info['jmeter_image'] is None


@pytest.mark.asyncio
async def test_info_says_when_jmeter_is_missing(monkeypatch):
    def missing():
        raise FileNotFoundError('JMeter executable not found')
    monkeypatch.setattr(config.settings, 'JMETER_EXECUTION_MODE', 'local')
    monkeypatch.setattr(routes, 'get_executor', missing)
    info = await routes.system_info()
    assert info['jmeter_home'] is None and info['jmeter_executable'] is None


@pytest.mark.asyncio
async def test_container_mode_shows_the_image_not_a_local_jmeter(monkeypatch):
    monkeypatch.setattr(config.settings, 'JMETER_EXECUTION_MODE', 'container')
    monkeypatch.setattr(config.settings, 'JMETER_CONTAINER_IMAGE', 'jmeter@sha256:abc')
    info = await routes.system_info()
    assert info['execution_mode'] == 'container' and info['jmeter_image'] == 'jmeter@sha256:abc'
    assert info['jmeter_home'] is None and info['jmeter_executable'] is None


def test_reports_follow_a_folder_changed_while_running(tmp_path, monkeypatch):
    from app.main import ReportFiles
    first, second = tmp_path / 'first', tmp_path / 'second'
    for folder in (first, second):
        (folder / 'run').mkdir(parents=True)
    (second / 'run' / 'result.jtl').write_text('new')
    monkeypatch.setattr(config, 'load_paths_config', lambda: {'reports_dir': str(first)})
    app = FastAPI()
    app.mount('/reports', ReportFiles(directory=str(first)))
    client = TestClient(app)
    assert client.get('/reports/run/result.jtl').status_code == 404
    monkeypatch.setattr(config, 'load_paths_config', lambda: {'reports_dir': str(second)})
    assert client.get('/reports/run/result.jtl').text == 'new'
