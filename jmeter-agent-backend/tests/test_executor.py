"""Tests for JMeterExecutor main flows.

No real JMeter installation is required: executors under test are built via
object.__new__ so __init__ never probes the filesystem, and process / polling
behavior is stubbed at the module boundary.
"""

import asyncio
import os
import xml.etree.ElementTree as ET

import pytest

import app.services.jmeter.executor as executor_mod
from app.services.jmeter.executor import (
    JMeterExecutor,
    get_executor,
    _is_pid_running,
    _check_process_exited_ok,
)


def make_executor(system: str = "Windows") -> JMeterExecutor:
    """Build an executor without filesystem probing for the JMeter binary."""
    ex = object.__new__(JMeterExecutor)
    ex.system = system
    ex.jmeter_path = "jmeter.bat" if system == "Windows" else "jmeter"
    return ex


@pytest.fixture
def fast_sleep(monkeypatch):
    """Collapse polling sleeps so completion-wait tests run in milliseconds.

    Safe because tests run serially; the original is restored on teardown.
    """
    real_sleep = asyncio.sleep

    async def _fake(delay):
        await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", _fake)
    return real_sleep


# ── Singleton ─────────────────────────────────────────────────


class TestGetExecutor:
    def test_lazy_singleton(self, monkeypatch):
        created = []

        class FakeExec:
            pass

        monkeypatch.setattr(executor_mod, "_executor_instance", None)
        monkeypatch.setattr(executor_mod, "JMeterExecutor", FakeExec)

        assert executor_mod._executor_instance is None  # lazy: not built on import
        e1 = get_executor()
        e2 = get_executor()
        assert e1 is e2
        assert isinstance(e1, FakeExec)

    def test_reset_allows_rebuild(self, monkeypatch):
        class FakeExec:
            pass

        monkeypatch.setattr(executor_mod, "_executor_instance", None)
        monkeypatch.setattr(executor_mod, "JMeterExecutor", FakeExec)
        e1 = get_executor()
        monkeypatch.setattr(executor_mod, "_executor_instance", None)
        e2 = get_executor()
        assert e1 is not e2


# ── Process liveness helpers ──────────────────────────────────


class TestPidHelpers:
    def test_is_pid_running_none(self):
        assert _is_pid_running(None) is False

    def test_is_pid_running_current_process(self):
        assert _is_pid_running(os.getpid()) is True

    def test_is_pid_running_dead_pid(self):
        # 2**22 is beyond any realistic live PID on Windows/Linux
        assert _is_pid_running(2 ** 22) is False

    def test_check_process_exited_ok_none(self):
        assert _check_process_exited_ok(None) is None


# ── Command building ──────────────────────────────────────────


class TestBuildCommand:
    def test_command_structure(self, tmp_path):
        ex = make_executor()
        report_dir = str(tmp_path / "report")
        cmd, jtl_path, log_path, html_dir = ex._build_command("plan.jmx", report_dir)

        assert cmd[0] == "jmeter.bat"
        assert "-n" in cmd
        assert cmd[cmd.index("-t") + 1] == "plan.jmx"
        assert cmd[cmd.index("-l") + 1] == jtl_path
        assert cmd[cmd.index("-j") + 1] == log_path
        assert cmd[cmd.index("-o") + 1] == html_dir
        assert jtl_path.endswith("result.jtl")
        # Directories are created eagerly so the JMeter process never fails on mkdir
        assert os.path.isdir(report_dir)
        assert os.path.isdir(html_dir)

    def test_response_data_on_error_enabled(self, tmp_path):
        # SelfHeal diagnosis depends on these JMeter properties being set
        ex = make_executor()
        cmd, *_ = ex._build_command("plan.jmx", str(tmp_path / "r"))
        joined = " ".join(cmd)
        assert "jmeter.save.saveservice.response_data_on_error=true" in joined


# ── Flag writing ──────────────────────────────────────────────


