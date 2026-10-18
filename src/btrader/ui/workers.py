"""Run blocking exchange calls off the UI thread."""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)


class TaskSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, object)


class Task(QRunnable):
    """One-shot background job wrapping a callable."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = TaskSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # pragma: no cover - executed in a worker thread
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            log.debug("后台任务失败: %s", traceback.format_exc())
            self._emit(self.signals.failed, str(exc) or exc.__class__.__name__)
            return
        self._emit(self.signals.finished, result)

    @staticmethod
    def _emit(signal, payload) -> None:
        # The receiving widget may already be gone (window closed).
        try:
            signal.emit(payload)
        except RuntimeError:
            pass


class TaskRunner(QObject):
    """Convenience wrapper around the global thread pool.

    Keeps references to in-flight tasks so Qt does not garbage collect the
    signal object before the worker finishes.
    """

    def __init__(self, parent: QObject | None = None, max_threads: int = 4) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max_threads)
        self._active: set[Task] = set()

    def run(
        self,
        fn: Callable[..., Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Task:
        task = Task(fn, *args, **kwargs)
        self._active.add(task)

        def _done(result: Any) -> None:
            self._active.discard(task)
            if on_done:
                on_done(result)

        def _error(message: str) -> None:
            self._active.discard(task)
            if on_error:
                on_error(message)
            else:
                log.warning("后台任务错误: %s", message)

        task.signals.finished.connect(_done)
        task.signals.failed.connect(_error)
        self.pool.start(task)
        return task

    def busy(self) -> bool:
        return bool(self._active)

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Drop queued work and wait briefly for running tasks to finish."""
        self.pool.clear()
        self.pool.waitForDone(timeout_ms)
        self._active.clear()
