"""Project logging facade: module loggers, context and shared output sinks."""
import atexit
import itertools
import json
import logging
import sys
import threading

from loongspears.utils.log import ColorFormatter

from config import env

from .context import log_context
from .files import FileLogConfig
from .files import FileLogHandler
from .files import FileLogWriter

_lock = threading.RLock()
_sequence = itertools.count(1)
_handlers = []
_writer = None
_loggers = set()
_FORMAT = (
    '%(asctime)s %(pathname)s %(lineno)d %(levelname)s: '
    'seq=%(log_sequence)s task_id=%(task_id)s %(message)s'
)


class ContextFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'log_sequence'):
            context = log_context()
            record.log_sequence = next(_sequence)
            record.log_user_id = context.user_id
            for name in ('user_id', 'session_id', 'request_uuid', 'task_id'):
                value = getattr(context, name)
                setattr(record, name, json.dumps(value, ensure_ascii=False) if value is not None else '-')
        return True


def _initialize():
    global _writer
    if _handlers:
        return
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColorFormatter(_FORMAT))
    console.addFilter(ContextFilter())
    _handlers.append(console)
    if env.LOG_FILE_ENABLED:
        _writer = FileLogWriter(FileLogConfig(
            directory=env.LOG_DIR, max_bytes=env.LOG_MAX_BYTES,
            retention_days=env.LOG_RETENTION_DAYS, backup_count=env.LOG_BACKUP_COUNT,
            user_max_open_files=env.LOG_USER_MAX_OPEN_FILES, queue_capacity=env.LOG_QUEUE_CAPACITY,
        ))
        files = FileLogHandler(_writer)
        files.setFormatter(logging.Formatter(_FORMAT))
        files.addFilter(ContextFilter())
        _handlers.append(files)


def getLogger(name):
    with _lock:
        _initialize()
        logger = logging.getLogger(name)
        if logger not in _loggers:
            logger.setLevel(env.LOG_LEVEL)
            logger.propagate = False
            for handler in _handlers:
                logger.addHandler(handler)
            _loggers.add(logger)
        return logger


def configure_tornado_logging():
    getLogger('tornado')


def shutdown_logging():
    global _writer
    with _lock:
        if _writer is not None:
            _writer.close()
            _writer = None
        for logger in _loggers:
            for handler in _handlers:
                logger.removeHandler(handler)
        for handler in _handlers:
            handler.close()
        _handlers.clear()
        _loggers.clear()


atexit.register(shutdown_logging)
global_logger = getLogger('global')
__all__ = ['getLogger', 'global_logger', 'configure_tornado_logging', 'shutdown_logging']
