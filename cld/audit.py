"""Audit logging wiring.

This is *runtime* wiring, run on every command — not a one-time setup. It ensures
the logs/ directory exists and attaches a file handler so that every write action
(server/volume create, attach, and every rollback delete) is recorded with its
resource IDs. The persistent log is the audit trail behind this tooling's
maximum-defensive posture; interactive console output stays on cld.ui.

logs/ lives at the repository root (one level above this package).
"""

import datetime
import logging
import os

_LOGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

_logger = None


def get_logger():
    """Return the process-wide audit logger, configuring it once.

    Idempotent: makedirs(exist_ok=True) just guarantees the log target exists
    before this run writes to it; it installs nothing persistent.
    """
    global _logger
    if _logger is not None:
        return _logger

    log = logging.getLogger("cld")
    log.setLevel(logging.INFO)
    log.propagate = False

    if not log.handlers:
        try:
            os.makedirs(_LOGS_DIR, exist_ok=True)
            day = datetime.date.today().strftime("%Y%m%d")
            handler = logging.FileHandler(
                os.path.join(_LOGS_DIR, f"cld-{day}.log"))
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(handler)
        except OSError:
            # Never let logging failure block the tool; fall back to no-op.
            log.addHandler(logging.NullHandler())

    _logger = log
    return _logger


def audit(action, **fields):
    """Record one write-relevant event, e.g. audit('server.create', id=..., name=...)."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    get_logger().info(f"{action} {extra}".rstrip())


def warn(action, **fields):
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    get_logger().warning(f"{action} {extra}".rstrip())
