import os
import subprocess
import shutil
import uuid
import asyncio
import platform
import signal
import logging
import psutil
from datetime import datetime
from typing import Optional, Callable
from xml.etree import ElementTree as ET

from app.core.config import settings
from app.models.schemas import TestExecutionInfo, TestExecutionStatus

_jmeter_slots: Optional[asyncio.Semaphore] = None


async def _acquire_jmeter_slot() -> asyncio.Semaphore:
    """Concurrent JMeter runs skew each other's numbers; wait for a free slot."""
    global _jmeter_slots
    if _jmeter_slots is None:
        _jmeter_slots = asyncio.Semaphore(settings.RESOURCE_MAX_CONCURRENT_TESTS)
    try:
        await asyncio.wait_for(_jmeter_slots.acquire(), timeout=settings.RESOURCE_MAX_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise RuntimeError(f'JMeter concurrency limit ({settings.RESOURCE_MAX_CONCURRENT_TESTS}) reached') from None
    return _jmeter_slots

# Windows-only Popen flags; resolved lazily so the Windows branches stay importable
# and testable on POSIX, where these constants do not exist.
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)


def _is_pid_running(pid: Optional[int]) -> bool:
    """Check whether a process is still alive (False when pid unknown or gone)."""
    if pid is None:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except Exception:
        return False


def _check_process_exited_ok(pid: Optional[int]) -> Optional[bool]:
    """Check if a process has exited successfully.

    Returns:
        True  — process has exited with code 0
        False — process has exited with non-zero code
        None  — process not found or cannot determine
    """
    if pid is None:
        return None
    try:
        proc = psutil.Process(pid)
        if proc.is_running():
            return None  # still running, indeterminate
        exit_code = proc.wait(timeout=1)  # process already exited, get exit code
        return exit_code == 0
    except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
        # Process gone: psutil can't get exit code. Assume OK if JTL exists.
        return None
    except Exception:
        return None


