"""Recipient and shared defaults editor; omitted fields remain inherited."""

# version: 0.6.1

from copy import deepcopy
import json
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QPlainTextEdit, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)
from ..workers import submit, show_error_with


class RecipientsTab(QWidget):
    data_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = None
        self._raw = {}
        self._loading = False
        self._shared = True
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._select)
        left.addWidget(self.list)
        add = QPushButton("New recipient…")
        add.clicked.connect(self._new)
        left.addWidget(add)
        delete = QPushButton("Delete recipient")
        delete.clicked.connect(self._delete)
        self.delete_button = delete
        left.addWidget(delete)
        root.addLayout(left, 1)
        editor = QWidget()
        self.editor = editor
        editor.setEnabled(False)
        form = QFormLayout(editor)
        self.identity = QLabel("Shared defaults")
        form.addRow("Recipient", self.identity)
        self.parent_id = QLineEdit()
        self.parent_id.setPlaceholderText("empty = shared defaults")
        form.addRow("Inherit from recipient id", self.parent_id)
        self.inherit_email = QCheckBox("Inherit email address")
        self.email = QLineEdit()
        form.addRow(self.inherit_email)
        form.addRow("Email address", self.email)
        form.addRow(QLabel("Unchecked overrides follow the parent or shared defaults.\nInterests and feeds are managed in the Interests tab."))
        self.enabled = {}
        self.words = {}
        self.details = {}
        for kind, label in (("summary", "Summary"), ("script", "Script EN"), ("script_de", "Script DE"), ("brief", "Brief")):
            enabled = QCheckBox(f"{label} enabled")
            enabled.setTristate(True)
            form.addRow(enabled)
            self.enabled[kind] = enabled
            override = QCheckBox("Override word target")
            words = QSpinBox()
            words.setRange(1, 100000)
            row = QHBoxLayout()
            row.addWidget(override)
            row.addWidget(words)
            form.addRow(f"{label} length", row)
            self.words[kind] = (override, words)
            detail = QComboBox()
            detail.addItems(["Inherit", "concise", "standard", "detailed"])
            form.addRow(f"{label} detail", detail)
            self.details[kind] = detail
        self.modes = QComboBox()
        self.modes.addItems(["Inherit", "Local files", "Email", "Files and email"])
        form.addRow("Delivery modes", self.modes)
        self.delivery_mode = QComboBox()
        self.delivery_mode.addItems(["Inherit", "Immediately after collection", "Scheduled per interest"])
        form.addRow("Delivery timing default", self.delivery_mode)
        self.delivery_time = QLineEdit("09:00")
        form.addRow("Delivery time (HH:MM)", self.delivery_time)
        self.interval = QSpinBox()
        self.interval.setRange(1, 24)
        self.interval.setValue(24)
        form.addRow("Delivery interval hours", self.interval)
        self.timezone = QLineEdit()
        self.timezone.setPlaceholderText("empty = server timezone")
        form.addRow("Delivery timezone", self.timezone)
        self.save = QPushButton("Save recipient settings")
        self.save.clicked.connect(self._save)
        form.addRow(self.save)
        complete = QPushButton("Edit complete configuration and interest templates…")
        complete.clicked.connect(self._edit_configuration)
        form.addRow(complete)
        form.addRow(QLabel("The complete editor manages named interest templates, feeds,\n"
                            "disabled interests and settings absent from this form.\n"
                            "Omitted fields inherit; explicit values override."))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(editor)
        root.addWidget(scroll, 2)
        self.setEnabled(False)

    def set_client(self, client):
        self.client = client
        self.editor.setEnabled(False)
        self.setEnabled(client is not None)
        if client:
            self.refresh()
        else:
            self.list.clear()
            self._raw = {}

    def refresh(self):
        if not self.client:
            return
        client = self.client
        submit(lambda: (client.get_recipient_defaults(), client.list_recipients()),
               lambda result: self._populate(result) if self.client is client else None,
               show_error_with(self, "Loading recipients failed"))

    def _populate(self, result):
        shared, recipients = result
        current = self._raw.get("id")
        self._loading = True
        self.list.clear()
        for label, data in [("Shared defaults", shared), *((r["id"], r) for r in recipients)]:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, data)
            self.list.addItem(item)
        self._loading = False
        index = next((i for i in range(self.list.count()) if self.list.item(i).data(Qt.UserRole).get("id") == current), 0)
        self.list.setCurrentRow(index)

    def _select(self, item, _previous):
        if self._loading or item is None:
            return
        self.editor.setEnabled(True)
        self._raw = deepcopy(item.data(Qt.UserRole))
        self._shared = "id" not in self._raw
        self.delete_button.setEnabled(not self._shared)
        self.identity.setText(self._raw.get("id", "Shared defaults"))
        self.parent_id.setText(self._raw.get("parent", ""))
        self.parent_id.setEnabled(not self._shared)
        self.email.setText(self._raw.get("email", ""))
        self.inherit_email.setChecked("email" not in self._raw)
        defaults = self._raw.get("interest_defaults", {})
        for kind in self.enabled:
            flag = f"enable_{kind}"
            self.enabled[kind].setCheckState(Qt.PartiallyChecked if flag not in defaults else Qt.Checked if defaults[flag] else Qt.Unchecked)
            override, words = self.words[kind]
            override.setChecked(f"target_{kind}_words" in defaults)
            words.setValue(defaults.get(f"target_{kind}_words", 600 if kind == "summary" else 690 if kind == "brief" else 1250))
            self.details[kind].setCurrentText(defaults.get(f"{kind}_detail", "Inherit"))
        modes = defaults.get("delivery")
        self.modes.setCurrentIndex(0 if modes is None else 3 if len(modes) == 2 else 2 if "email" in modes else 1)
        schedule = defaults.get("delivery_schedule")
        self.delivery_mode.setCurrentIndex(0 if schedule is None else 2 if schedule else 1)
        self.delivery_time.setText((schedule or {}).get("start_time", "09:00"))
        self.interval.setValue((schedule or {}).get("interval_hours", 24))
        self.timezone.setText((schedule or {}).get("timezone", ""))

    def _edit_configuration(self):
        if not self.client or self.list.currentItem() is None:
            return
        client, shared = self.client, self._shared
        rid = self._raw.get("id")
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Complete configuration — {rid or 'Shared defaults'}")
        dialog.resize(700, 600)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Edit sparse JSON configuration. Named interests merge by name; feed lists replace.\n"
                               "Remove an override to inherit. Set enabled to false to exclude an interest,\n"
                               "or true to restore it. Save replaces this configuration; keep entries to retain.\n"
                               "Recipient ids cannot change. Secrets belong on the server."))
        editor = QPlainTextEdit(json.dumps(self._raw, indent=2, ensure_ascii=False))
        layout.addWidget(editor)
        error = QLabel()
        error.setWordWrap(True)
        layout.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        def save():
            if self.client is not client:
                error.setText("The connection changed. Close this editor and reload the configuration.")
                return
            try:
                data = json.loads(editor.toPlainText())
                if not isinstance(data, dict):
                    raise ValueError("Configuration must be a JSON object.")
                if not shared and data.get("id", rid) != rid:
                    raise ValueError("Recipient ids cannot change.")
            except (ValueError, TypeError) as exc:
                error.setText(str(exc))
                return
            buttons.button(QDialogButtonBox.Save).setEnabled(False)

            def failed(message):
                error.setText(message)
                buttons.button(QDialogButtonBox.Save).setEnabled(True)

            def saved(_):
                dialog.accept()
                if self.client is client:
                    self.refresh()
                    self.data_changed.emit()

            submit(lambda: client.put_recipient_defaults(data) if shared else client.update_recipient(rid, data),
                   saved, failed)

        buttons.accepted.connect(save)
        dialog.exec()

    def _save(self):
        if not self.client or self.list.currentItem() is None:
            return
        data = deepcopy(self._raw)
        if self.inherit_email.isChecked():
            data.pop("email", None)
        else:
            data["email"] = self.email.text().strip()
        if not self._shared:
            data["parent"] = self.parent_id.text().strip()
        defaults = data.setdefault("interest_defaults", {})
        for kind in self.enabled:
            flag = f"enable_{kind}"
            state = self.enabled[kind].checkState()
            if state == Qt.PartiallyChecked:
                defaults.pop(flag, None)
            else:
                defaults[flag] = state == Qt.Checked
            override, words = self.words[kind]
            if override.isChecked():
                defaults[f"target_{kind}_words"] = words.value()
            else:
                defaults.pop(f"target_{kind}_words", None)
            if self.details[kind].currentIndex():
                defaults[f"{kind}_detail"] = self.details[kind].currentText()
            else:
                defaults.pop(f"{kind}_detail", None)
        if self.modes.currentIndex():
            defaults["delivery"] = {1: ["file"], 2: ["email"], 3: ["file", "email"]}[self.modes.currentIndex()]
        else:
            defaults.pop("delivery", None)
        if self.delivery_mode.currentIndex() == 0:
            defaults.pop("delivery_schedule", None)
        elif self.delivery_mode.currentIndex() == 1:
            defaults["delivery_schedule"] = {}
        else:
            defaults["delivery_schedule"] = {"start_time": self.delivery_time.text().strip(), "interval_hours": self.interval.value()}
            if self.timezone.text().strip():
                defaults["delivery_schedule"]["timezone"] = self.timezone.text().strip()
        client, shared = self.client, self._shared
        submit(lambda: client.put_recipient_defaults(data) if shared else client.update_recipient(data["id"], data),
               lambda _: (self.refresh(), self.data_changed.emit()), show_error_with(self, "Saving recipient settings failed"))

    def _new(self):
        if not self.client:
            return
        rid, ok = QInputDialog.getText(self, "New recipient", "Recipient id (letters, numbers, underscores or dashes):")
        if not ok or not rid.strip():
            return
        client = self.client
        def created(_):
            if self.client is client:
                self._raw = {"id": rid.strip()}
                self.refresh()
                self.data_changed.emit()
        submit(lambda: client.create_recipient({"id": rid.strip()}),
               created, show_error_with(self, "Creating recipient failed"))

    def _delete(self):
        if not self.client or self._shared:
            return
        rid = self._raw["id"]
        if QMessageBox.question(self, "Delete recipient", f"Delete {rid!r}? Stored run history and queued deliveries are retained.") != QMessageBox.Yes:
            return
        client = self.client
        submit(lambda: client.delete_recipient(rid), lambda _: (self.refresh(), self.data_changed.emit()),
               show_error_with(self, "Deleting recipient failed"))
