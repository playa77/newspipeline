"""Smoke test: package imports and scaffold-level invariants hold.

Guard tests for G1–G5 land in WP9 (see WORKPACKAGES.md).
"""

# version: 0.2.0

import newspipeline
from newspipeline import cli, config, emailer, fileout, llm, pipeline, scheduler, store
from newspipeline.web.app import create_app


def test_version():
    assert newspipeline.__version__ == "0.2.0"


def test_all_modules_importable():
    for module in (cli, config, emailer, fileout, llm, pipeline, scheduler, store):
        assert module.__name__.startswith("newspipeline")


def test_flask_app_creates():
    app = create_app()
    assert app is not None
