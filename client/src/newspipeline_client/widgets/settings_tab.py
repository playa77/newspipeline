"""Global settings tab — mirrors GET/PUT /api/settings as a form."""

# version: 0.3.0

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiClient
from ..workers import iter_form_controls, show_error_with, submit

_STAGES = ("analysis", "generation", "brief")
_EFFORTS = ("", "low", "medium", "high")


class SettingsTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.client: ApiClient | None = None
        self.api_key_configured = False
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()

        self.api_base_url = QLineEdit()
        self.default_model = QLineEdit()
        self.timezone = QLineEdit()
        self.output_dir = QLineEdit()
        form.addRow("LLM endpoint URL", self.api_base_url)
        form.addRow("Default model", self.default_model)
        form.addRow("Timezone (IANA)", self.timezone)
        form.addRow("Output directory (server-side)", self.output_dir)

        models_box = QGroupBox("Per-stage model overrides (empty = default)")
        models_form = QFormLayout(models_box)
        self.stage_models = {stage: QLineEdit() for stage in _STAGES}
        for stage, edit in self.stage_models.items():
            models_form.addRow(stage.capitalize(), edit)
        self.effort = QComboBox()
        self.effort.addItem("(not sent)", "")
        for level in _EFFORTS[1:]:
            self.effort.addItem(level, level)
        models_form.addRow("Reasoning effort", self.effort)
        form.addRow(models_box)

        smtp_box = QGroupBox("SMTP relay")
        smtp_form = QFormLayout(smtp_box)
        self.smtp_host = QLineEdit()
        self.smtp_port = QSpinBox(); self.smtp_port.setRange(1, 65535)
        self.smtp_user = QLineEdit()
        self.smtp_sender = QLineEdit()
        smtp_form.addRow("Host", self.smtp_host)
        smtp_form.addRow("Port", self.smtp_port)
        smtp_form.addRow("User", self.smtp_user)
        smtp_form.addRow("Sender address", self.smtp_sender)
        # The password deliberately has no field here: it lives in .env on
        # the VPS and never crosses the API (G1 / D8).
        self.smtp_password_note = QLabel(
            "Password: set NEWS_SMTP_PASSWORD in .env on the server (never sent over the API)"
        )
        self.smtp_password_note.setWordWrap(True)
        smtp_form.addRow(self.smtp_password_note)

        user_box = QGroupBox("Fallback recipient")
        user_form = QFormLayout(user_box)
        self.user_email = QLineEdit()
        user_form.addRow("Email", self.user_email)
        self.api_key_note = QLabel()
        self.api_key_note.setWordWrap(True)
        user_form.addRow(self.api_key_note)

        root.addLayout(form)
        root.addWidget(smtp_box)
        root.addWidget(user_box)

        buttons = QHBoxLayout()
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self.refresh)
        save_btn = QPushButton("Save settings")
        save_btn.clicked.connect(self._save)
        buttons.addWidget(reload_btn)
        buttons.addWidget(save_btn)
        buttons.addStretch(1)
        root.addLayout(buttons)
        self._set_enabled(False)

    def set_client(self, client: ApiClient | None) -> None:
        self.client = client
        self._set_enabled(client is not None)
        if client is not None:
            self.refresh()

    def _set_enabled(self, enabled: bool) -> None:
        for child in iter_form_controls(self):
            child.setEnabled(enabled)

    def refresh(self) -> None:
        if self.client is None:
            return
        submit(self.client.get_settings, self._apply, show_error_with(self, "Load failed"))

    def _apply(self, data: dict[str, Any]) -> None:
        g = data.get("global", {})
        self.api_key_configured = bool(data.get("api_key_configured"))
        self.api_base_url.setText(g.get("api_base_url", ""))
        self.default_model.setText(g.get("default_model", ""))
        self.timezone.setText(g.get("timezone", ""))
        self.output_dir.setText(g.get("output_dir", ""))
        models = g.get("models", {})
        for stage, edit in self.stage_models.items():
            edit.setText(models.get(stage, ""))
        index = self.effort.findData(g.get("reasoning", {}).get("effort", ""))
        self.effort.setCurrentIndex(max(0, index))
        smtp = data.get("smtp", {})
        self.smtp_host.setText(smtp.get("host", ""))
        self.smtp_port.setValue(int(smtp.get("port", 25)))
        self.smtp_user.setText(smtp.get("user", ""))
        self.smtp_sender.setText(smtp.get("sender", ""))
        self.user_email.setText(data.get("user", {}).get("email", ""))
        state = "configured" if self.api_key_configured else "NOT SET"
        self.api_key_note.setText(f"API key: {state} (NEWS_API_KEY lives in .env on the server)")

    def _save(self) -> None:
        if self.client is None:
            return
        patch = {
            "global": {
                "api_base_url": self.api_base_url.text().strip(),
                "default_model": self.default_model.text().strip(),
                "timezone": self.timezone.text().strip(),
                "output_dir": self.output_dir.text().strip(),
                "models": {
                    stage: edit.text().strip()
                    for stage, edit in self.stage_models.items()
                },
                "reasoning": {"effort": self.effort.currentData() or ""},
            },
            "smtp": {
                "host": self.smtp_host.text().strip(),
                "port": self.smtp_port.value(),
                "user": self.smtp_user.text().strip(),
                "sender": self.smtp_sender.text().strip(),
            },
            "user": {"email": self.user_email.text().strip()},
        }
        submit(
            lambda: self.client.put_settings(patch),
            self._saved,
            show_error_with(self, "Saving settings failed"),
        )

    def _saved(self, data: dict[str, Any]) -> None:
        self._apply(data)
        QMessageBox.information(
            self, "newspipeline client", "Global settings saved.",
        )
