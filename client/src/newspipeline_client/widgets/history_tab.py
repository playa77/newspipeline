"""Run-history tab — recent runs per interest from GET /api/status."""

# version: 0.3.0

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiClient
from ..workers import show_error_with, submit

_COLUMNS = ("Run", "Status", "Stage", "Error", "Started (UTC)", "Completed (UTC)")


class HistoryTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.client: ApiClient | None = None
        self._snapshot: list[dict[str, Any]] = []
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Interest"))
        self.interest_combo = QComboBox()
        self.interest_combo.currentIndexChanged.connect(self._render_selected)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(self.interest_combo, 1)
        top.addWidget(refresh_btn)
        root.addLayout(top)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.Stretch,
        )  # the error column gets the space
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.table, 1)

    def set_client(self, client: ApiClient | None) -> None:
        self.client = client
        if client is not None:
            self.refresh()

    def refresh(self) -> None:
        if self.client is None:
            return
        submit(self.client.get_status, self._apply,
               show_error_with(self, "Loading run history failed"))

    def _apply(self, data: dict[str, Any]) -> None:
        self._snapshot = data.get("interests", [])
        names = [entry.get("name", "?") for entry in self._snapshot]
        current = self.interest_combo.currentText()
        self.interest_combo.blockSignals(True)
        self.interest_combo.clear()
        self.interest_combo.addItems(names)
        if current in names:
            self.interest_combo.setCurrentText(current)
        self.interest_combo.blockSignals(False)
        self._render_selected()

    def _render_selected(self) -> None:
        index = max(0, self.interest_combo.currentIndex())
        rows: list[dict[str, Any]] = []
        if 0 <= index < len(self._snapshot):
            rows = self._snapshot[index].get("recent_runs", [])
        self.table.setRowCount(len(rows))
        for row_idx, run in enumerate(rows):
            values = (
                str(run.get("id", "")),
                run.get("status", ""),
                run.get("current_stage") or "",
                run.get("error") or "",
                run.get("started_at") or "",
                run.get("completed_at") or "",
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 1 and value == "failed":
                    item.setForeground(QColor("#b2182b"))
                self.table.setItem(row_idx, col, item)
