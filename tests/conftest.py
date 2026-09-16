"""Shared fixtures for the guard-test suite (WP9, invariants G1–G5).

- ``isolated_config`` redirects config.SETTINGS_PATH / config.ENV_PATH to a
  tmp dir — the real settings.toml and .env are never touched by tests.
- ``fixture_server`` serves a fixture RSS feed, article HTML pages, and a
  mock OpenAI-compatible /v1/chat/completions endpoint from one local HTTP
  server, capturing every LLM request for inspection.
- ``smtp_server`` is a minimal fake SMTP server capturing messages (the
  unauthenticated path only; authenticated SMTP is T2, untestable locally).
"""

# version: 0.2.1

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from newspipeline import config
from newspipeline.store import Store

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Fixture Feed</title>
    <item>
      <title>Article One</title>
      <link>http://127.0.0.1:PORT/a1</link>
      <pubDate>Sat, 08 Aug 2026 06:00:00 +0000</pubDate>
      <description>First fixture article excerpt.</description>
    </item>
    <item>
      <title>Article Two</title>
      <link>http://127.0.0.1:PORT/a2</link>
      <pubDate>Sat, 08 Aug 2026 07:30:00 +0000</pubDate>
      <description>Second fixture article excerpt.</description>
    </item>
  </channel>
</rss>
"""

ARTICLE_HTML = """<html><body><article>
<p>Fixture article body about renewable energy adoption, grid storage,
and the policy shifts driving investment in European energy markets.</p>
</article></body></html>"""

THEMES = [
    {"title": "Renewable Energy Surge", "description": "Investment in renewables accelerates.", "article_indices": [1, 2]},
    {"title": "Grid Storage Policy", "description": "New policy shifts storage economics.", "article_indices": [1]},
]
THEMES_JSON = json.dumps(THEMES)


# ---- fixture HTTP server (feed + articles + mock LLM) ------------------------

class _FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/feed.xml":
            body = FEED_XML.replace("PORT", str(self.server.server_port)).encode()
            ctype = "application/rss+xml"
        elif self.path in ("/a1", "/a2"):
            body = ARTICLE_HTML.encode()
            ctype = "text/html"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self.server.captured_llm_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        # Analysis prompts ask to identify "major themes" and return an
        # "article_indices" array; everything else (summary, scripts, brief)
        # gets plain generated text.
        if "article_indices" in body["messages"][-1]["content"]:
            content = THEMES_JSON
        else:
            content = f"Generated fixture content for model {body.get('model')}."
        payload = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content}}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.captured_llm_requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


# ---- fake SMTP server --------------------------------------------------------

class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 fake ESMTP\r\n")
        rcpt: list[str] = []
        data_lines: list[bytes] = []
        in_data = False
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if in_data:
                if line in (b".\r\n", b".\n"):
                    in_data = False
                    self.server.captured_emails.append({
                        "rcpt": list(rcpt),
                        "data": b"".join(data_lines).decode(),
                    })
                    self.wfile.write(b"250 queued\r\n")
                else:
                    data_lines.append(line)
                continue
            text = line.decode(errors="replace").strip()
            cmd = text.split(" ", 1)[0].upper()
            if cmd in ("EHLO", "HELO"):
                self.wfile.write(b"250-fake\r\n250 OK\r\n")
            elif cmd == "MAIL":
                rcpt.clear()
                self.wfile.write(b"250 OK\r\n")
            elif cmd == "RCPT":
                rcpt.append(text.split(":", 1)[1].strip().strip("<>"))
                self.wfile.write(b"250 OK\r\n")
            elif cmd == "DATA":
                in_data = True
                data_lines.clear()
                self.wfile.write(b"354 go ahead\r\n")
            elif cmd == "RSET":
                rcpt.clear()
                self.wfile.write(b"250 OK\r\n")
            elif cmd == "STARTTLS":
                # Refuse loudly: this fake never negotiates TLS, and a test
                # routing the authenticated path here must fail clearly.
                self.wfile.write(b"454 TLS not available\r\n")
            elif cmd == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                break
            else:
                self.wfile.write(b"250 OK\r\n")


@pytest.fixture
def smtp_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    server.daemon_threads = True
    server.captured_emails = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


# ---- config / store fixtures -------------------------------------------------

@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect settings.toml and .env to tmp; clear secret env vars."""
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.toml")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("NEWS_API_KEY", raising=False)
    monkeypatch.delenv("NEWS_SMTP_PASSWORD", raising=False)
    return config


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "news.db")
    yield s
    s.close()


# ---- settings / interest factories -------------------------------------------

def make_settings(http_port: int, output_dir: Path, **overrides: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "global": {
            "api_base_url": f"http://127.0.0.1:{http_port}/v1",
            "default_model": "fixture/default-model",
            "timezone": "Europe/Berlin",
            "output_dir": str(output_dir),
            "models": {"analysis": "", "generation": "", "brief": ""},
            "reasoning": {"effort": ""},
        },
        "smtp": {"host": "localhost", "port": 25, "user": "", "sender": "newspipeline@test.local"},
        "user": {"email": ""},
        "interests": [],
    }
    for key, value in overrides.items():
        settings[key].update(value)
    return settings


def make_interest(http_port: int, delivery: list[str], **overrides: Any) -> dict[str, Any]:
    interest: dict[str, Any] = {
        "name": "guard-fixture",
        "start_time": "04:00",
        "interval_hours": 24,
        "max_themes": 5,
        "enable_summary": True,
        "enable_script": True,
        "enable_script_de": True,
        "enable_brief": True,
        "delivery": delivery,
        "email_recipient": "",
        "input_data_length_mode": "full_article",
        "feeds": [{"name": "Fixture Feed", "url": f"http://127.0.0.1:{http_port}/feed.xml", "category": "news"}],
    }
    interest.update(overrides)
    return interest
