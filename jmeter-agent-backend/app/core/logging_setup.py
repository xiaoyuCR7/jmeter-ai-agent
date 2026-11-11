"""Where the backend's own log lines go: the console and a rotating file.

Without this nothing below WARNING was ever shown, and nothing was kept: uvicorn configures only its own
loggers. Only the ``app`` logger is configured here, so uvicorn's access lines (whose URLs can carry the
local access token) stay out of the file.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from app.core.config import settings

FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3
_MARK = '_jmeter_agent_handler'


def log_file() -> str:
    """The log file's path, or '' when LOG_FILE is "off"."""
    if settings.LOG_FILE.strip().lower() == 'off':
        return ''
    return settings.LOG_FILE.strip() or os.path.join(settings.BASE_DIR, 'logs', 'jmeter-agent.log')


def configure_logging() -> None:
    """Idempotent: a second call (tests, reload) replaces the handlers this module added."""
    level = getattr(logging, settings.LOG_LEVEL.strip().upper(), None)
    if not isinstance(level, int):
        level = logging.INFO
    logger = logging.getLogger('app')
    for handler in [h for h in logger.handlers if getattr(h, _MARK, False)]:
        logger.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    path = log_file()
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handlers.append(RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding='utf-8'))
        except OSError as error:
            # A read-only folder must not stop the app; the console still gets everything.
            logging.getLogger(__name__).warning('Log file %s unavailable: %s', path, error)
    for handler in handlers:
        handler.setFormatter(logging.Formatter(FORMAT))
        setattr(handler, _MARK, True)
        logger.addHandler(handler)
    logger.setLevel(level)
    # Left propagating: uvicorn gives the root logger no handler, and pytest's caplog listens there.
    logger.propagate = True
