"""Reusable Qt plumbing: run blocking API calls off the UI thread."""

# version: 0.3.0

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

logger = logging.getLogger(__name__)


class _CallSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class ApiCall(QRunnable):
    def __init__(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_failed: Callable[[str], None],
    ):
        super().__init__()
        self.fn = fn
        self.signals = _CallSignals()
        self.signals.done.connect(on_done)
        self.signals.failed.connect(on_failed)

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:
            logger.info("API call failed: %s", exc)
            self.signals.failed.emit(str(exc))
        else:
            self.signals.done.emit(result)


def submit(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_failed: Callable[[str], None],
) -> None:
    """Run ``fn`` (a blocking ApiClient call) on the global thread pool."""
    QThreadPool.globalInstance().start(ApiCall(fn, on_done, on_failed))


def show_error(parent, message: str) -> None:
    """Uniform error surface for failed API/tunnel operations."""
    from PySide6.QtWidgets import QMessageBox

    logger.error("%s", message)
    QMessageBox.critical(parent, "newspipeline client", message)


def show_error_with(parent, prefix: str):
    """Bound error handler with a context prefix, e.g. show_error_with(self,
    'Loading failed') -> handler(message)."""
    def handler(message: str) -> None:
        show_error(parent, f"{prefix}\n\n{message}")
    return handler


def iter_form_controls(widget):
    """All editable form controls under ``widget``.

    PySide6's findChildren does not accept a tuple of types (its isinstance
    check rejects it), so iterate per class.
    """
    from PySide6.QtWidgets import QLineEdit, QSpinBox, QComboBox

    for cls in (QLineEdit, QSpinBox, QComboBox):
        yield from widget.findChildren(cls)
