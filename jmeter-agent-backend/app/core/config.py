from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from typing import Optional, List, Dict, Any, Literal
import os
import json
import logging

logger = logging.getLogger(__name__)

# 路径配置文件
PATHS_CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "paths_config.json")


_paths_cache: Optional[tuple] = None  # (file, mtime_ns, size, config)


def load_paths_config() -> Dict[str, str]:
    """加载用户自定义的路径配置

    settings.TEST_PLANS_DIR and REPORTS_DIR ask on every access, several times per request. The file is
    parsed again only when it changed on disk; a stat is all an unchanged file costs.
    """
    global _paths_cache
    try:
        stat = os.stat(PATHS_CONFIG_FILE)
    except OSError:
        _paths_cache = None
        return {}
    key = (PATHS_CONFIG_FILE, stat.st_mtime_ns, stat.st_size)
    if _paths_cache is not None and _paths_cache[:3] == key:
        return dict(_paths_cache[3])
    try:
        with open(PATHS_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError('expected a JSON object')
    except Exception as error:
        # Not silent: the app would quietly fall back to default folders and the user's choice is lost.
        logger.warning("Ignoring unreadable %s: %s", PATHS_CONFIG_FILE, error)
        config = {}
    _paths_cache = (*key, config)
    return dict(config)


def save_paths_config(config: Dict[str, str]):
    """保存用户自定义的路径配置"""
    global _paths_cache
    with open(PATHS_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    _paths_cache = None  # Two saves within one clock tick would otherwise look unchanged.


class Settings(BaseSettings):
    APP_NAME: str = "JMeter Agent"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False  # True restarts the backend on every source change, which ends the runs in flight.
    
    # Server
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    ALLOWED_HOSTS: List[str] = ["localhost", "127.0.0.1", "::1"]

    # Database
    # A relative SQLite path is taken from the backend folder, wherever the backend was started from.
    DATABASE_URL: str = "sqlite+aiosqlite:///./jmeter_agent.db"

    @field_validator('DATABASE_URL')
    @classmethod
    def _database_in_backend_folder(cls, url: str) -> str:
        # Taken from the working directory, a start from another folder silently opened a new, empty database.
        for prefix in ('sqlite+aiosqlite:///', 'sqlite:///'):
            if url.startswith(prefix):
                path, mark, query = url[len(prefix):].partition('?')
                if path and not path.startswith(':') and not os.path.isabs(path) and not path.startswith('/'):
                    backend = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                    path = os.path.normpath(os.path.join(backend, path)).replace(os.sep, '/')
                    return prefix + path + mark + query
        return url

    # Browser origins allowed to call the local API
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://127.0.0.1:5173",
                               "http://localhost:3000", "http://127.0.0.1:3000"]

    # Paths
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    JMETER_HOME: str = os.path.join(os.path.dirname(BASE_DIR), "bin")
    JMETER_EXECUTION_MODE: Literal['local', 'container'] = 'local'
    JMETER_CONTAINER_IMAGE: str = ''  # Operator-provided, locally available image@sha256:digest.
    JMETER_CONTAINER_USER: str = '65534:65534'
    JMETER_CONTAINER_MEMORY_MB: int = 2048
    JMETER_CONTAINER_CPUS: float = 2.0
    JMETER_CONTAINER_NETWORK: str = ''  # Docker network for runs; empty uses Docker's default bridge. Never 'host'.
    
    # Default paths (can be overridden by user config)
    TEST_PLANS_DIR_DEFAULT: str = os.path.join(BASE_DIR, "test_plans")
    REPORTS_DIR_DEFAULT: str = os.path.join(BASE_DIR, "reports")
    BACKUPS_DIR: str = os.path.join(os.path.dirname(BASE_DIR), "backups")
    AI_MODELS_CONFIG_FILE: str = os.path.join(BASE_DIR, "ai_models_config.json")
    TEST_DATA_MAX_UPLOAD_BYTES: int = Field(default=100 * 1024 * 1024, gt=0)
    
    @property
    def TEST_PLANS_DIR(self) -> str:
        """获取测试计划目录（优先使用用户配置）"""
        paths_config = load_paths_config()
        return paths_config.get("test_plans_dir", self.TEST_PLANS_DIR_DEFAULT)
    
    @property
    def REPORTS_DIR(self) -> str:
        """获取报告目录（优先使用用户配置）"""
        paths_config = load_paths_config()
        return paths_config.get("reports_dir", self.REPORTS_DIR_DEFAULT)
    
    # AI models are configured in the UI (ai_models_config.json). This names the one used when a
    # request does not choose; empty or unknown falls back to the first enabled model.
    DEFAULT_AI_MODEL: str = ""

    # JMeter
    # Stops a run that never ends. A plan with a fixed duration gets that duration plus time for its report.
    JMETER_TIMEOUT: int = 3600  # seconds
    # Run guard: stop a run whose recent requests nearly all fail (see services/jmeter/run_guard.py)
    RUN_GUARD_ENABLED: bool = True
    RUN_GUARD_ERROR_RATE: float = 90.0           # % of failed requests in the window
    RUN_GUARD_WINDOW_SECONDS: float = 30.0
    RUN_GUARD_MIN_SAMPLES: int = 50              # fewer requests than this in the window say nothing
    RUN_GUARD_GRACE_SECONDS: float = 30.0        # from the first sample; warm-up errors are common
    RUN_GUARD_MEMORY_PERCENT: float = 97.0       # stop when this machine's memory is this full; 0 = off
    # Stop when responses take this many times longer than at the start of the run; 0 = off. Off by
    # default: stress and capacity tests are meant to slow the target down.
    RUN_GUARD_LATENCY_FACTOR: float = 0.0
    REPORT_LANGUAGE: str = ""  # zh-CN or en for reports written after a run; empty follows I18N_DEFAULT_LANG
    AUTO_ANALYSIS_ENABLED: bool = True  # Off to spare model calls on batches; facts are recorded either way
    JMETER_MAX_OUTPUT_MB: int = 10240  # stop a run whose report directory grows past this; 0 = no limit
    # Remove the HTML dashboard (about 3 MB each) of runs that ended more than this many days ago; 0 keeps
    # everything. The summary, AI report, logs and history stay. See services/report_retention.py.
    REPORTS_RETENTION_DAYS: int = Field(default=0, ge=0)
    REPORTS_RETENTION_INCLUDE_JTL: bool = False  # Also remove those runs' raw result.jtl

    # Resource monitoring thresholds (CPU/memory usage %)
    # Tests will wait or be deferred when usage exceeds these thresholds
    RESOURCE_CPU_THRESHOLD: float = 80.0       # CPU usage % — above this, tests wait
    RESOURCE_MEMORY_THRESHOLD: float = 85.0    # Memory usage % — above this, tests wait
    RESOURCE_COOLDOWN_SECONDS: float = 10.0    # Minimum gap between consecutive tests
    RESOURCE_MAX_WAIT_SECONDS: float = 300.0   # Max time to wait for resources to free up
    # JMeter runs at the same time. One: runs on the same machine skew each other's numbers, so a
    # second run waits its turn. Raise it only when the runs must overlap and the machine has room.
    RESOURCE_MAX_CONCURRENT_TESTS: int = Field(default=1, ge=1)

    # Runs kept whole in memory (summary, AI report); the rest are read back from the database when opened.
    EXECUTION_CACHE_SIZE: int = Field(default=256, ge=1)

    # Agent
    AGENT_TOOL_TIMEOUT_SECONDS: float = 600.0    # max seconds per tool call (10 min)
    AGENT_MAX_CONCURRENT: int = 3                # max concurrent agent executions
    AGENT_CONTEXT_TOKEN_BUDGET: int = 16384      # fallback for models whose context window is unknown
    AGENT_MAX_TOOL_CALLS_PER_RESPONSE: int = 8

    # i18n
    # zh-CN or en: the language of messages when no request says otherwise (an MCP client, a scheduled job).
    I18N_DEFAULT_LANG: str = "zh-CN"

    # CI/CD integration
    WEBHOOK_URL: Optional[str] = None            # URL to POST execution lifecycle events to
    WEBHOOK_SECRET: Optional[str] = None         # HMAC-SHA256 key for the X-JMeter-Agent-Signature header
    WEBHOOK_ON_START: bool = True                # test.started
    WEBHOOK_ON_COMPLETE: bool = True             # test.completed
    WEBHOOK_ON_FAILURE: bool = True              # test.failed and test.cancelled
    WEBHOOK_ON_ANALYSIS: bool = True             # analysis.completed (post-run AI analysis)
    WEBHOOK_INCLUDE_ANALYSIS: bool = False       # also send the AI's full assessment, not just its conclusion

    # JMeter JVM configuration (local mode; a HEAP, GC_ALGO or JVM_ARGS environment variable wins).
    # Container mode uses the image's own defaults inside JMETER_CONTAINER_MEMORY_MB.
    JMETER_HEAP_MIN: str = "256m"                # -Xms initial heap
    JMETER_HEAP_MAX: str = "2g"                  # -Xmx max heap
    JMETER_GC_OPTS: str = "-XX:+UseG1GC -XX:MaxGCPauseMillis=100 -XX:G1ReservePercent=20"  # GC options
    JMETER_EXTRA_OPTS: str = ""                  # Additional JVM options, placed before JVM_ARGS

    @field_validator('JMETER_HEAP_MIN', 'JMETER_HEAP_MAX')
    @classmethod
    def _heap_size(cls, value: str) -> str:
        import re
        if not re.fullmatch(r'[1-9][0-9]*[kKmMgG]', value.strip()):
            raise ValueError('heap sizes look like 512m or 2g')
        return value.strip()

    # Logging: the console and a rotating file (default BASE_DIR/logs/jmeter-agent.log; "off" for none).
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = ""
    # Distributed testing — remote engine hosts
    JMETER_REMOTE_ENGINES: str = ""        # Comma-separated host:port list, e.g. "slave1:1099,slave2:1099"
    # RMI over SSL needs rmi_keystore.jks on every engine; off passes server.rmi.ssl.disable=true.
    JMETER_REMOTE_SSL_ENABLED: bool = False
    JMETER_DISTRIBUTED_MAX_ENGINES: int = 4 # Max concurrent remote engines
    
    model_config = SettingsConfigDict(env_file=".env", extra="allow")


settings = Settings()

# Ensure directories exist
os.makedirs(settings.TEST_PLANS_DIR, exist_ok=True)
os.makedirs(settings.REPORTS_DIR, exist_ok=True)


def validate_deployment_security():
    """There is no authentication, so the API must never listen beyond loopback."""
    if settings.HOST not in ('127.0.0.1', 'localhost', '::1'):
        raise RuntimeError('No-login local mode: HOST must be a loopback address (127.0.0.1 / ::1)')
    if "*" in settings.CORS_ORIGINS:
        raise RuntimeError("CORS_ORIGINS must list trusted origins, not '*'")


def safe_path(user_path: str, allowed_dirs: list[str] = None) -> str:
    """Resolve and validate a path is within allowed directories. Prevents path traversal.

    Args:
        user_path: The path to validate (relative or absolute).
        allowed_dirs: List of allowed base directories. Defaults to TEST_PLANS_DIR + REPORTS_DIR.

    Returns:
        Resolved absolute path if safe.

    Raises:
        ValueError: If path escapes allowed directories.
    """
    if allowed_dirs is None:
        allowed_dirs = [
            settings.TEST_PLANS_DIR,
            settings.REPORTS_DIR,
        ]
    resolved = os.path.realpath(os.path.abspath(user_path))
    for base in allowed_dirs:
        base_resolved = os.path.realpath(os.path.abspath(base))
        try:
            if os.path.normcase(os.path.commonpath([resolved, base_resolved])) == os.path.normcase(base_resolved):
                return resolved
        except ValueError:
            continue
    raise ValueError(f"Access denied: path '{user_path}' is outside allowed directories")


def load_user_model_configs() -> List[Dict[str, Any]]:
    """从 JSON 文件加载用户自定义模型配置"""
    if os.path.exists(settings.AI_MODELS_CONFIG_FILE):
        try:
            with open(settings.AI_MODELS_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as error:
            logger.warning("Ignoring unreadable %s: %s", settings.AI_MODELS_CONFIG_FILE, error)
    return []


def save_user_model_configs(configs: List[Dict[str, Any]]):
    """保存用户自定义模型配置到 JSON 文件

    The file holds API keys. It is written to a temporary file only its owner can read (mode 0600 on
    POSIX; on Windows the folder's own permissions apply) and swapped in whole, so a crash never
    leaves half a file. A key written as ``env:NAME`` is not in the file at all (see resolve_api_key).
    """
    import tempfile
    path = settings.AI_MODELS_CONFIG_FILE
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path) or '.', prefix='.ai_models_')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


API_KEY_ENV_PREFIX = 'env:'


def resolve_api_key(value: Optional[str]) -> str:
    """The key to send: ``env:OPENAI_API_KEY`` reads that environment variable, so the key need not be on disk."""
    value = (value or '').strip()
    if value.lower().startswith(API_KEY_ENV_PREFIX):
        return os.environ.get(value[len(API_KEY_ENV_PREFIX):].strip(), '')
    return value


def get_all_model_configs() -> List[Dict[str, Any]]:
    """获取所有模型配置（仅用户自定义，本项目不内置模型）。"""
    return load_user_model_configs()


def sanitize_model_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the model config with the API key redacted.

    The key is never sent to the frontend; it is only stored on disk and
    used server-side when building adapters.
    """
    safe = dict(config)
    key = str(safe.get("api_key") or "")
    if key and not key.lower().startswith(API_KEY_ENV_PREFIX):
        safe["api_key"] = "***"  # A reference to an environment variable names no secret, so it is shown.
    return safe