class TestWriteFlag:
    def test_completed_when_jtl_has_data(self, tmp_path):
        flag = str(tmp_path / "_done.flag")
        jtl = tmp_path / "result.jtl"
        jtl.write_text("x" * 100)
        JMeterExecutor._write_flag_from_python(flag, str(jtl), process_ok=None)
        assert open(flag).read() == "COMPLETED"

    def test_failed_when_jtl_missing(self, tmp_path):
        flag = str(tmp_path / "_done.flag")
        JMeterExecutor._write_flag_from_python(
            flag, str(tmp_path / "nope.jtl"), process_ok=None
        )
        assert open(flag).read() == "FAILED"

    def test_failed_when_process_errored_even_with_jtl(self, tmp_path):
        flag = str(tmp_path / "_done.flag")
        jtl = tmp_path / "result.jtl"
        jtl.write_text("x" * 100)
        JMeterExecutor._write_flag_from_python(flag, str(jtl), process_ok=False)
        assert open(flag).read() == "FAILED"


# ── Log error extraction ──────────────────────────────────────


class TestReadLastLogErrors:
    def test_missing_file_returns_none(self, tmp_path):
        ex = make_executor()
        assert ex._read_last_log_errors(str(tmp_path / "nope.log")) is None

    def test_no_errors_returns_unknown(self, tmp_path):
        log = tmp_path / "jmeter.log"
        log.write_text("INFO all good\nINFO done\n")
        ex = make_executor()
        assert ex._read_last_log_errors(str(log)) == "Unknown error (check logs)"

    def test_extracts_error_lines(self, tmp_path):
        log = tmp_path / "jmeter.log"
        log.write_text("INFO start\nERROR connection refused\nFATAL boom\n")
        ex = make_executor()
        result = ex._read_last_log_errors(str(log))
        assert "connection refused" in result
        assert "boom" in result


# ── execute() input validation ────────────────────────────────


class TestExecute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize('execution_id', ['../escape', 'bad&id', 'CON', 'name.', 'a/b', 'a\\b'])
    async def test_unsafe_execution_ids_are_rejected_before_filesystem_effects(self, tmp_path, execution_id, monkeypatch):
        from unittest.mock import AsyncMock
        ex = make_executor()
        ex._execute_windows_terminal = AsyncMock(return_value=None)
        ex._wait_for_completion = AsyncMock()
        ex._ws_broadcast = AsyncMock()
        jmx = tmp_path / 'plan.jmx'
        jmx.write_text('<jmeterTestPlan/>')
        with pytest.raises(ValueError):
            await ex.execute(str(jmx), execution_id)

    @pytest.mark.asyncio
    async def test_missing_jmx_raises(self, tmp_path):
        ex = make_executor()
        with pytest.raises(FileNotFoundError):
            await ex.execute(str(tmp_path / "nope.jmx"), "exec-x")


class TestSchedulerReadiness:
    def test_readiness_does_not_require_executor(self):
        from types import SimpleNamespace
        from app.services.jmeter.test_scheduler import TestScheduler, SchedulerDecision
        from app.services.jmeter.resource_monitor import ResourceSnapshot

        monitor = SimpleNamespace(snapshot=lambda: ResourceSnapshot(1, 1, 1, 10, 1),
                                  get_history=lambda n: [])
        scheduler = TestScheduler(monitor=monitor)
        assert scheduler.check_readiness().decision == SchedulerDecision.PROCEED
        assert scheduler._executor is None
        assert scheduler._active_test_count == 0

    def test_explicit_zero_disables_cooldown_and_wait(self):
        from app.services.jmeter.test_scheduler import TestScheduler

        scheduler = TestScheduler(cooldown_seconds=0, max_wait_seconds=0)
        assert scheduler.cooldown_seconds == 0
        assert scheduler.max_wait_seconds == 0


# ── Completion waiting (B8/B9 regression coverage) ────────────


