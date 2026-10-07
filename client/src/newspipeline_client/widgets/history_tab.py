"""Run-history tab — recent runs per interest from GET /api/status."""

# version: 0.6.0

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
        root.addWidget(QLabel("Scheduled deliveries"))
        self.delivery_table = QTableWidget(0, 5)
        self.delivery_table.setHorizontalHeaderLabels(["Run", "Due (UTC)", "Delivery status", "Attempts", "Error"])
        self.delivery_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.delivery_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        root.addWidget(self.delivery_table, 1)
        retry = QPushButton("Retry selected failed delivery")
        retry.clicked.connect(self._retry_delivery)
        root.addWidget(retry)

    def set_client(self, client: ApiClient | None) -> None:
        self.client = client
        self._snapshot = []
        self.interest_combo.clear()
        self.table.setRowCount(0)
        self.delivery_table.setRowCount(0)
        if client is not None:
            self.refresh()

    def refresh(self) -> None:
        if self.client is None:
            return
        client = self.client
        submit(client.get_status, lambda data: self._apply(data) if self.client is client else None,
               show_error_with(self, "Loading run history failed"))

    def _apply(self, data: dict[str, Any]) -> None:
        deliveries = data.get("deliveries", [])
        self.delivery_table.setRowCount(len(deliveries))
        for row, job in enumerate(deliveries):
            for col, key in enumerate(("run_id", "due_at", "status", "attempts", "error")):
                self.delivery_table.setItem(row, col, QTableWidgetItem(str(job.get(key) or "")))
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

    def _retry_delivery(self):
        row = self.delivery_table.currentRow()
        if self.client is None or row < 0:
            return
        run_id = int(self.delivery_table.item(row, 0).text())
        client = self.client
        submit(lambda: client.retry_delivery(run_id), lambda _: self.refresh(),
               show_error_with(self, "Retrying delivery failed"))

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
