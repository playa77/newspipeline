"""Connection profile persistence (no secrets — DECISIONS.md D7).

The profile holds only routing information: VPS hostname, SSH user, and
the server's API port on the VPS. Authentication is your existing SSH
key; the tunnel is the trust boundary, so no tokens or passwords are
stored here, ever.
"""

# version: 0.3.0

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def profile_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(root) / "newspipeline-client"


def profile_path() -> Path:
    return profile_dir() / "profile.json"


@dataclass
class Profile:
    host: str = ""
    user: str = ""
    port: int = 8421  # server-side API port on the VPS

    @classmethod
    def load(cls) -> "Profile":
        path = profile_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                host=str(data.get("host", "")),
                user=str(data.get("user", "")),
                port=int(data.get("port", 8421)),
            )
        except (ValueError, OSError):
            logger.warning("Unreadable profile at %s — starting fresh", path)
            return cls()

    def save(self) -> None:
        path = profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        logger.info("Profile saved to %s", path)
