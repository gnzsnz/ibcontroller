"""Unit tests for logging_setup.py -- no live Gateway needed."""

from __future__ import annotations

import logging
import logging.handlers

from typed_settings.types import Secret

from ibcontroller.logging_setup import (
    configure_logging,
    configure_trace,
    stop_logging,
)


def test_configure_logging_sets_up_console_handler():
    configure_logging(level=logging.DEBUG)
    logger = logging.getLogger("ibcontroller")
    assert logger.level == logging.DEBUG
    assert logger.propagate is False
    assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
    stop_logging()


def test_configure_logging_adds_queued_file_handler_when_log_dir_given(tmp_path):
    configure_logging(log_dir=tmp_path)
    logger = logging.getLogger("ibcontroller")
    # The file handler is behind a QueueHandler (module docstring, 2026-09-08):
    # the logger itself sees no FileHandler -- that lives on the listener thread.
    assert any(isinstance(h, logging.handlers.QueueHandler) for h in logger.handlers)
    assert not any(isinstance(h, logging.FileHandler) for h in logger.handlers)

    child = logging.getLogger("ibcontroller.dispatch")
    child.info("test message, no secret involved")
    # stop_logging is load-bearing here: the record was queued, and only the
    # listener thread's drain (joined by stop_logging) guarantees it reached
    # the file before we read it.
    stop_logging()

    log_file = tmp_path / "ibcontroller.log"
    assert log_file.exists()
    assert "test message" in log_file.read_text()


def test_configure_logging_filename_carries_the_instance_name(tmp_path):
    configure_logging(log_dir=tmp_path, filename="ibcontroller-paper.log")
    logger = logging.getLogger("ibcontroller")
    logger.info("instance-qualified log file")
    stop_logging()

    assert (tmp_path / "ibcontroller-paper.log").exists()
    assert not (tmp_path / "ibcontroller.log").exists()


def test_configure_logging_is_idempotent(tmp_path):
    configure_logging(log_dir=tmp_path)
    configure_logging(log_dir=tmp_path)
    logger = logging.getLogger("ibcontroller")
    assert len(logger.handlers) == 2  # console + queue handler, not accumulated
    stop_logging()


def test_secret_is_already_safe_through_ordinary_logging(tmp_path):
    """Sanitization isn't this module's job -- it's already handled at the
    value's own origin (typed_settings.types.Secret). A Secret passed straight
    into a logging call, with no special handling here, must never show its
    real value. Checked against the real file this module writes, not
    `caplog` -- `caplog`'s default capture relies on propagation to the root
    logger, which `configure_logging` deliberately disables (its own module
    docstring), so it can't see records from an `ibcontroller.*` child logger
    without extra wiring caplog itself doesn't do automatically."""
    configure_logging(log_dir=tmp_path)
    logger = logging.getLogger("ibcontroller.somewhere")
    secret = Secret("super-secret-value")

    logger.info("attempting login as %s", secret)
    stop_logging()

    log_file = tmp_path / "ibcontroller.log"
    contents = log_file.read_text()
    assert "super-secret-value" not in contents
    assert "*******" in contents


def test_configure_trace_enabled_writes_pure_ndjson(tmp_path):
    configure_trace(instance="paper", enabled=True, trace_dir=tmp_path)
    logger = logging.getLogger("ibcontroller.trace.paper.cmd")
    assert logger.isEnabledFor(logging.DEBUG)
    logger.debug('{"sent": true}')
    stop_logging()

    cmd_file = tmp_path / "cmd-paper.jsonl"
    assert cmd_file.exists()
    # NDJSON: the line is exactly the JSON object, no
    # asctime/levelname/name prefix from the default formatter.
    assert cmd_file.read_text().splitlines() == ['{"sent": true}']


def test_configure_trace_disabled_emits_nothing(tmp_path):
    configure_trace(instance="paper", enabled=False, trace_dir=tmp_path)
    logger = logging.getLogger("ibcontroller.trace.paper.cmd")
    assert not logger.isEnabledFor(logging.DEBUG)
    logger.debug("should not be written")
    assert not logger.handlers
    stop_logging()
    assert not (tmp_path / "cmd-paper.jsonl").exists()
    assert not (tmp_path / "events-paper.jsonl").exists()


def test_configure_trace_instances_do_not_cross_write(tmp_path):
    configure_trace(instance="paper", enabled=True, trace_dir=tmp_path)
    configure_trace(instance="live", enabled=True, trace_dir=tmp_path)
    logging.getLogger("ibcontroller.trace.paper.cmd").debug('{"who": "paper"}')
    logging.getLogger("ibcontroller.trace.live.cmd").debug('{"who": "live"}')
    stop_logging()

    paper_text = (tmp_path / "cmd-paper.jsonl").read_text()
    live_text = (tmp_path / "cmd-live.jsonl").read_text()
    assert '"who": "paper"' in paper_text
    assert '"who": "live"' not in paper_text
    assert '"who": "live"' in live_text
    assert '"who": "paper"' not in live_text


def test_configure_trace_truncates_stale_files_when_enabled(tmp_path):
    (tmp_path / "cmd-paper.jsonl").write_text("stale content from a dead instance\n")
    configure_trace(instance="paper", enabled=True, trace_dir=tmp_path)
    logging.getLogger("ibcontroller.trace.paper.cmd").debug('{"fresh": true}')
    stop_logging()

    cmd_text = (tmp_path / "cmd-paper.jsonl").read_text()
    assert "stale content" not in cmd_text
    assert '"fresh": true' in cmd_text
