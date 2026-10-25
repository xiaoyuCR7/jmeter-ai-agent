"""Guard for conftest.py: tests must never read or write the user's own data."""
import os
from pathlib import Path

from app.core import config
from app.models import db
from app.services.ai.agent import memory


def test_every_data_location_is_inside_the_throwaway_directory():
    scratch = Path(os.environ['BASE_DIR']).resolve()
    source = Path(__file__).resolve().parents[1]
    assert not scratch.is_relative_to(source)
    locations = [config.settings.TEST_PLANS_DIR, config.settings.REPORTS_DIR, config.settings.AI_MODELS_CONFIG_FILE,
                 config.settings.BASE_DIR, config.PATHS_CONFIG_FILE, memory._get_memory_db_path(),
                 config.settings.DATABASE_URL.split(':///', 1)[1]]
    for location in locations:
        assert Path(location).resolve().is_relative_to(scratch), location


def test_the_database_engine_opens_the_throwaway_database():
    url = str(db.get_engine().url)
    assert Path(url.split(':///', 1)[1]).resolve().is_relative_to(Path(os.environ['BASE_DIR']).resolve())
