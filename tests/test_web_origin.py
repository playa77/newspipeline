"""Guard tests for the localhost origin gate on state-changing requests
(ESC-2 hardening, human-decided 2026-08-25).

Cross-site browser POSTs (drive-by CSRF) and DNS-rebinding Host headers
must be rejected; local tooling without an Origin header and same-origin
UI posts must keep working.
"""

# version: 0.2.0

import pytest

from newspipeline.web.app import create_app

from conftest import make_interest, make_settings


@pytest.fixture
def client(isolated_config, store):
    app = create_app(store=store, scheduler=None)
    app.testing = True
    return app.test_client()


def test_cross_site_origin_rejected(client):
    resp = client.post(
        "/run/whatever", data={}, headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_dns_rebinding_host_rejected_on_post(client):
    # Attacker resolves their domain to 127.0.0.1; the Host header betrays it.
    resp = client.post("/run/whatever", data={}, headers={"Host": "evil.example"})
    assert resp.status_code == 403


def test_dns_rebinding_host_rejected_on_get_too(client):
    # Reads are gated as well: a rebound page must not even SEE settings.
    resp = client.get("/", headers={"Host": "evil.example"})
    assert resp.status_code == 403


def test_origin_null_rejected(client):
    resp = client.post("/run/whatever", data={}, headers={"Origin": "null"})
    assert resp.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1.evil.com", "evil.com", "LOCALHOST.attacker.io"])
def test_lookalike_hosts_rejected(client, host):
    assert client.post("/run/x", data={}, headers={"Host": host}).status_code == 403


def test_ipv6_loopback_host_allowed(client):
    resp = client.post("/run/nope", data={}, headers={"Host": "[::1]:8421"})
    assert resp.status_code == 302


def test_uppercase_loopback_host_allowed(client):
    resp = client.post("/run/nope", data={}, headers={"Host": "LOCALHOST:8421"})
    assert resp.status_code == 302


def test_multiple_host_headers_fail_closed(client):
    # Werkzeug comma-joins duplicate Host headers; the combined value must
    # not slip through the membership check.
    resp = client.post(
        "/run/x", data={},
        headers={"Host": "127.0.0.1, evil.example"},
    )
    assert resp.status_code == 403


def test_port_mismatched_loopback_origin_allowed(client):
    # A loopback Origin hostname means the page genuinely loaded from the
    # local UI (browsers forge neither Host nor Origin); port mismatches
    # between Origin and Host are therefore tolerated by design.
    resp = client.post(
        "/run/nope", data={},
        headers={"Origin": "http://localhost:9999", "Host": "127.0.0.1:8421"},
    )
    assert resp.status_code == 302


def test_no_origin_header_allowed(client, store):
    # Local tooling (curl, scripts) sends no Origin: must reach the route.
    resp = client.post("/run/nope", data={})
    assert resp.status_code == 302  # route ran; flashes "scheduler not running"


def test_same_origin_post_allowed(client, store, tmp_path):
    host = "127.0.0.1:8421"
    from newspipeline import config as cfg
    cfg.save_settings(make_settings(1, tmp_path / "out"))
    cfg.save_settings({
        **make_settings(1, tmp_path / "out"),
        "interests": [make_interest(1, ["file"], name="orig")],
    })
    resp = client.post(
        "/interests/orig/delete",
        data={},
        headers={"Origin": f"http://{host}", "Host": host},
    )
    assert resp.status_code == 302
    assert cfg.load_settings()["interests"] == []


def test_get_requests_never_blocked(client):
    assert client.get("/").status_code == 200
