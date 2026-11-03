"""SelfHeal outcomes with temporary plans and fully fake execution."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from app.services.ai.agents.selfheal_agent import SelfHealAgent


@pytest.mark.asyncio
async def test_auxiliary_agent_budget_fails_before_model_and_preserves_fallback(monkeypatch):
    from app.core.config import settings
    from app.services.ai.adapters.openai_adapter import OpenAIAdapter
    from app.services.ai.adapters.factory import AIAdapterFactory
    adapter = object.__new__(OpenAIAdapter)
    adapter.max_tokens = 8000
    adapter.chat = AsyncMock(return_value='[]')
    monkeypatch.setattr(settings, 'AGENT_CONTEXT_TOKEN_BUDGET', 400)
    monkeypatch.setattr(AIAdapterFactory, 'create', lambda: adapter)
    assert await SelfHealAgent()._llm_diagnose([], [], 'fixture log') == []
    adapter.chat.assert_not_awaited()


@pytest.fixture
def plan(tmp_path, monkeypatch):
    import subprocess
    from app.core.config import settings
    from app.services import execution_lifecycle
    from app.services.jmeter.jmx_generator import JMXGenerator
    from app.models.schemas import APIEndpoint, TestConfig, ExtractionRule
    def forbidden(*args, **kwargs):
        raise AssertionError('No real JMeter, subprocess or LLM in outcome tests')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(execution_lifecycle, 'get_executor', forbidden)
    monkeypatch.setattr(settings, 'TEST_PLANS_DIR_DEFAULT', str(tmp_path / 'plans'))
    monkeypatch.setattr(settings, 'REPORTS_DIR_DEFAULT', str(tmp_path / 'reports'))
    monkeypatch.setattr('app.core.config.load_paths_config', lambda: {})
    from app.services.ai.adapters.factory import AIAdapterFactory
    monkeypatch.setattr(AIAdapterFactory, 'create', forbidden)
    path = tmp_path / 'plans' / 'fixture.jmx'
    path.parent.mkdir()
    path.write_text(JMXGenerator().generate_basic_jmx([APIEndpoint(name='auth', method='GET', path='/',
        extractions=[ExtractionRule(ref_name='token', extract_type='json_path', expression='$.old.token')])],
        TestConfig(test_type='baseline'), 'https://fixture.invalid'), encoding='utf-8')
    report = tmp_path / 'reports' / 'failed'
    report.mkdir(parents=True)
    (report / 'result.jtl').write_text('timeStamp,elapsed,label,responseCode,success\n1,10,auth,401,false\n')
    return path


@pytest.mark.asyncio
async def test_selfheal_parses_the_jtl_off_the_event_loop(plan, monkeypatch):
    import threading
    from app.services.jmeter.report_parser import ReportParser
    threads = []
    def parse(self, path):
        threads.append(threading.current_thread())
        return ReportParser()._empty_summary()
    monkeypatch.setattr(ReportParser, 'parse_jtl', parse)
    agent = SelfHealAgent()
    monkeypatch.setattr(agent, '_llm_diagnose', AsyncMock(return_value=[]))
    await agent.diagnose_and_fix(str(plan), 'failed', auto_re_run=False)
    assert threads and threading.current_thread() not in threads


@pytest.mark.asyncio
async def test_selfheal_without_response_evidence_never_edits(plan, monkeypatch):
    agent = SelfHealAgent()
    monkeypatch.setattr(agent, '_llm_diagnose', AsyncMock(return_value=[]))
    before = plan.read_bytes()
    result = await agent.diagnose_and_fix(str(plan), 'failed', auto_re_run=False)
    assert not result.success
    assert plan.read_bytes() == before
    assert not result.applied and not result.verified


@pytest.mark.asyncio
@pytest.mark.parametrize('status,count,errors', [('failed', 0, 0), ('cancelled', 0, 0),
    ('completed', 0, 0), ('completed', 1, 1), ('completed', 1, 0)])
async def test_selfheal_verifies_real_samples_and_cleared_errors(plan, monkeypatch, status, count, errors):
    from app.services import execution_lifecycle
    agent = SelfHealAgent()
    monkeypatch.setattr(agent, '_extract_failed_response_bodies', lambda path: [
        {'response_code': '401', 'label': 'auth', 'response_body': '{"new":{"token":"abcdefghijk"}}'}])
    execution = SimpleNamespace(status=SimpleNamespace(value=status), error_message='fixture',
        summary=SimpleNamespace(total_requests=count, error_count=errors, error_rate=100 if errors else 0))
    monkeypatch.setattr(execution_lifecycle.execution_lifecycle, 'execute', AsyncMock(return_value=execution))
    result = await agent.diagnose_and_fix(str(plan), 'failed')
    expected = status == 'completed' and count > 0 and errors == 0
    assert result.applied and result.verified is expected
    assert result.success is expected and result.re_run_success is expected
    if not expected:
        assert result.error


@pytest.mark.asyncio
async def test_agents_reject_unsafe_paths_and_execution_ids(plan, monkeypatch):
    original = plan.read_bytes()
    result = await SelfHealAgent().diagnose_and_fix(str(plan), '../escape')
    assert not result.success and result.error
    assert plan.read_bytes() == original


@pytest.mark.asyncio
async def test_selfheal_unverified_llm_suggestion_does_not_write(plan, monkeypatch):
    from app.services.ai.agents.selfheal_agent import ExtractionFix
    agent = SelfHealAgent()
    monkeypatch.setattr(agent, '_extract_failed_response_bodies', lambda path: [
        {'response_code': '401', 'label': 'auth', 'response_body': '{"message":"denied"}'}])
    monkeypatch.setattr(agent, '_llm_diagnose', AsyncMock(return_value=[
        ExtractionFix('$.old.token', '$.missing.token', 'token', .9, 'llm')]))
    before = plan.read_bytes()
    result = await agent.diagnose_and_fix(str(plan), 'failed', auto_re_run=False)
    assert not result.success and not result.applied and result.suggestions
    assert plan.read_bytes() == before


@pytest.mark.asyncio
async def test_selfheal_rerun_exception_is_not_success(plan, monkeypatch):
    from app.services import execution_lifecycle
    agent = SelfHealAgent()
    monkeypatch.setattr(agent, '_extract_failed_response_bodies', lambda path: [
        {'response_code': '401', 'label': 'auth', 'response_body': '{"new":{"token":"abcdefghijk"}}'}])
    monkeypatch.setattr(execution_lifecycle.execution_lifecycle, 'execute', AsyncMock(side_effect=RuntimeError('fixture exception')))
    result = await agent.diagnose_and_fix(str(plan), 'failed')
    assert result.applied and not result.verified and not result.success
    assert 'fixture exception' in result.error


@pytest.mark.asyncio
async def test_selfheal_unknown_variable_is_a_suggestion_not_an_applied_fix(plan, monkeypatch):
    from app.services.ai.agents.selfheal_agent import ExtractionFix
    agent = SelfHealAgent()
    monkeypatch.setattr(agent, '_extract_failed_response_bodies', lambda path: [
        {'response_code': '401', 'label': 'auth', 'response_body': '{"value":"abcdefghijk"}'}])
    monkeypatch.setattr(agent, '_llm_diagnose', AsyncMock(return_value=[
        ExtractionFix('$.old', '$.value', 'unknown', .9, 'llm')]))
    before = plan.read_bytes()
    result = await agent.diagnose_and_fix(str(plan), 'failed', auto_re_run=False)
    assert not result.applied and not result.success and result.suggestions
    assert plan.read_bytes() == before


@pytest.mark.asyncio
async def test_applied_without_rerun_is_unverified_and_keeps_existing_backup(plan, monkeypatch):
    agent = SelfHealAgent()
    legacy_backup = Path(str(plan) + '.bak')
    legacy_backup.write_bytes(b'preserve user backup')
    monkeypatch.setattr(agent, '_extract_failed_response_bodies', lambda path: [
        {'response_code': '401', 'label': 'auth', 'response_body': '{"new":{"token":"abcdefghijk"}}'}])
    result = await agent.diagnose_and_fix(str(plan), 'failed', auto_re_run=False)
    assert result.applied and not result.verified and not result.test_re_run
    assert legacy_backup.read_bytes() == b'preserve user backup'
    assert list(plan.parent.glob('*.selfheal.*.bak'))
