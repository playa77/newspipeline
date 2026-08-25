"""Guard tests for the localhost origin gate on state-changing requests
(ESC-2 hardening, human-decided 2026-08-25; retained behind the SSH tunnel
as defense-in-depth per DECISIONS.md D7).

Cross-site browser POSTs (drive-by CSRF) and DNS-rebinding Host headers
must be rejected; local tooling without an Origin header and tunneled
client requests must keep working.
"""

# version: 0.3.0

import pytest

from newspipeline.web.app import create_app

from conftest import make_interest, make_settings


TRIGGER = "/api/interests/nope/trigger"


@pytest.fixture
def client(isolated_config, store):
    app = create_app(store=store, scheduler=None)
    app.testing = True
    return app.test_client()


def test_cross_site_origin_rejected(client):
    resp = client.post(
        TRIGGER, json={}, headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_dns_rebinding_host_rejected_on_post(client):
    # Attacker resolves their domain to 127.0.0.1; the Host header betrays it.
    resp = client.post(TRIGGER, json={}, headers={"Host": "evil.example"})
    assert resp.status_code == 403


def test_dns_rebinding_host_rejected_on_get_too(client):
    # Reads are gated as well: a rebound page must not even SEE settings.
    resp = client.get("/api/settings", headers={"Host": "evil.example"})
    assert resp.status_code == 403


def test_origin_null_rejected(client):
    resp = client.post(TRIGGER, json={}, headers={"Origin": "null"})
    assert resp.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1.evil.com", "evil.com", "LOCALHOST.attacker.io"])
def test_lookalike_hosts_rejected(client, host):
    assert client.post(TRIGGER, json={}, headers={"Host": host}).status_code == 403


def test_ipv6_loopback_host_allowed(client):
    resp = client.post(TRIGGER, json={}, headers={"Host": "[::1]:8421"})
    assert resp.status_code == 503  # route ran; scheduler disabled


def test_uppercase_loopback_host_allowed(client):
    resp = client.post(TRIGGER, json={}, headers={"Host": "LOCALHOST:8421"})
    assert resp.status_code == 503  # route ran; scheduler disabled


def test_multiple_host_headers_fail_closed(client):
    # Werkzeug comma-joins duplicate Host headers; the combined value must
    # not slip through the membership check.
    resp = client.post(
        TRIGGER, json={},
        headers={"Host": "127.0.0.1, evil.example"},
    )
    assert resp.status_code == 403


def test_port_mismatched_loopback_origin_allowed(client):
    # A loopback Origin hostname means the page genuinely loaded from the
    # local UI (browsers forge neither Host nor Origin); port mismatches
    # between Origin and Host are therefore tolerated by design.
    resp = client.post(
        TRIGGER, json={},
        headers={"Origin": "http://localhost:9999", "Host": "127.0.0.1:8421"},
    )
    assert resp.status_code == 503  # route ran; scheduler disabled


def test_no_origin_header_allowed(client):
    # Local tooling (the desktop client over the tunnel) sends no Origin:
    # must reach the route.
    resp = client.post(TRIGGER, json={})
    assert resp.status_code == 503  # route ran; scheduler disabled
    assert resp.get_json()["error"]


def test_same_origin_delete_allowed(client, store, tmp_path):
    host = "127.0.0.1:8421"
    from newspipeline import config as cfg
    cfg.save_settings({
        **make_settings(1, tmp_path / "out"),
        "interests": [make_interest(1, ["file"], name="orig")],
    })
    resp = client.delete(
        "/api/interests/orig",
        headers={"Origin": f"http://{host}", "Host": host},
    )
    assert resp.status_code == 200
    assert cfg.load_settings()["interests"] == []


def test_get_requests_never_blocked(client):
    assert client.get("/api/settings").status_code == 200


def test_put_with_evil_origin_rejected(client):
    resp = client.put(
        "/api/settings", json={"global": {}},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