class TestWaitForCompletion:
    @pytest.mark.asyncio
    async def test_existing_flag_returns_immediately(self, tmp_path, fast_sleep):
        flag = tmp_path / "_done.flag"
        flag.write_text("COMPLETED")
        ex = make_executor()
        # Flag is checked on the first poll iteration — no JTL needed
        await ex._wait_for_completion(str(flag), timeout=60)

    @pytest.mark.asyncio
    async def test_jtl_stability_writes_flag(self, tmp_path, fast_sleep):
        jtl = tmp_path / "result.jtl"
        jtl.write_text("x" * 50)
        flag = str(tmp_path / "_done.flag")
        ex = make_executor()

        await ex._wait_for_completion(flag, timeout=60, pid=None, jtl_path=str(jtl))

        assert os.path.exists(flag)
        assert open(flag).read() == "COMPLETED"

    @pytest.mark.asyncio
    async def test_waits_while_pid_alive_despite_stable_jtl(self, tmp_path, fast_sleep):
        """Regression for B8/B9: stable JTL + live process must NOT finish.

        JMeter keeps running after the last sample to build the HTML report;
        declaring done on JTL stability alone orphans the process.
        """
        jtl = tmp_path / "result.jtl"
        jtl.write_text("x" * 50)
        flag = str(tmp_path / "_done.flag")
        ex = make_executor()

        # Process stays alive for the first stability check, then exits.
        checks = {"n": 0}

        def fake_alive(pid):
            checks["n"] += 1
            return checks["n"] <= 1

        monkey = pytest.MonkeyPatch()
        monkey.setattr(executor_mod, "_is_pid_running", fake_alive)
        try:
            await ex._wait_for_completion(flag, timeout=120, pid=1234, jtl_path=str(jtl))
        finally:
            monkey.undo()

        # The first stability hit found the process alive and kept waiting;
        # only after it exited was the flag written.
        assert checks["n"] >= 2
        assert open(flag).read() == "COMPLETED"

    @pytest.mark.asyncio
    async def test_timeout_raises(self, tmp_path, fast_sleep):
        ex = make_executor()
        with pytest.raises(asyncio.TimeoutError):
            await ex._wait_for_completion(
                str(tmp_path / "_done.flag"), timeout=2,
                pid=None, jtl_path=str(tmp_path / "nope.jtl"),
            )


# ── cancel() and the shared process table ─────────────────────


class TestCancel:
    def test_windows_kills_tree_in_one_call_before_discarding_owner(self, monkeypatch):
        from unittest.mock import Mock
        ex = make_executor()
        run = Mock(return_value=type('Result', (), {'returncode': 0})())
        monkeypatch.setattr(executor_mod.subprocess, 'run', run)
        ex._running_processes['tree-kill'] = {'pid': 2 ** 22}
        ex._kill_process('tree-kill')
        assert run.call_count == 1
        assert '/T' in run.call_args.args[0]

    @pytest.mark.asyncio
    async def test_failed_kill_keeps_process_ownership_and_does_not_claim_cancelled(self, monkeypatch):
        ex = make_executor()
        monkeypatch.setattr(executor_mod.subprocess, 'run', lambda *a, **k: type('R', (), {'returncode': 5})())
        monkeypatch.setattr(executor_mod, '_is_pid_running', lambda pid: True)
        ex._running_processes['failed-kill'] = {'pid': 2 ** 22}
        try:
            assert await ex.cancel('failed-kill') is False
            assert 'failed-kill' in ex._running_processes
        finally:
            ex._running_processes.pop('failed-kill', None)

    @pytest.mark.asyncio
    async def test_cancel_unknown_id(self):
        ex = make_executor()
        assert await ex.cancel("no-such-execution") is False

    @pytest.mark.asyncio
    async def test_cancel_kills_registered_process(self, monkeypatch):
        """Regression for B9: cancel must reach the shared process table
        regardless of which executor instance started the test."""
        killed = []

        def fake_run(cmd, **kwargs):
            killed.append(cmd)

            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(executor_mod.subprocess, "run", fake_run)

        starter = make_executor()
        canceller = make_executor()  # different instance, shared class table
        starter._running_processes["exec-1"] = {
            "pid": 2 ** 22, "report_dir": "/tmp/x", "flag_file": "/tmp/x/_done.flag",
        }
        try:
            assert await canceller.cancel("exec-1") is True
            assert "exec-1" not in starter._running_processes
            assert any("taskkill" in str(c) for c in killed)
        finally:
            JMeterExecutor._running_processes.pop("exec-1", None)


# ── JUnit XML export ──────────────────────────────────────────


class TestExportJunitXml:
    @pytest.mark.asyncio
    async def test_export_from_sample_jtl(self, tmp_path):
        sample = os.path.join(os.path.dirname(__file__), "fixtures", "sample.jtl")
        if not os.path.exists(sample):
            pytest.skip("sample.jtl fixture missing")

        out = str(tmp_path / "junit" / "report.xml")
        ex = make_executor()
        result_path = await ex.export_junit_xml(sample, out, test_name="Sample")

        assert result_path == out
        tree = ET.parse(out)
        root = tree.getroot()
        assert root.tag == "testsuite"
        assert root.get("name") == "Sample"
        assert int(root.get("tests")) > 0
        cases = root.findall("testcase")
        assert len(cases) >= 1
        assert all(c.get("name") for c in cases)


