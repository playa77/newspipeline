"""Main window: connection bar (tunnel lifecycle + visible state) and the
three management tabs."""

# version: 0.3.0

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiClient
from ..profile import Profile
from ..tunnel import TunnelManager
from .history_tab import HistoryTab
from .interests_tab import InterestsTab
from .settings_tab import SettingsTab

logger = logging.getLogger(__name__)

_STATE_STYLES = {
    "disconnected": ("Disconnected", "#666666"),
    "connecting": ("Connecting…", "#a05a00"),
    "connected": ("Connected", "#1c7d34"),
    "error": ("Error", "#b2182b"),
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("newspipeline client")
        self.profile = Profile.load()
        self.tunnel = TunnelManager(self._on_tunnel_state, self._on_tunnel_error)
        self.client: ApiClient | None = None

        central = QWidget()
        layout = QVBoxLayout(central)

        # ---- connection bar -------------------------------------------------
        bar = QFormLayout()
        row = QHBoxLayout()
        self.host_edit = QLineEdit(self.profile.host)
        self.host_edit.setPlaceholderText("VPS hostname or IP")
        self.user_edit = QLineEdit(self.profile.user)
        self.user_edit.setPlaceholderText("SSH user")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.profile.port)
        self.port_spin.setToolTip("Server API port on the VPS (default 8421)")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._disconnect)
        self.disconnect_btn.setEnabled(False)
        row.addWidget(self.host_edit, 2)
        row.addWidget(self.user_edit, 1)
        row.addWidget(self.port_spin, 0)
        row.addWidget(self.connect_btn)
        row.addWidget(self.disconnect_btn)
        container = QWidget()
        container.setLayout(row)
        bar.addRow("Server (host · user · port)", container)

        self.state_label = QLabel("Disconnected")
        self.state_label.setAlignment(Qt.AlignCenter)
        self._set_state_style("disconnected")
        bar.addRow("Connection", self.state_label)
        layout.addLayout(bar)

        # ---- tabs -------------------------------------------------------------
        self.tabs = QTabWidget()
        self.settings_tab = SettingsTab()
        self.interests_tab = InterestsTab()
        self.history_tab = HistoryTab()
        self.tabs.addTab(self.settings_tab, "Global settings")
        self.tabs.addTab(self.interests_tab, "Interests")
        self.tabs.addTab(self.history_tab, "Run history")
        self.tabs.setEnabled(False)
        layout.addWidget(self.tabs, 1)

        # Interactions that add runs should refresh the history view.
        self.interests_tab.data_changed.connect(self.history_tab.refresh)
        self.interests_tab.triggered.connect(lambda _name: self.history_tab.refresh())

        self.setCentralWidget(central)

    # ---- connection lifecycle ------------------------------------------------

    def _connect(self) -> None:
        host = self.host_edit.text().strip()
        user = self.user_edit.text().strip()
        if not host or not user:
            QMessageBox.warning(
                self, "newspipeline client",
                "Both host and SSH user are required.",
            )
            return
        self.profile.host, self.profile.user = host, user
        self.profile.port = self.port_spin.value()
        self.profile.save()  # routing info only — no secrets (D7)
        self.host_edit.setEnabled(False)
        self.user_edit.setEnabled(False)
        self.port_spin.setEnabled(False)
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        try:
            self.tunnel.start(host, user, self.profile.port)
        except RuntimeError as exc:
            self._reset_connection_controls()
            QMessageBox.critical(self, "newspipeline client", str(exc))

    def _disconnect(self) -> None:
        logger.info("Disconnect requested")
        self.tunnel.stop()
        self._teardown_client()

    def _on_tunnel_state(self, state: str, *args) -> None:
        if state == "connected":
            local_port = args[0]
            self.client = ApiClient(f"http://127.0.0.1:{local_port}")
            self.settings_tab.set_client(self.client)
            self.interests_tab.set_client(self.client)
            self.history_tab.set_client(self.client)
            self.tabs.setEnabled(True)
            self.state_label.setText(
                f"Connected — server reachable via 127.0.0.1:{local_port}"
            )
            self._set_state_style("connected")
        elif state == "connecting":
            self.state_label.setText("Connecting…")
            self._set_state_style("connecting")
        else:  # disconnected
            self._teardown_client()
            self.state_label.setText("Disconnected")
            self._set_state_style("disconnected")

    def _on_tunnel_error(self, message: str) -> None:
        logger.error("Tunnel error: %s", message)
        self._teardown_client()
        self.state_label.setText("Error — see message")
        self._set_state_style("error")
        QMessageBox.critical(self, "Connection problem", message)
        self._reset_connection_controls()

    def _teardown_client(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        self.settings_tab.set_client(None)
        self.interests_tab.set_client(None)
        self.history_tab.set_client(None)
        self.tabs.setEnabled(False)

    def _reset_connection_controls(self) -> None:
        self.host_edit.setEnabled(True)
        self.user_edit.setEnabled(True)
        self.port_spin.setEnabled(True)
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)

    def _set_state_style(self, state: str) -> None:
        _text, color = _STATE_STYLES[state]
        self.state_label.setStyleSheet(
            f"color: white; background-color: {color}; padding: 4px;"
            "border-radius: 4px; font-weight: bold;"
        )

    # ---- window lifecycle ------------------------------------------------------

    def closeEvent(self, event) -> None:
        # Clean tunnel teardown on exit — also on crash paths via __del__
        # of the subprocess handlers; explicit stop keeps ssh from leaking.
        self.tunnel.stop()
        super().closeEvent(event)
