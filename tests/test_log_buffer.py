"""Tests for the global error-log capture and viewer."""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("PySide6")

from btrader.logging_setup import ErrorLogBuffer  # noqa: E402


@pytest.fixture
def buffer() -> ErrorLogBuffer:
    buf = ErrorLogBuffer(capacity=5)
    logger = logging.getLogger("btrader.test.logbuf")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.handlers = [buf]
    yield buf
    logger.handlers = []


def _log(buffer: ErrorLogBuffer, level: int, message: str, exc_info=None) -> None:
    logger = logging.getLogger("btrader.test.logbuf")
    logger.log(level, message, exc_info=exc_info)


def test_captures_warnings_and_errors_only(buffer):
    _log(buffer, logging.INFO, "忽略")
    _log(buffer, logging.WARNING, "警告一")
    _log(buffer, logging.ERROR, "错误一")

    messages = [item.message for item in buffer.records()]
    assert messages == ["警告一", "错误一"]
    assert buffer.counts() == (1, 1)


def test_error_detail_keeps_the_traceback(buffer):
    try:
        raise ValueError("模拟失败")
    except ValueError:
        _log(buffer, logging.ERROR, "下单出错", exc_info=True)

    record = buffer.records()[-1]
    assert record.level_name == "ERROR"
    assert "ValueError: 模拟失败" in record.detail
    assert "模拟失败" in record.as_text()


def test_ring_buffer_drops_oldest(buffer):
    for index in range(8):
        _log(buffer, logging.WARNING, f"警告{index}")

    messages = [item.message for item in buffer.records()]
    assert len(messages) == 5
    assert messages[0] == "警告3"
    assert messages[-1] == "警告7"


def test_revision_changes_on_emit_and_clear(buffer):
    start = buffer.revision
    _log(buffer, logging.WARNING, "警告")
    assert buffer.revision > start

    marked = buffer.revision
    buffer.clear()
    assert buffer.records() == []
    assert buffer.revision > marked