@pytest.mark.asyncio
async def test_tracked_launcher_uses_argument_list_and_registers_before_yield(tmp_path, monkeypatch):
    from unittest.mock import Mock
    ex = make_executor('Linux')
    process = Mock(pid=987654)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(executor_mod.subprocess, 'Popen', spawn)
    flag = str(tmp_path / '_done.flag')
    try:
        pid = await ex._execute_unix_terminal(['jmeter', '-t', 'plan $(unsafe).jmx'], 'owned', flag)
        assert pid == process.pid
        assert ex._running_processes['owned']['process'] is process
        args, kwargs = spawn.call_args
        assert args[0] == ['jmeter', '-t', 'plan $(unsafe).jmx']
        assert kwargs['shell'] is False
        assert kwargs['start_new_session'] is True
    finally:
        info = ex._running_processes.pop('owned', {})
        if info.get('console'):
            info['console'].close()


@pytest.mark.asyncio
async def test_tracked_completion_uses_real_exit_code_without_stability_delay(tmp_path, monkeypatch):
    from unittest.mock import Mock
    ex = make_executor('Linux')
    jtl = tmp_path / 'result.jtl'
    jtl.write_text('data')
    flag = str(tmp_path / '_done.flag')
    process = Mock()
    process.wait.return_value = 3
    ex._running_processes['tracked'] = {'pid': 987654, 'process': process, 'flag_file': flag}
    try:
        await asyncio.wait_for(ex._wait_for_completion(flag, 10, pid=987654, jtl_path=str(jtl)), timeout=1)
        assert (tmp_path / '_done.flag').read_text() == 'FAILED'
    finally:
        ex._running_processes.pop('tracked', None)


@pytest.mark.asyncio
async def test_distributed_uses_shared_executor_guard_and_lifecycle(tmp_path):
    from unittest.mock import AsyncMock
    from app.services.jmeter.distributed_executor import DistributedExecutor
    runner = AsyncMock()
    distributed = DistributedExecutor(runner)
    await distributed.execute('plan.jmx', 'distributed-id', ['engine:1099'])
    runner.execute.assert_awaited_once_with('plan.jmx', 'distributed-id', progress_callback=None,
                                           remote_engines=['engine:1099'])


def test_container_launcher_has_bounded_permissions_and_no_backend_mount(tmp_path, monkeypatch):
    from unittest.mock import Mock
    ex = make_executor('Linux')
    plans = tmp_path / 'plans'; plans.mkdir()
    monkeypatch.setattr(executor_mod.settings, 'TEST_PLANS_DIR_DEFAULT', str(plans))
    outputs = tmp_path / 'reports'; outputs.mkdir()
    jmx = outputs / 'snapshot.jmx'; jmx.write_text('<jmeterTestPlan/>')
    monkeypatch.setattr(executor_mod.settings, 'JMETER_EXECUTION_MODE', 'container', raising=False)
    monkeypatch.setattr(executor_mod.settings, 'JMETER_CONTAINER_IMAGE', 'test-image@sha256:' + 'a' * 64, raising=False)
    monkeypatch.setattr(executor_mod.shutil, 'which', lambda name: '/mock/docker' if name == 'docker' else None)
    spawn = Mock(return_value=Mock(pid=987654))
    monkeypatch.setattr(executor_mod.subprocess, 'Popen', spawn)
    ex._running_processes['boxed'] = {'working_directory': str(plans), 'report_dir': str(outputs)}
    command, _, _, _ = ex._build_command(str(jmx), str(outputs))
    try:
        ex._start_process(command, 'boxed', str(outputs / '_done.flag'))
        argv = spawn.call_args.args[0]
        assert argv[:2] == ['/mock/docker', 'run']
        assert '--read-only' in argv and '--cap-drop=ALL' in argv
        assert '--security-opt=no-new-privileges' in argv and '--pull=never' in argv
        assert argv[argv.index('--user') + 1] != '0'
        mounts = [argv[i + 1] for i, item in enumerate(argv) if item == '--mount']
        assert any('target=/input/test.jmx' in mount and 'readonly' in mount for mount in mounts)
        assert not any('source=' + str(outputs) + ',' in mount for mount in mounts)
        assert not any(str(executor_mod.settings.BASE_DIR) + ',' in mount for mount in mounts)
    finally:
        info = ex._running_processes.pop('boxed', {})
        if info.get('console'):
            info['console'].close()


