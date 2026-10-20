"""Smoke test: package imports and scaffold-level invariants hold.

Guard tests for G1–G9 live in their dedicated modules (see DECISIONS.md).
"""

# version: 0.5.2

import newspipeline
from newspipeline import cli, config, emailer, fileout, llm, pipeline, scheduler, store
from newspipeline.web.app import create_app


def test_version():
    assert newspipeline.__version__ == "0.5.2"


def test_all_modules_importable():
    for module in (cli, config, emailer, fileout, llm, pipeline, scheduler, store):
        assert module.__name__.startswith("newspipeline")


def test_flask_app_creates(store):
    # The injectable-dependency pattern (D8): tests pass an isolated store
    # so the real data/news.db is never touched.
    app = create_app(store=store)
    assert app is not None