class JMeterExecutor:
    """JMeter测试执行器 - 支持多终端并行执行

    Use get_executor() to obtain the process-wide shared instance instead
    of constructing JMeterExecutor() directly (see notes on get_executor).
    """

    # Shared across ALL instances (routes, scheduler, agents each construct
    # their own JMeterExecutor) so cancel()/shutdown can reach any process
    # regardless of which instance started it.
    _running_processes: dict = {}

    def __init__(self):
        self.system = platform.system()
        self.jmeter_path = 'jmeter' if settings.JMETER_EXECUTION_MODE == 'container' else self._find_jmeter_executable()
    
    def _find_jmeter_executable(self) -> str:
        """查找JMeter可执行文件"""
        if self.system == "Windows":
            candidates = [
                os.path.join(settings.JMETER_HOME, "jmeter.bat"),
                os.path.join(settings.JMETER_HOME, "jmeter.cmd"),
                os.path.join(os.path.dirname(settings.JMETER_HOME), "bin", "jmeter.bat"),
            ]
        else:
            candidates = [
                os.path.join(settings.JMETER_HOME, "jmeter"),
                os.path.join(settings.JMETER_HOME, "jmeter.sh"),
                os.path.join(os.path.dirname(settings.JMETER_HOME), "bin", "jmeter"),
            ]
        
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        
        # Try PATH
        jmeter_cmd = "jmeter.bat" if self.system == "Windows" else "jmeter"
        if shutil.which(jmeter_cmd):
            return jmeter_cmd
        
        raise FileNotFoundError(f"JMeter executable not found. Checked: {candidates}")
    
    def _build_command(self, jmx_path: str, report_dir: str) -> tuple:
        """构建JMeter命令参数"""
        jtl_path = os.path.join(report_dir, "result.jtl")
        log_path = os.path.join(report_dir, "jmeter.log")
        html_report_dir = os.path.join(report_dir, "html")
        
        os.makedirs(report_dir, exist_ok=True)
        os.makedirs(html_report_dir, exist_ok=True)
        
        cmd = [
            self.jmeter_path,
            "-n",
            "-t", jmx_path,
            "-l", jtl_path,
            "-j", log_path,
            "-e",
            "-o", html_report_dir,
            # Save response body for failed requests — enables SelfHeal diagnosis
            "-Jjmeter.save.saveservice.response_data_on_error=true",
            "-Jjmeter.save.saveservice.assertion_results_failure_message=true",
        ]
        
        return cmd, jtl_path, log_path, html_report_dir
    
    @staticmethod
    def validate_execution_id(execution_id: str):
        import re
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,49}", execution_id)
                or execution_id.endswith('.')
                or execution_id.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                    *{f'COM{i}' for i in range(1, 10)}, *{f'LPT{i}' for i in range(1, 10)}}):
            raise ValueError('Invalid execution ID')

    async def execute(self, jmx_path: str, execution_id: str,
                      progress_callback: Optional[Callable[[int], None]] = None, *,
                      remote_engines: Optional[list[str]] = None,
                      working_directory: Optional[str] = None) -> TestExecutionInfo:
        """Run one owned child process; queued and active work share cancellation."""
        self.validate_execution_id(execution_id)
        if any(entry.get('cleanup_failed') for entry in self._running_processes.values()):
            raise RuntimeError('A previous execution could not be stopped; clean it up before starting another test')
        if execution_id in self._running_processes:
            raise ValueError('Execution ID is already running')
        jmx_path = os.path.abspath(jmx_path)
        if not os.path.isfile(jmx_path):
            raise FileNotFoundError(f'JMX file not found: {jmx_path}')
        if remote_engines:
            import re
            if len(remote_engines) > settings.JMETER_DISTRIBUTED_MAX_ENGINES or any(
                    not re.fullmatch(r'[A-Za-z0-9_.:\[\]-]+', engine) for engine in remote_engines):
                raise ValueError('Invalid remote engine list')

        report_dir = os.path.join(settings.REPORTS_DIR, execution_id)
        flag_file = os.path.join(report_dir, '_done.flag')
        if os.path.exists(flag_file) or os.path.exists(os.path.join(report_dir, 'result.jtl')):
            raise ValueError('Execution output already exists; use a new execution ID')
        cmd, jtl_path, log_path, _ = self._build_command(jmx_path, report_dir)
        if remote_engines:
            cmd += ['-R', ','.join(remote_engines)]
        info = TestExecutionInfo(id=execution_id, test_plan_id='', status=TestExecutionStatus.PENDING,
                                 start_time=datetime.now(), jmx_path=jmx_path, report_dir=report_dir,
                                 log_path=log_path)
        entry = {'pid': None, 'task': asyncio.current_task(), 'report_dir': report_dir,
                 'flag_file': flag_file, 'working_directory': working_directory or os.path.dirname(jmx_path),
                 'cancel_requested': False}
        self._running_processes[execution_id] = entry
        monitor = None
        try:
            slots = await _acquire_jmeter_slot()
            try:
                if any(item.get('cleanup_failed') for item in self._running_processes.values()):
                    raise RuntimeError('Previous execution cleanup is incomplete')
                info.status = TestExecutionStatus.RUNNING
                await self._ws_broadcast(execution_id, 'status', {'status': 'running', 'progress': 0})
                try:
                    launcher = self._execute_windows_terminal if self.system == 'Windows' else self._execute_unix_terminal
                    pid = await launcher(cmd, execution_id, flag_file)
                    entry['pid'] = pid
                    if progress_callback:
                        monitor = asyncio.create_task(self._monitor_progress(execution_id, flag_file, progress_callback))
                    await self._wait_for_completion(flag_file, settings.JMETER_TIMEOUT, pid=pid, jtl_path=jtl_path)
                    flag = ''
                    if os.path.exists(flag_file):
                        with open(flag_file, encoding='ascii') as stream:
                            flag = stream.read().strip()
                    if entry['cancel_requested']:
                        info.status = TestExecutionStatus.CANCELLED
                    elif flag == 'FAILED' or not os.path.exists(jtl_path) or os.path.getsize(jtl_path) == 0:
                        info.status = TestExecutionStatus.FAILED
                        info.error_message = self._read_last_log_errors(log_path) or 'JTL result file is empty or missing'
                    else:
                        info.status = TestExecutionStatus.COMPLETED
                    info.progress = 100
                except asyncio.CancelledError:
                    await asyncio.to_thread(self._kill_process, execution_id)
                    info.status = TestExecutionStatus.CANCELLED
                    raise
                except asyncio.TimeoutError:
                    await asyncio.to_thread(self._kill_process, execution_id)
                    info.status = TestExecutionStatus.FAILED
                    info.error_message = f'Test timed out after {settings.JMETER_TIMEOUT}s'
                except Exception as error:
                    await asyncio.to_thread(self._kill_process, execution_id)
                    info.status = TestExecutionStatus.FAILED
                    info.error_message = str(error)
            finally:
                slots.release()
        except asyncio.CancelledError:
            info.status = TestExecutionStatus.CANCELLED
            raise
        except Exception as error:
            info.status = TestExecutionStatus.FAILED
            info.error_message = str(error)
        finally:
            info.end_time = datetime.now()
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            # Docker CLI exit alone does not prove that its daemon-owned container
            # exited (for example, the client connection may have been lost).
            if entry.get('container_name') and self._running_processes.get(execution_id) is entry:
                await asyncio.to_thread(self._kill_process, execution_id)
            process = entry.get('process')
            # Retain ownership when termination failed, so a later cancel can retry.
            if entry.get('cleanup_failed'):
                info.status = TestExecutionStatus.RUNNING
                info.end_time = None
                info.error_message = 'Termination failed; process/container remains owned and requires cleanup'
            elif process is None or process.poll() is not None:
                self._running_processes.pop(execution_id, None)
                if entry.get('console'):
                    entry['console'].close()
        return info

    def _start_process(self, cmd: list, execution_id: str, flag_file: str) -> int:
        """Register the actual child before the first await; never find it by text matching."""
        command = list(cmd)
        entry = self._running_processes.setdefault(execution_id, {})
        if settings.JMETER_EXECUTION_MODE == 'container':
            command = self._container_command(command, entry)
        elif self.system == 'Windows' and command[0].lower().endswith(('.bat', '.cmd')):
            script = shutil.which(command[0]) or command[0]
            jar = os.path.join(os.path.dirname(os.path.abspath(script)), 'ApacheJMeter.jar')
            java = os.path.join(os.environ.get('JAVA_HOME', ''), 'bin', 'java.exe')
            if not os.path.isfile(java):
                java = shutil.which('java')
            if not java or not os.path.isfile(jar):
                raise FileNotFoundError('Tracked Windows execution requires Java and bin/ApacheJMeter.jar')
            # Running Java directly avoids cmd.exe expansion of user-supplied paths.
            import shlex
            def options(value):
                lexer = shlex.shlex(value, posix=True)
                lexer.whitespace_split = True
                lexer.commenters = ''
                lexer.escape = ''  # Backslashes in Windows paths are data, not shell escapes.
                return list(lexer)
            defaults = []
            if not os.environ.get('JMETER_COMPLETE_ARGS'):
                defaults = ['-XX:+HeapDumpOnOutOfMemoryError', '-Djava.security.egd=file:/dev/urandom']
                defaults += options(os.environ.get('HEAP', '-Xms1g -Xmx1g -XX:MaxMetaspaceSize=256m'))
                defaults += options(os.environ.get('GC_ALGO', '-XX:+UseG1GC -XX:MaxGCPauseMillis=100 -XX:G1ReservePercent=20'))
                for variable in ('JAVA9_OPTS', 'VERBOSE_GC', 'DDRAW', 'JMETER_LANGUAGE', 'RUN_IN_DOCKER'):
                    defaults += options(os.environ.get(variable, ''))
            jvm_args = defaults + options(os.environ.get('JMETER_OPTS', '')) + options(os.environ.get('JVM_ARGS', ''))
            command = [java, *jvm_args, '-jar', jar, *command[1:]]
        console_path = os.path.join(os.path.dirname(flag_file), 'console.log')
        console = open(console_path, 'ab')
        options = dict(shell=False, stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
                       cwd=entry.get('working_directory'), env=self._process_environment())
        if self.system == 'Windows':
            options['creationflags'] = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        else:
            options['start_new_session'] = True
        try:
            process = subprocess.Popen(command, **options)
        except BaseException:
            console.close()
            raise
        entry.update(process=process, pid=process.pid, flag_file=flag_file, console=console,
                     owned_process_group=self.system != 'Windows')
        return process.pid

    @staticmethod
    def _process_environment():
        # Child scripts must not inherit backend model keys, JWT secrets or DB credentials.
        allowed = {'PATH', 'JAVA_HOME', 'JRE_HOME', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
                   'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'LANG', 'LC_ALL', 'TZ',
                   'JVM_ARGS', 'JMETER_OPTS', 'HEAP', 'GC_ALGO', 'JMETER_COMPLETE_ARGS',
                   'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY'}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    def _container_command(self, command, entry):
        import re
        import math
        from pathlib import Path
        docker = shutil.which('docker')
        if '-R' in command:
            raise ValueError('Container mode does not configure remote RMI callbacks; use separately isolated remote engines')
        if not docker:
            raise FileNotFoundError('Container mode requires an available Docker CLI and daemon')
        image = settings.JMETER_CONTAINER_IMAGE
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]*@sha256:[0-9a-f]{64}', image):
            raise ValueError('Container mode requires a locally available image pinned by sha256 digest')
        if not re.fullmatch(r'[1-9][0-9]*(?::[1-9][0-9]*)?', settings.JMETER_CONTAINER_USER):
            raise ValueError('Container user and group must be non-root numeric IDs')
        if settings.JMETER_CONTAINER_MEMORY_MB < 128 or not math.isfinite(settings.JMETER_CONTAINER_CPUS) or settings.JMETER_CONTAINER_CPUS <= 0:
            raise ValueError('Container memory and CPU limits must be positive')
        source = Path(entry['working_directory']).resolve(strict=True)
        if not source.is_relative_to(Path(settings.TEST_PLANS_DIR).resolve()):
            raise ValueError('Container resources must be inside TEST_PLANS_DIR; use relative CSV/script paths')
        name = 'jmeter-agent-' + uuid.uuid4().hex
        argv = [docker, 'run', '--rm', '--pull=never', '--name', name,
                '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--pids-limit=256', '--memory', f'{settings.JMETER_CONTAINER_MEMORY_MB}m',
                '--memory-swap', f'{settings.JMETER_CONTAINER_MEMORY_MB}m',
                '--cpus', str(settings.JMETER_CONTAINER_CPUS), '--user', settings.JMETER_CONTAINER_USER,
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m', '--workdir', '/work']
        def mount(path, target, readonly=False):
            path = str(Path(path).resolve(strict=True))
            if ',' in path:
                raise ValueError('Container bind paths cannot contain commas')
            argv.extend(['--mount', f'type=bind,source={path},target={target}' + (',readonly' if readonly else '')])
        mount(source, '/work', readonly=True)
        remapped = list(command[1:])
        for option, target in [('-t', '/input/test.jmx'), ('-l', '/output/result.jtl'),
                               ('-j', '/output/jmeter.log'), ('-o', '/output/html')]:
            index = remapped.index(option) + 1
            original = remapped[index]
            if option in ('-l', '-j'):
                with open(original, 'xb'):
                    pass
            mount(original, target, readonly=option == '-t')
            remapped[index] = target
        # Do not mount the whole report directory: it contains the immutable snapshot.
        entry.update(container_name=name, docker_path=docker)
        return [*argv, '--entrypoint', 'jmeter', image, *remapped]

    async def _execute_windows_terminal(self, cmd: list, execution_id: str, flag_file: str) -> int:
        return self._start_process(cmd, execution_id, flag_file)

    async def _execute_unix_terminal(self, cmd: list, execution_id: str, flag_file: str) -> int:
        return self._start_process(cmd, execution_id, flag_file)

    async def _wait_for_completion(self, flag_file: str, timeout: int,
                                    pid: Optional[int] = None,
                                    jtl_path: Optional[str] = None) -> None:
        """Wait for JMeter to finish by polling JTL file stability.

        Uses JTL file size stability as the primary detection mechanism:
        - JMeter writes results incrementally to JTL during the test
        - When JMeter exits, the JTL stops growing
        - We detect "done" when JTL size is stable for 3 consecutive checks

        Falls back to PID polling if JTL path is not available.
        Writes _done.flag from Python once completion is detected.
        """
        tracked = next((entry.get('process') for entry in self._running_processes.values()
                        if entry.get('flag_file') == flag_file and entry.get('process') is not None), None)
        if tracked is not None:
            exit_code = await asyncio.wait_for(asyncio.to_thread(tracked.wait), timeout=timeout)
            self._write_flag_from_python(flag_file, jtl_path, exit_code == 0)
            return
        # Compatibility for imported/legacy executions lacking a child handle.
        waited = 0
        last_jtl_size = -1
        stable_count = 0

        while waited < timeout:
            # Primary: check if flag was already written
            if os.path.exists(flag_file):
                return

            # JTL-based detection: wait for file to exist and stabilize
            if jtl_path and os.path.exists(jtl_path):
                current_size = os.path.getsize(jtl_path)
                if current_size > 0 and current_size == last_jtl_size:
                    stable_count += 1
                    if stable_count >= 3:  # 6 seconds of stability
                        if pid is not None and _is_pid_running(pid):
                            # JMeter still alive — after the test it keeps
                            # running to generate the HTML report. Declaring
                            # done now would orphan the process and truncate
                            # the report. Reset and keep waiting.
                            stable_count = 0
                        else:
                            success = _check_process_exited_ok(pid)
                            self._write_flag_from_python(flag_file, jtl_path, success)
                            return
                else:
                    stable_count = 0
                    last_jtl_size = current_size
            else:
                # JTL not yet created — keep waiting
                stable_count = 0

            await asyncio.sleep(2)
            waited += 2

        raise asyncio.TimeoutError()

    @staticmethod
    def _write_flag_from_python(flag_file: str, jtl_path: Optional[str] = None,
                                 process_ok: Optional[bool] = None):
        """Write _done.flag from Python (avoids cmd.exe Unicode path issues).

        Determines COMPLETED vs FAILED based on:
        1. JTL file existence and size (primary)
        2. JMeter process exit status, if available (secondary)
        """
        try:
            has_jtl = jtl_path and os.path.exists(jtl_path) and os.path.getsize(jtl_path) > 0
            # Process check: if we know the process failed, mark FAILED even if JTL exists
            if process_ok is False:
                with open(flag_file, "w", encoding="ascii") as f:
                    f.write("FAILED")
            elif has_jtl:
                with open(flag_file, "w", encoding="ascii") as f:
                    f.write("COMPLETED")
            else:
                with open(flag_file, "w", encoding="ascii") as f:
                    f.write("FAILED")
        except Exception:
            pass  # flag file is best-effort; caller handles JTL checks independently
    
    async def _monitor_progress(self, execution_id: str, flag_file: str,
                                callback: Callable[[int], None]):
        """监控进度"""
        progress = 0
        last_progress = -1
        while not os.path.exists(flag_file):
            await asyncio.sleep(5)
            progress = min(progress + 5, 95)
            if progress == last_progress:
                continue
            last_progress = progress
            try:
                callback(progress)
            except Exception:
                pass
            try:
                await self._ws_broadcast(execution_id, "progress", {
                    "progress": progress,
                })
            except Exception:
                pass
        # Final push so subscribers see 100% immediately
        try:
            callback(100)
        except Exception:
            pass
    
    def _kill_process(self, execution_id: str) -> bool:
        """Terminate the complete owned tree before dropping its registry entry."""
        entry = self._running_processes.get(execution_id)
        if entry is None:
            return False
        pid = entry.get('pid')
        process = entry.get('process')
        try:
            if entry.get('container_name'):
                options = dict(capture_output=True, timeout=15)
                if self.system == 'Windows':
                    options['creationflags'] = _CREATE_NO_WINDOW
                removed = subprocess.run([entry['docker_path'], 'rm', '-f', entry['container_name']], **options)
                if removed.returncode != 0:
                    # --rm may have removed it already. Require a successful daemon
                    # query; neither an error string nor a dead CLI proves absence.
                    remaining = subprocess.run([entry['docker_path'], 'container', 'ls', '--all',
                        '--filter', 'name=^/' + entry['container_name'] + '$', '--format', '{{.Names}}'], **options)
                    if remaining.returncode != 0 or remaining.stdout.strip():
                        entry['cleanup_failed'] = True
                        return False
                entry['cleanup_failed'] = False
            if pid and (process is None or process.poll() is None):
                if self.system == 'Windows':
                    result = subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                                            capture_output=True, timeout=10,
                                            creationflags=_CREATE_NO_WINDOW)
                    if result.returncode != 0 and _is_pid_running(pid):
                        entry['cleanup_failed'] = True
                        return False
                elif entry.get('owned_process_group'):
                    os.killpg(pid, signal.SIGKILL)
                else:
                    parent = psutil.Process(pid)
                    for child in parent.children(recursive=True):
                        child.kill()
                    parent.kill()
                if process is not None:
                    process.wait(timeout=10)
            self._running_processes.pop(execution_id, None)
            if entry.get('console'):
                entry['console'].close()
            return True
        except (ProcessLookupError, psutil.NoSuchProcess):
            self._running_processes.pop(execution_id, None)
            if entry.get('console'):
                entry['console'].close()
            return True
        except Exception:
            entry['cleanup_failed'] = True
            logging.getLogger(__name__).exception('Unable to terminate owned execution %s', execution_id)
            return False

    async def cancel(self, execution_id: str) -> bool:
        entry = self._running_processes.get(execution_id)
        if entry is None:
            return False
        entry['cancel_requested'] = True
        if not await asyncio.to_thread(self._kill_process, execution_id):
            return False
        task = entry.get('task')
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

    def _read_last_log_errors(self, log_path: str) -> Optional[str]:
        """读取日志文件中的最后几条错误信息"""
        if not os.path.exists(log_path):
            return None
        try:
            with open(log_path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 65536))
                lines = f.read(65536).decode('utf-8', errors='replace').splitlines()
                # 找最后包含 ERROR 或 Exception 的行
                error_lines = [l.strip() for l in lines if "ERROR" in l or "Exception" in l or "FATAL" in l]
                if error_lines:
                    return " | ".join(error_lines[-5:])[:500]
                return "Unknown error (check logs)"
        except Exception:
            return "Failed to read log file"


    async def _ws_broadcast(self, execution_id: str, event_type: str, data: dict):
        """Send a WebSocket event for real-time progress."""
        try:
            from app.services.ws_manager import ws_manager
            await ws_manager.broadcast(execution_id, {**data, "event": event_type})
        except Exception:
            pass

    async def export_junit_xml(self, jtl_path: str, output_path: str,
                               test_name: str = "JMeter Test") -> str:
        """Export a JTL result file as JUnit XML for CI/CD integration.

        Args:
            jtl_path: Path to the JTL result file.
            output_path: Output path for the JUnit XML file.
            test_name: Test suite name for the XML.

        Returns:
            Path to the generated JUnit XML file.
        """
        from app.services.jmeter.report_parser import ReportParser
        parser = ReportParser()
        summary = await asyncio.to_thread(parser.parse_jtl, jtl_path)

        # Build JUnit XML
        testsuite = ET.Element("testsuite")
        testsuite.set("name", test_name)
        testsuite.set("tests", str(summary.total_requests))
        failures = int(summary.error_rate / 100 * summary.total_requests)
        testsuite.set("failures", str(failures))
        testsuite.set("errors", "0")
        testsuite.set("time", str(round(summary.avg_response_time / 1000, 2)))
        testsuite.set("timestamp", datetime.now().isoformat())

        # Per-label test cases
        per_label = getattr(summary, "per_label", None) or {}
        for label, stats in per_label.items():
            testcase = ET.SubElement(testsuite, "testcase")
            testcase.set("classname", test_name)
            testcase.set("name", label)
            testcase.set("time", str(round(stats.get("avg_response_time", 0) / 1000, 2)))
            if stats.get("error_rate", 0) > 0:
                failure = ET.SubElement(testcase, "failure")
                failure.set("message", f"Error rate: {stats['error_rate']}%")
                failure.text = f"P95: {stats.get('p95_response_time', 'N/A')}ms, "
                failure.text += f"P99: {stats.get('p99_response_time', 'N/A')}ms"

        # If no per-label data, create one test case
        if not per_label:
            testcase = ET.SubElement(testsuite, "testcase")
            testcase.set("classname", test_name)
            testcase.set("name", "All Requests")
            testcase.set("time", str(round(summary.avg_response_time / 1000, 2)))
            if failures > 0:
                failure = ET.SubElement(testcase, "failure")
                failure.set("message", f"Error rate: {summary.error_rate}%")

        tree = ET.ElementTree(testsuite)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        tree.write(output_path, encoding="utf-8", xml_declaration=True)
        return output_path


_executor_instance: Optional[JMeterExecutor] = None


def get_executor() -> JMeterExecutor:
    """Process-wide lazily-initialized JMeterExecutor singleton.

    Lazy so that importing routes/tools never probes the filesystem for
    the JMeter binary — a missing JMeter only fails actual test execution,
    not application startup. Construction is side-effect-free (read-only
    probing), so a benign double-construct under a race is harmless.
    """
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = JMeterExecutor()
    return _executor_instance
