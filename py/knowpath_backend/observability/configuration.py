"""Production-only handler setup; one rotating JSONL file per process."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
from threading import RLock

from .events import log_event, redact, safe_exception, safe_value

_lock = RLock()
_original = None
_third_party = ('httpx','httpcore','urllib3','openai','sqlalchemy','neo4j','qdrant_client','multipart','python_multipart','pypdf')


@dataclass(frozen=True)
class LogSettings:
    level: str = 'INFO'
    console_format: str = 'text'
    directory: Path | None = Path(__file__).resolve().parents[2] / 'logs'
    max_bytes: int = 10 * 1024 * 1024
    backups: int = 5

    def __post_init__(self):
        if self.level not in {'DEBUG','INFO','WARNING','ERROR','CRITICAL'}:
            raise ValueError('KNOWPATH_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL')
        if self.console_format not in {'text','json'}:
            raise ValueError('KNOWPATH_LOG_FORMAT must be text or json')
        if not 256 <= self.max_bytes <= 1024*1024*1024 or not 1 <= self.backups <= 100:
            raise ValueError('Logging rotation requires 256-1073741824 bytes and 1-100 backups')

    @classmethod
    def from_env(cls):
        directory = os.getenv('KNOWPATH_LOG_DIR')
        return cls(level=os.getenv('KNOWPATH_LOG_LEVEL','INFO').upper(),
                   console_format=os.getenv('KNOWPATH_LOG_FORMAT','text').lower(),
                   directory=cls.directory if directory is None else Path(directory).expanduser() if directory.strip() else None,
                   max_bytes=int(os.getenv('KNOWPATH_LOG_MAX_BYTES',str(cls.max_bytes))),
                   backups=int(os.getenv('KNOWPATH_LOG_BACKUPS',str(cls.backups))))


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service):
        super().__init__()
        self.service = service

    def document(self, record):
        fields = safe_value(getattr(record, 'fields', {}))
        result = {**fields, 'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds'),
                  'level': record.levelname, 'service': self.service, 'pid': record.process,
                  'thread': record.threadName, 'logger': record.name, 'event': getattr(record,'event','legacy.log')}
        if not hasattr(record, 'event'):
            # Uvicorn lifespan errors may already be formatted traceback strings
            # with source lines and exception bodies, without any exc_info.
            dependency = any(record.name == name or record.name.startswith(name+'.') for name in (*_third_party, 'uvicorn'))
            result['message'] = '[dependency message omitted]' if dependency else redact(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            result['exception'] = safe_exception(record.exc_info[1])
        return result

    def format(self, record):
        return json.dumps(self.document(record), ensure_ascii=False, allow_nan=False, separators=(',',':'))


class TextFormatter(JsonFormatter):
    def format(self, record):
        item = self.document(record)
        prefix = f"{item.pop('timestamp')} {item.pop('level'):<8} [{item.pop('service')}:{item.pop('pid')}] {item.pop('event')}"
        return prefix + ' ' + json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(',',':'))


class _NoiseFilter(logging.Filter):
    def filter(self, record):
        return not (record.levelno < logging.WARNING and any(record.name == name or record.name.startswith(name+'.') for name in _third_party))


def _remove_owned():
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler,'_knowpath_owned',False):
            root.removeHandler(handler)
            handler.close()


def configure_logging(service, *, settings=None, stream=None):
    global _original
    settings = settings or LogSettings.from_env()
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', service):
        raise ValueError('Invalid logging service name')
    with _lock:
        root = logging.getLogger()
        names = (*_third_party, 'uvicorn', 'uvicorn.error', 'uvicorn.access')
        if _original is None:
            _original = (root.level, {name: (logging.getLogger(name).level, logging.getLogger(name).disabled,
                logging.getLogger(name).propagate, list(logging.getLogger(name).handlers)) for name in names})
        _remove_owned()
        root.setLevel(settings.level)
        console = logging.StreamHandler(stream if stream is not None else sys.stderr)
        console.setFormatter((JsonFormatter if settings.console_format=='json' else TextFormatter)(service=service))
        console._knowpath_owned = True
        console.addFilter(_NoiseFilter())
        root.addHandler(console)
        if settings.directory is not None:
            try:
                directory = Path(settings.directory)
                directory.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(directory/f'{service}-{os.getpid()}.jsonl', maxBytes=settings.max_bytes,
                    backupCount=settings.backups, encoding='utf-8')
                handler._knowpath_owned = True
                handler.setFormatter(JsonFormatter(service=service))
                handler.addFilter(_NoiseFilter())
                root.addHandler(handler)
            except OSError as exc:
                log_event(logging.getLogger(__name__), 'logging.file.unavailable', level=logging.ERROR, exc=exc)
        for name in _third_party:
            logging.getLogger(name).setLevel(logging.WARNING)
        for name in ('uvicorn','uvicorn.error','uvicorn.access'):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = True
        logging.getLogger('uvicorn.access').disabled = True  # Safe route-based access logging lives in ASGI middleware.


def shutdown_logging():
    global _original
    with _lock:
        _remove_owned()
        if _original is not None:
            root_level, loggers = _original
            logging.getLogger().setLevel(root_level)
            for name, (level, disabled, propagate, handlers) in loggers.items():
                logger = logging.getLogger(name)
                logger.setLevel(level)
                logger.disabled, logger.propagate, logger.handlers = disabled, propagate, handlers
            _original = None
