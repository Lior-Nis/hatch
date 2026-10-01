import json
import logging

from app.observability.logging import JsonFormatter


def record(message: str, **extra: object) -> logging.LogRecord:
    log_record = logging.LogRecord(
        "app.production.run", logging.WARNING, __file__, 1, message, (), None
    )
    for key, value in extra.items():
        setattr(log_record, key, value)
    return log_record


def test_log_lines_are_json_with_event_level_logger_and_time() -> None:
    line = json.loads(JsonFormatter().format(record("generation_failed")))

    assert line["event"] == "generation_failed"
    assert line["level"] == "warning"
    assert line["logger"] == "app.production.run"
    assert line["time"].endswith("+00:00")


def test_extra_fields_are_included_as_structured_data() -> None:
    line = json.loads(
        JsonFormatter().format(record("generation_failed", experiment_id="exp-1", error="nsfw"))
    )

    assert line["experiment_id"] == "exp-1"
    assert line["error"] == "nsfw"


def test_exceptions_are_captured() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        log_record = record("job_failed")
        log_record.exc_info = sys.exc_info()

    line = json.loads(JsonFormatter().format(log_record))

    assert "RuntimeError: boom" in line["exception"]
