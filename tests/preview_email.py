"""Render preview files for the email layout (dev aid, not a test).

Writes HTML + plain-text renderings of a sample theme report and a sample
multi-topic brief so the design can be eyeballed in a browser:

    python dev_preview.py [outdir]
"""

# version: 0.1.0

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from test_email_layout import BRIEF, THEME  # noqa: E402  (sample documents)

from newspipeline.email_layout import render_html, render_text  # noqa: E402


def main() -> None:
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/newspipeline-preview")
    outdir.mkdir(parents=True, exist_ok=True)
    for name, document in (("brief", BRIEF), ("theme", THEME)):
        (outdir / f"{name}.html").write_text(render_html(document), encoding="utf-8")
        (outdir / f"{name}.txt").write_text(render_text(document), encoding="utf-8")
    print(f"previews written to {outdir}")


if __name__ == "__main__":
    main()
