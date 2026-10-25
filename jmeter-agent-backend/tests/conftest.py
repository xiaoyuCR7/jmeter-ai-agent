"""Point every place the app writes data at a throwaway directory for the whole test run.

Without this, a test that reached the database or a data folder wrote into the user's real
jmeter_agent.db, test_plans/, reports/ or model keys. This module is imported by pytest
before any test module, so the settings below are in place before the app is imported.
"""
import os
import shutil
import tempfile
from pathlib import Path

_DATA = Path(tempfile.mkdtemp(prefix='jmeter-agent-tests-'))

os.environ.update({
    'DATABASE_URL': f'sqlite+aiosqlite:///{(_DATA / "jmeter_agent.db").as_posix()}',
    # BASE_DIR roots facts.db, hypotheses.db, conversation memory, RAG data and test_data.
    'BASE_DIR': str(_DATA),
    'TEST_PLANS_DIR_DEFAULT': str(_DATA / 'test_plans'),
    'REPORTS_DIR_DEFAULT': str(_DATA / 'reports'),
    'AI_MODELS_CONFIG_FILE': str(_DATA / 'ai_models_config.json'),
    'MEMORY_DB_PATH': str(_DATA / 'conversation_memory.db'),
    'RAG_LANCEDB_PATH': str(_DATA / 'rag_lancedb'),
})

from app.core import config  # noqa: E402  (must follow the environment above)

# A user's paths_config.json would otherwise override the plan and report folders.
config.PATHS_CONFIG_FILE = str(_DATA / 'paths_config.json')


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA, ignore_errors=True)
