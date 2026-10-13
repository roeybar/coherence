"""Hosted-mode startup: the filesystem checks, the log redaction, then the
write-refusing audit hook.

The root, /app and /data must sit on read-only mounts and /tmp must be a
tmpfs, or the engine refuses to start. A write probe then runs as a second
check, and only EROFS counts as read-only. Only then is the hook installed.
It refuses writes anywhere outside /tmp. Hosted log lines carry the logger
name, the level and an exception type or the bare event text, nothing else.
"""

import errno
import logging
import logging.config
import os
import sys

PROBE_DIRS = ("/", "/app", "/data")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_PATH_EVENTS = {
    "os.mkdir": (0,),
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.symlink": (1,),
    "os.link": (1,),
    "os.truncate": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.utime": (0,),
}

probe_result: dict = {}
_hooked = False

LOG_FORMAT = "%(name)s %(levelname)s %(message)s"
# Applied after uvicorn's own config: its loggers lose their handlers and
# propagate to the redacting root handler.
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        name: {"handlers": [], "propagate": True}
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    },
}


class HostedWriteRefused(PermissionError):
    pass


def current_mode() -> str:
    return "hosted" if os.environ.get("COHERENCE_MODE") == "hosted" else "selfhost"


def _existing(d: str) -> str:
    while not os.path.isdir(d):
        d = os.path.dirname(d) or "/"
    return d


def tmp_is_tmpfs(mounts: str = "/proc/mounts") -> bool:
    try:
        with open(mounts, encoding="ascii", errors="replace") as fh:
            rows = [line.split() for line in fh]
    except OSError:
        return False
    found = [r[2] for r in rows if len(r) > 2 and r[1] == "/tmp"]
    return bool(found) and found[-1] == "tmpfs"


def boot_probe() -> dict:
    for d in PROBE_DIRS:
        if not os.statvfs(_existing(d)).f_flag & os.ST_RDONLY:
            raise RuntimeError("hosted mode refuses to start: %s is not on a read-only mount" % d)
    if not tmp_is_tmpfs():
        raise RuntimeError("hosted mode refuses to start: /tmp is not a tmpfs")
    out = {}
    for d in PROBE_DIRS:
        target = os.path.join(d, ".write-probe-%d" % os.getpid())
        made = None
        try:
            if os.path.isdir(d):
                os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                made = target
            else:
                os.mkdir(d)
                made = d
        except OSError as e:
            if e.errno == errno.EROFS:
                out[d] = e.errno
                continue
        if made:
            try:
                os.rmdir(made) if made == d else os.remove(made)
            except OSError:
                pass
        raise RuntimeError("hosted mode refuses to start: %s is not read-only" % d)
    return out


def redact(record: logging.LogRecord) -> logging.LogRecord:
    """Keeps an exception type name or the event text; drops args and tracebacks."""
    exc = record.exc_info[1] if isinstance(record.exc_info, tuple) else None
    if exc is None and isinstance(record.args, tuple):
        exc = next((a for a in record.args if isinstance(a, BaseException)), None)
    if exc is not None:
        record.msg = type(exc).__name__
    elif isinstance(record.msg, str):
        record.msg = (record.msg.splitlines() or [""])[0]
    else:
        record.msg = type(record.msg).__name__
    record.args = None
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    return record


class RedactFilter(logging.Filter):
    def filter(self, record):
        redact(record)
        return True


class _Stderr(logging.StreamHandler):
    """Writes to whatever sys.stderr is at emit time."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


def install_hosted_logging() -> None:
    """Every record is redacted when it is made, so no handler sees more."""
    base = logging.getLogRecordFactory()
    if not getattr(base, "_hosted", False):

        def factory(*args, **kwargs):
            return redact(base(*args, **kwargs))

        factory._hosted = True
        logging.setLogRecordFactory(factory)
    root = logging.getLogger()
    if not any(getattr(h, "_hosted", False) for h in root.handlers):
        handler = _Stderr()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(RedactFilter())
        handler._hosted = True
        root.addHandler(handler)
    logging.config.dictConfig(UVICORN_LOG_CONFIG)


def _outside_tmp(path) -> bool:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str):
        return False
    real = os.path.realpath(path)
    return not (real == "/tmp" or real.startswith("/tmp/"))


def _hook(event, args):
    if event == "open":
        path, mode, flags = args
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        if writing and _outside_tmp(path):
            raise HostedWriteRefused(errno.EACCES, "hosted mode refuses writes", str(path))
    elif event in _PATH_EVENTS:
        for i in _PATH_EVENTS[event]:
            if i < len(args) and _outside_tmp(args[i]):
                raise HostedWriteRefused(errno.EACCES, "hosted mode refuses writes", str(args[i]))


def startup() -> dict:
    """Checks, logging, then hook. Idempotent: the hook is installed once."""
    global _hooked
    if not _hooked:
        probe_result.update(boot_probe())
        install_hosted_logging()
        sys.addaudithook(_hook)
        _hooked = True
    return dict(probe_result)
