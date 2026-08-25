"""Application entry point (`newspipeline-client`).

Headless-safe import order: nothing GUI-related loads until main() runs,
so the Qt-free modules (api, tunnel, profile) stay usable for checks.
"""

# version: 0.3.0

from __future__ import annotations

import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def main() -> int:
    _setup_logging()
    from PySide6.QtWidgets import QApplication

    from .widgets.main_window import MainWindow

    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1150, 760)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
