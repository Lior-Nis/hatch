"""Structured logging: one JSON object per line, with every ``extra`` field
(experiment_id, job_id, error, ...) preserved as data rather than prose."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_STANDARD_FIELDS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys() | {"message", "asctime"}
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS:
                line[key] = value
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging(*, level: str = "INFO", json_format: bool = True) -> None:
    """Send application logs to stderr. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter()
        if json_format
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, "_hatch", False)]:
        root.removeHandler(existing)
    handler._hatch = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
