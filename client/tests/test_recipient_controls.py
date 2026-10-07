"""Recipient HTTP routing and sparse GUI edits in a headless desktop client."""

# version: 0.6.0

from copy import deepcopy
import httpx
import pytest
from newspipeline_client.api import ApiClient
from newspipeline_client.widgets.interests_tab import InterestsTab
from newspipeline_client.widgets.recipients_tab import RecipientsTab
from newspipeline_client.widgets.main_window import MainWindow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qt_app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def test_scoped_clients_keep_http_routes_independent():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"interests": [], "interest": {}, "overrides": {}, "feeds": [], "deliveries": []})
    root = ApiClient("http://127.0.0.1:8421")
    root._client.close()
    root._client = httpx.Client(base_url="http://127.0.0.1:8421", transport=httpx.MockTransport(respond))
    alice, bob = root.for_recipient("alice"), root.for_recipient("bob")
    alice.list_interests()
    bob.update_interest("Energy & space", {"target_summary_words": 100})
    root.list_interests()
    alice.get_status()
    bob.retry_delivery(4)
    assert requests[0].url.path == "/api/recipients/alice/interests"
    assert requests[1].url.path == "/api/recipients/bob/interests/Energy & space"
    assert requests[2].url.path == "/api/interests"
    assert requests[3].url.path == "/api/recipients/alice/status"
    assert requests[4].url.query == b"recipient=bob"
    alice.close()  # a scoped view must not close the shared transport
    bob.list_interests()
    root.close()


class FakeClient:
    recipient_id = "alice"
    def __init__(self):
        self.calls = []
    def update_interest(self, name, payload):
        self.calls.append((name, deepcopy(payload)))
        return {}
    def get_interest(self, name):
        return {"overrides": {"name": name, "target_summary_words": 800}}


def sync_submit(fn, success, error):
    success(fn())


def test_interest_save_only_overrides_changed_controls(qt_app, monkeypatch):
    monkeypatch.setattr("newspipeline_client.widgets.interests_tab.submit", sync_submit)
    tab = InterestsTab()
    tab.refresh = lambda: None
    tab.client = FakeClient()
    tab._show_interest({"name": "Energy", "enable_summary": True, "target_summary_words": 600,
                        "delivery": ["file"], "summary_detail": "concise", "delivery_schedule": {"start_time": "09:00"}})
    tab.deliverables["enable_summary"][1].setValue(250)
    tab._save()
    assert tab.client.calls == [("Energy", {"target_summary_words": 250})]
    tab._inherit()
    assert tab.client.calls[-1] == ("Energy", {"inherit": ["target_summary_words"]})
    tab.close()


def test_late_interest_response_cannot_replace_another_recipient(qt_app, monkeypatch):
    pending = []
    monkeypatch.setattr("newspipeline_client.widgets.interests_tab.submit", lambda fn, success, error: pending.append(success))
    alice, bob = FakeClient(), FakeClient()
    alice.list_interests = bob.list_interests = lambda: []
    bob.recipient_id = "bob"
    tab = InterestsTab()
    tab.set_client(alice)
    tab.set_client(bob)
    pending[0]([{"name": "Alice only"}])
    assert tab.list_widget.count() == 0
    pending[1]([{"name": "Bob only"}])
    assert tab.list_widget.item(0).data(Qt.UserRole)["name"] == "Bob only"
    tab.close()


def test_recipient_form_preserves_sparse_interests_and_parent(qt_app, monkeypatch):
    monkeypatch.setattr("newspipeline_client.widgets.recipients_tab.submit", sync_submit)
    calls = []
    client = FakeClient()
    client.update_recipient = lambda rid, payload: calls.append((rid, payload))
    tab = RecipientsTab()
    tab.client = client
    tab.refresh = lambda: None
    tab._populate(({}, [{"id": "alice", "parent": "parent", "interests": [{"name": "Energy", "feeds": []}]}]))
    tab.list.setCurrentRow(1)
    tab.inherit_email.setChecked(False)
    tab.email.setText("alice@example.com")
    tab._save()
    assert calls[0][1]["parent"] == "parent"
    assert calls[0][1]["interests"] == [{"name": "Energy", "feeds": []}]
    assert calls[0][1]["interest_defaults"] == {}
    assert calls[0][1]["email"] == "alice@example.com"
    tab.close()


def test_main_window_exposes_recipient_selection_and_delivery_controls(qt_app):
    window = MainWindow()
    assert window.tabs.count() == 4
    assert window.recipients_tab is not None
    assert window.interests_tab.delivery_interval.maximum() == 24
    assert window.history_tab.delivery_table.columnCount() == 5
    window.close()
