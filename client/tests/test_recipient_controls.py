"""Recipient HTTP routing and sparse GUI edits in a headless desktop client."""

# version: 0.6.1

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
    try:
        result = fn()
    except Exception as exc:
        error(str(exc))
    else:
        success(result)


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


def test_restore_one_override_preserves_other_preferences(qt_app, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr("newspipeline_client.widgets.interests_tab.submit", sync_submit)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kwargs: ("target_summary_words", True))
    tab = InterestsTab()
    tab.client = FakeClient()
    tab.refresh = lambda: None
    tab._show_interest({"name": "Energy"})
    tab._inherit_field()
    assert tab.client.calls == [("Energy", {"inherit": ["target_summary_words"]})]
    tab.close()


def test_reconnect_discards_late_recipient_list(qt_app, monkeypatch):
    pending = []
    monkeypatch.setattr("newspipeline_client.widgets.main_window.submit", lambda fn, success, error: pending.append(success))
    window = MainWindow()
    alice, bob = FakeClient(), FakeClient()
    alice.list_recipients = bob.list_recipients = lambda: []
    window.client = alice
    window._refresh_recipients()
    window.client = bob
    window._refresh_recipients()
    window._select_recipient = lambda: None
    pending[0]([{"id": "old-server"}])
    assert window.recipient_combo.findData("old-server") == -1
    pending[1]([{"id": "new-server"}])
    assert window.recipient_combo.findData("new-server") >= 0
    window.client = None
    window.close()


def test_deliveries_show_zero_attempts_and_only_retry_failed(qt_app, monkeypatch):
    from newspipeline_client.widgets.history_tab import HistoryTab
    monkeypatch.setattr("newspipeline_client.widgets.history_tab.submit", sync_submit)
    tab = HistoryTab()
    tab.client = FakeClient()
    calls = []
    tab.client.retry_delivery = calls.append
    tab.refresh = lambda: None
    tab._apply({"deliveries": [{"run_id": 8, "status": "pending", "attempts": 0}]})
    assert tab.delivery_table.item(0, 3).text() == "0"
    tab.delivery_table.setCurrentCell(0, 0)
    tab._retry_delivery()
    assert calls == []
    tab.delivery_table.item(0, 2).setText("failed")
    tab._retry_delivery()
    assert calls == [8]
    tab.close()


def test_complete_editor_shared_templates_propagate_and_recipient_restore_is_scoped(qt_app, monkeypatch, tmp_path):
    """Drive the real Qt editor through the HTTP client into the Flask API."""
    import json
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit
    from newspipeline import config
    from newspipeline.store import Store
    from newspipeline.web.app import create_app
    state = {"recipients": [{"id": "alice"}, {"id": "bob", "parent": "alice"}]}
    monkeypatch.setattr(config, "load_settings", lambda: deepcopy(state))
    def persist(data):
        state.clear()
        state.update(deepcopy(data))
    monkeypatch.setattr(config, "save_settings", persist)
    store = Store(tmp_path / "news.db")
    server = create_app(store=store).test_client()
    def respond(request):
        response = server.open(request.url.raw_path.decode(), method=request.method,
                               data=request.content, content_type="application/json",
                               base_url="http://127.0.0.1:8421")
        return httpx.Response(response.status_code, content=response.data,
                              headers={"content-type": "application/json"})
    client = ApiClient("http://127.0.0.1:8421")
    client._client.close()
    client._client = httpx.Client(base_url="http://127.0.0.1:8421", transport=httpx.MockTransport(respond))
    monkeypatch.setattr("newspipeline_client.widgets.recipients_tab.submit", sync_submit)
    tab = RecipientsTab()
    tab.set_client(client)
    desired = {"interest_defaults": {"max_themes": 7, "target_summary_words": 300},
               "interests": [{"name": "Energy", "feeds": [{"name": "RSS", "url": "https://example.com/feed"}]}]}
    def edit(dialog):
        editor = dialog.findChild(QPlainTextEdit)
        buttons = dialog.findChild(QDialogButtonBox)
        before = deepcopy(state)
        editor.setPlainText("invalid JSON")
        buttons.accepted.emit()
        assert state == before
        editor.setPlainText(json.dumps({"NEWS_API_KEY": "forbidden"}))
        buttons.accepted.emit()
        assert state == before
        assert buttons.button(QDialogButtonBox.Save).isEnabled()
        editor.setPlainText(json.dumps(desired))
        buttons.accepted.emit()
        assert dialog.result() == QDialog.Accepted
        return dialog.result()
    monkeypatch.setattr(QDialog, "exec", edit)
    tab._edit_configuration()
    alice, bob = client.for_recipient("alice"), client.for_recipient("bob")
    assert alice.list_interests()[0]["max_themes"] == 7
    assert bob.list_interests()[0]["target_summary_words"] == 300
    bob.update_interest("Energy", {"target_summary_words": 900})
    bob.delete_interest("Energy")
    assert bob.list_interests() == []
    tab.list.setCurrentRow(2)
    desired = deepcopy(state["recipients"][1])
    desired["interests"][0]["enabled"] = True
    tab._edit_configuration()
    assert bob.list_interests()[0]["target_summary_words"] == 900
    assert alice.list_interests()[0]["target_summary_words"] == 300
    assert bob.list_interests()[0]["feeds"][0]["name"] == "RSS"
    assert state["recipients"][1]["parent"] == "alice"
    tab.close()
    client.close()
    store.close()


def test_global_settings_cannot_use_old_server_values_after_reconnect(qt_app, monkeypatch):
    from newspipeline_client.widgets.settings_tab import SettingsTab
    pending = []
    monkeypatch.setattr("newspipeline_client.widgets.settings_tab.submit", lambda fn, success, error: pending.append(success))
    old, new = FakeClient(), FakeClient()
    old.get_settings = new.get_settings = lambda: {}
    tab = SettingsTab()
    tab.set_client(old)
    tab.set_client(new)
    assert not tab.save_button.isEnabled()
    pending[0]({"global": {"default_model": "old-server"}})
    assert tab.default_model.text() == ""
    pending[1]({"global": {"default_model": "new-server"}})
    assert tab.default_model.text() == "new-server"
    assert tab.save_button.isEnabled()
    tab.set_client(None)
    assert not tab.save_button.isEnabled()
    tab.close()
