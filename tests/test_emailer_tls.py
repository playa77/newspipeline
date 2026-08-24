"""Guard tests for STARTTLS certificate verification on the authenticated
SMTP path (supports invariant G1's secret-handling posture; the SMTP auth
path itself is triggered invariant T2).

smtplib's default STARTTLS context does not verify certificates; these
tests pin that send_email always upgrades with a verifying context when
authenticating, and never issues STARTTLS on the unauthenticated local-relay
path.
"""

# version: 0.1.0

import ssl

from newspipeline import emailer


class _RecordingSMTP:
    """Stands in for smtplib.SMTP, recording an ordered event sequence."""

    instances: list["_RecordingSMTP"] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.starttls_contexts: list[ssl.SSLContext | None] = []
        self.logins: list[tuple[str, str]] = []
        self.events: list[str] = []
        _RecordingSMTP.instances.append(self)

    def __enter__(self) -> "_RecordingSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def starttls(self, context: ssl.SSLContext | None = None) -> None:
        self.starttls_contexts.append(context)
        self.events.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.logins.append((user, password))
        self.events.append("login")

    def send_message(self, msg: object) -> None:
        self.events.append("send")


def _send(monkeypatch, smtp_user: str) -> None:
    monkeypatch.setattr(emailer.smtplib, "SMTP", _RecordingSMTP)
    _RecordingSMTP.instances.clear()
    emailer.send_email(
        smtp_host="relay.example.org",
        smtp_port=587,
        smtp_user=smtp_user,
        smtp_password="secret-password",
        sender="pipe@example.org",
        recipient="user@example.org",
        subject="test",
        body="body",
    )


def test_authenticated_send_verifies_starttls(monkeypatch):
    _send(monkeypatch, smtp_user="pipe@example.org")
    (server,) = _RecordingSMTP.instances
    assert len(server.starttls_contexts) == 1
    ctx = server.starttls_contexts[0]
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    # Order matters: credentials must never travel before the channel is
    # upgraded with a verifying context.
    assert server.events == ["starttls", "login", "send"]
    assert server.logins == [("pipe@example.org", "secret-password")]


def test_unauthenticated_relay_skips_starttls(monkeypatch):
    _send(monkeypatch, smtp_user="")
    (server,) = _RecordingSMTP.instances
    assert server.starttls_contexts == []
    assert server.logins == []
    assert server.events == ["send"]
