"""Interests tab — CRUD over /api/interests as lists and forms.

Covers feature parity with the old web UI (W6): interest create/edit/
delete, feed add/remove (WP7 convention: no in-place feed edit), schedule,
deliverable toggles with word targets, delivery modes with per-interest
recipient override, paused state made discoverable, trigger-now, and
first-class rename (D11) with a trivial confirm — history is preserved.
"""

# version: 0.3.1

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiClient
from ..workers import iter_form_controls, show_error, show_error_with, submit

_DELIVERABLES = (
    ("enable_summary", "target_summary_words", "Summary"),
    ("enable_script", "target_script_words", "Script EN"),
    ("enable_script_de", "target_script_de_words", "Script DE"),
    ("enable_brief", "target_brief_words", "Daily brief"),
)
_LENGTH_MODES = ("full_article", "headers_only", "word_count")


class FeedDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add feed")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com/rss.xml")
        self.category = QLineEdit()
        form.addRow("Name", self.name)
        form.addRow("URL", self.url)
        form.addRow("Category (optional)", self.category)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class InterestsTab(QWidget):
    data_changed = Signal()   # after any mutation
    triggered = Signal(str)   # a run was triggered

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.client: ApiClient | None = None
        self._current_name: str | None = None
        self._loading = False
        self._build_ui()

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)

        # Left: the list of interests.
        left = QVBoxLayout()
        left.addWidget(QLabel("Interests"))
        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._on_selected)
        left.addWidget(self.list_widget)
        new_btn = QPushButton("New interest…")
        new_btn.clicked.connect(self._new_interest)
        left.addWidget(new_btn)
        root.addLayout(left, 1)

        # Right: editor form for the selected interest.
        right = QVBoxLayout()

        header = QHBoxLayout()
        self.name_label = QLabel("(no interest selected)")
        bold = QFont(); bold.setBold(True)
        self.name_label.setFont(bold)
        header.addWidget(self.name_label, 1)
        rename_btn = QPushButton("Rename…")
        rename_btn.clicked.connect(self._rename)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete)
        trigger_btn = QPushButton("Trigger now")
        trigger_btn.clicked.connect(self._trigger)
        header.addWidget(rename_btn)
        header.addWidget(delete_btn)
        header.addWidget(trigger_btn)
        right.addLayout(header)

        # Paused state must be discoverable at a glance (W6).
        self.paused_notice = QLabel(
            "PAUSED — all deliverables disabled; this interest will not "
            "run on a schedule."
        )
        self.paused_notice.setStyleSheet(
            "color: white; background-color: #a05a00; padding: 6px;"
            "border-radius: 4px;"
        )
        self.paused_notice.setWordWrap(True)
        self.paused_notice.hide()
        right.addWidget(self.paused_notice)

        schedule_box = QGroupBox("Schedule")
        schedule_form = QFormLayout(schedule_box)
        self.start_time = QLineEdit()
        self.start_time.setPlaceholderText("HH:MM")
        self.interval_hours = QSpinBox(); self.interval_hours.setRange(1, 168)
        schedule_form.addRow("Start time", self.start_time)
        schedule_form.addRow("Interval hours", self.interval_hours)
        right.addWidget(schedule_box)

        deliverables_box = QGroupBox("Deliverables")
        grid = QGridLayout(deliverables_box)
        self.deliverables: dict[str, tuple[QCheckBox, QSpinBox]] = {}
        for row, (flag, words_key, label) in enumerate(_DELIVERABLES):
            flag_box = QCheckBox(label)
            flag_box.stateChanged.connect(lambda *_: self._update_paused_notice())
            words = QSpinBox(); words.setRange(1, 100000)
            words.setSuffix(" words target")
            grid.addWidget(flag_box, row, 0)
            grid.addWidget(words, row, 1)
            self.deliverables[flag] = (flag_box, words)
        right.addWidget(deliverables_box)

        input_box = QGroupBox("Analysis input")
        input_form = QFormLayout(input_box)
        self.length_mode = QComboBox()
        for mode in _LENGTH_MODES:
            self.length_mode.addItem(mode, mode)
        self.input_word_count = QSpinBox(); self.input_word_count.setRange(1, 100000)
        input_form.addRow("Article text mode", self.length_mode)
        input_form.addRow("Word count (word_count mode)", self.input_word_count)
        right.addWidget(input_box)

        delivery_box = QGroupBox("Delivery")
        delivery_layout = QFormLayout(delivery_box)
        modes_row = QHBoxLayout()
        self.file_mode = QCheckBox("Local files (server archive)")
        self.email_mode = QCheckBox("Email")
        modes_row.addWidget(self.file_mode)
        modes_row.addWidget(self.email_mode)
        self.email_recipient = QLineEdit()
        self.email_recipient.setPlaceholderText("empty = global fallback recipient")
        delivery_layout.addRow(modes_row)
        delivery_layout.addRow("Recipient override", self.email_recipient)
        right.addWidget(delivery_box)

        feeds_box = QGroupBox("Feeds")
        feeds_layout = QVBoxLayout(feeds_box)
        self.feeds_list = QListWidget()
        feeds_layout.addWidget(self.feeds_list)
        feeds_buttons = QHBoxLayout()
        add_feed_btn = QPushButton("Add feed…")
        add_feed_btn.clicked.connect(self._add_feed)
        remove_feed_btn = QPushButton("Remove selected feed")
        remove_feed_btn.clicked.connect(self._remove_feed)
        feeds_buttons.addWidget(add_feed_btn)
        feeds_buttons.addWidget(remove_feed_btn)
        feeds_buttons.addStretch(1)
        feeds_layout.addLayout(feeds_buttons)
        right.addWidget(feeds_box)

        save_btn = QPushButton("Save changes")
        save_btn.clicked.connect(self._save)
        right.addWidget(save_btn)

        right.addStretch(1)
        root.addLayout(right, 2)
        self._set_editor_enabled(False)

    # ---- client lifecycle ------------------------------------------------------

    def set_client(self, client: ApiClient | None) -> None:
        self.client = client
        if client is not None:
            self.refresh()
        else:
            self.list_widget.clear()
            self._set_editor_enabled(False)

    def refresh(self) -> None:
        if self.client is None:
            return
        submit(self.client.list_interests, self._populate,
               show_error_with(self, "Loading interests failed"))

    def _populate(self, interests: list[dict[str, Any]]) -> None:
        self._loading = True
        self.list_widget.clear()
        for interest in interests:
            name = interest.get("name", "?")
            label = f"{name}  (paused)" if interest.get("paused") else name
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, interest)
            self.list_widget.addItem(item)
        self._loading = False
        match = self._find_item(self._current_name) if self._current_name else []
        if match:
            self.list_widget.setCurrentItem(match[0])
        elif self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        else:
            self._show_interest(None)

    def _find_item(self, name: str):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if (item.data(Qt.UserRole) or {}).get("name") == name:
                return [item]
        return []

    # ---- selection & rendering -----------------------------------------------

    def _on_selected(self, current, _previous) -> None:
        if self._loading:
            return
        self._show_interest(current.data(Qt.UserRole) if current else None)

    def _show_interest(self, interest: dict[str, Any] | None) -> None:
        self._current_name = interest.get("name") if interest else None
        self._set_editor_enabled(interest is not None)
        if interest is None:
            self.name_label.setText("(no interest selected)")
            self.feeds_list.clear()
            self.paused_notice.hide()
            return
        self.name_label.setText(interest["name"])
        self.start_time.setText(interest.get("start_time", "04:00"))
        self.interval_hours.setValue(int(interest.get("interval_hours", 24)))
        for flag, (box, words) in self.deliverables.items():
            box.setChecked(bool(interest.get(flag)))
            words.setValue(int(interest.get(_words_for(flag), 600)))
        index = self.length_mode.findData(interest.get("input_data_length_mode"))
        self.length_mode.setCurrentIndex(max(0, index))
        self.input_word_count.setValue(int(interest.get("input_word_count", 256)))
        modes = interest.get("delivery", [])
        self.file_mode.setChecked("file" in modes)
        self.email_mode.setChecked("email" in modes)
        self.email_recipient.setText(interest.get("email_recipient", ""))
        self.feeds_list.clear()
        for feed in interest.get("feeds", []):
            label = f"{feed.get('name', '?')} — {feed.get('url', '')}"
            if feed.get("category"):
                label += f" [{feed['category']}]"
            self.feeds_list.addItem(label)
        self._update_paused_notice()

    def _update_paused_notice(self) -> None:
        any_enabled = any(box.isChecked() for box, _ in self.deliverables.values())
        self.paused_notice.setVisible(
            self._current_name is not None and not any_enabled
        )

    def _set_editor_enabled(self, enabled: bool) -> None:
        for child in iter_form_controls(self):
            child.setEnabled(enabled)
        for box, words in self.deliverables.values():
            box.setEnabled(enabled)
            words.setEnabled(enabled)
        self.file_mode.setEnabled(enabled)
        self.email_mode.setEnabled(enabled)
        self.feeds_list.setEnabled(enabled)

    # ---- actions ------------------------------------------------------------

    def _collect_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "start_time": self.start_time.text().strip(),
            "interval_hours": self.interval_hours.value(),
            "delivery": [
                mode for mode, box in
                (("file", self.file_mode), ("email", self.email_mode))
                if box.isChecked()
            ] or ["file"],
            "email_recipient": self.email_recipient.text().strip(),
            "input_data_length_mode": self.length_mode.currentData(),
            "input_word_count": self.input_word_count.value(),
        }
        for flag, (box, words) in self.deliverables.items():
            payload[flag] = box.isChecked()
            payload[_words_for(flag)] = words.value()
        return payload

    def _save(self) -> None:
        name = self._require_current()
        if name is None or self.client is None:
            return
        payload = self._collect_payload()
        submit(
            lambda: self.client.update_interest(name, payload),
            lambda _: (self.refresh(), self.data_changed.emit()),
            show_error_with(self, f"Saving {name!r} failed"),
        )

    def _new_interest(self) -> None:
        if self.client is None:
            return
        name, ok = QInputDialog.getText(
            self, "New interest",
            "Name of the new interest (feeds and settings editable afterwards):",
        )
        name = name.strip()
        if not ok or not name:
            return
        submit(
            lambda: self.client.create_interest({"name": name}),
            lambda _: (
                setattr(self, "_current_name", name),
                self.refresh(),
                self.data_changed.emit(),
            ),
            show_error_with(self, "Creating the interest failed"),
        )

    def _delete(self) -> None:
        name = self._require_current()
        if name is None or self.client is None:
            return
        answer = QMessageBox.question(
            self, "Delete interest",
            f"Delete interest {name!r}?\n\nNote: its run history stays in "
            "the server database under this name but becomes orphaned "
            "(only renaming preserves history).",
        )
        if answer != QMessageBox.Yes:
            return
        submit(
            lambda: self.client.delete_interest(name),
            lambda _: (
                setattr(self, "_current_name", None),
                self.refresh(),
                self.data_changed.emit(),
            ),
            show_error_with(self, f"Deleting {name!r} failed"),
        )

    def _rename(self) -> None:
        name = self._require_current()
        if name is None or self.client is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename interest",
            f"New name for {name!r}:", text=name,
        )
        new_name = new_name.strip()
        if not ok or not new_name or new_name == name:
            return
        # History is preserved now (D11) — a trivial confirm, no warning.
        confirm = QMessageBox.question(
            self, "Confirm rename",
            f"Rename {name!r} to {new_name!r}?\n\nRun history moves with "
            "the rename.",
        )
        if confirm != QMessageBox.Yes:
            return
        submit(
            lambda: self.client.rename_interest(name, new_name),
            lambda _: (
                setattr(self, "_current_name", new_name),
                self.refresh(),
                self.data_changed.emit(),
            ),
            show_error_with(self, "Renaming failed"),
        )

    def _add_feed(self) -> None:
        name = self._require_current()
        if name is None or self.client is None:
            return
        dialog = FeedDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        feed = {
            "name": dialog.name.text().strip(),
            "url": dialog.url.text().strip(),
            "category": dialog.category.text().strip(),
        }
        submit(
            lambda: self.client.add_feed(name, feed),
            lambda _: (self.refresh(), self.data_changed.emit()),
            show_error_with(self, "Adding the feed failed"),
        )

    def _remove_feed(self) -> None:
        name = self._require_current()
        if name is None or self.client is None:
            return
        index = self.feeds_list.currentRow()
        if index < 0:
            show_error(self, "Select a feed to remove first.")
            return
        submit(
            lambda: self.client.delete_feed(name, index),
            lambda _: (self.refresh(), self.data_changed.emit()),
            show_error_with(self, "Removing the feed failed"),
        )

    def _trigger(self) -> None:
        name = self._require_current()
        if name is None or self.client is None:
            return
        submit(
            lambda: self.client.trigger_interest(name),
            lambda _: (
                QMessageBox.information(
                    self, "newspipeline client",
                    f"Run triggered for {name!r}. See Run history.",
                ),
                self.triggered.emit(name),
            ),
            show_error_with(self, f"Triggering {name!r} failed"),
        )

    # ---- helpers -------------------------------------------------------------

    def _require_current(self) -> str | None:
        name = self._current_name
        if not name:
            show_error(self, "Select an interest first.")
            return None
        return name


def _words_for(flag: str) -> str:
    return {
        "enable_summary": "target_summary_words",
        "enable_script": "target_script_words",
        "enable_script_de": "target_script_de_words",
        "enable_brief": "target_brief_words",
    }[flag]
