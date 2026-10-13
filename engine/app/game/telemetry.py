"""Telemetry: log_event records the event name in the log and nothing else;
no per-turn record is kept.
"""

import logging


log = logging.getLogger(__name__)


def log_event(event: str) -> None:
    """Log the event name only. Never raises."""
    log.info("event %s", event)