@pytest.mark.asyncio
async def test_cancel_container_removes_container_before_discarding_owner(monkeypatch):
    from unittest.mock import Mock
    ex = make_executor('Linux')
    run = Mock(return_value=type('Result', (), {'returncode': 0})())
    monkeypatch.setattr(executor_mod.subprocess, 'run', run)
    ex._running_processes['boxed-cancel'] = {'container_name': 'jmeter-agent-boxed-cancel', 'docker_path': '/mock/docker'}
    try:
        assert await ex.cancel('boxed-cancel')
        run.assert_called_once()
        assert run.call_args.args[0] == ['/mock/docker', 'rm', '-f', 'jmeter-agent-boxed-cancel']
    finally:
        ex._running_processes.pop('boxed-cancel', None)


def test_jmeter_child_environment_does_not_inherit_backend_secrets(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-not-a-real-key')
    monkeypatch.setenv('JWT_SECRET', 'test-not-a-real-secret')
    assert 'OPENAI_API_KEY' not in JMeterExecutor._process_environment()
    assert 'JWT_SECRET' not in JMeterExecutor._process_environment()


def test_tracked_windows_java_keeps_heap_defaults_and_explicit_jvm_args(tmp_path, monkeypatch):
    from unittest.mock import Mock
    ex = make_executor('Windows')
    (tmp_path / 'ApacheJMeter.jar').write_bytes(b'not-executed')
    monkeypatch.setenv('JVM_ARGS', r'-Ddata=C:\datasets -Xmx2g')
    monkeypatch.delenv('HEAP', raising=False)
    monkeypatch.delenv('JMETER_COMPLETE_ARGS', raising=False)
    monkeypatch.setattr(executor_mod.shutil, 'which', lambda name: 'java.exe' if name == 'java' else None)
    spawn = Mock(return_value=Mock(pid=987654))
    monkeypatch.setattr(executor_mod.subprocess, 'Popen', spawn)
    try:
        ex._start_process([str(tmp_path / 'jmeter.bat'), '-n'], 'windows-options', str(tmp_path / '_done.flag'))
        argv = spawn.call_args.args[0]
        assert '-Xms1g' in argv and '-XX:+UseG1GC' in argv
        assert r'-Ddata=C:\datasets' in argv and '-Xmx2g' in argv
        assert argv.index('-Xmx2g') > argv.index('-Xmx1g')
    finally:
        info = ex._running_processes.pop('windows-options', {})
        if info.get('console'):
            info['console'].close()


@pytest.mark.asyncio
async def test_container_cleanup_failure_retains_owner_even_after_cli_exit(monkeypatch):
    ex = make_executor('Linux')
    monkeypatch.setattr(executor_mod.subprocess, 'run', lambda *a, **k: type('R', (), {'returncode': 1})())
    ex._running_processes['docker-disconnected'] = {'container_name': 'jmeter-agent-owned', 'docker_path': 'docker'}
    try:
        assert not await ex.cancel('docker-disconnected')
        assert ex._running_processes['docker-disconnected']['cleanup_failed']
    finally:
        ex._running_processes.pop('docker-disconnected', None)


@pytest.mark.asyncio
async def test_container_already_removed_requires_successful_daemon_absence_check(monkeypatch):
    from unittest.mock import Mock
    ex = make_executor('Linux')
    run = Mock(side_effect=[type('R', (), {'returncode': 1})(),
                            type('R', (), {'returncode': 0, 'stdout': b''})()])
    monkeypatch.setattr(executor_mod.subprocess, 'run', run)
    ex._running_processes['auto-removed'] = {'container_name': 'jmeter-agent-removed', 'docker_path': 'docker'}
    try:
        assert await ex.cancel('auto-removed')
        assert run.call_count == 2
        assert run.call_args.args[0][:3] == ['docker', 'container', 'ls']
    finally:
        ex._running_processes.pop('auto-removed', None)


def test_log_tail_is_bounded_even_for_a_single_large_line(tmp_path):
    from app.services.jmeter.report_parser import ReportParser
    path = tmp_path / 'large.log'; path.write_text('x' * 100000 + '\nERROR final failure\n')
    assert len(ReportParser().read_log(str(path))) <= 65536
    assert make_executor()._read_last_log_errors(str(path)) == 'ERROR final failure'
