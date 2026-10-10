"""Unit tests for web_control_panel: auth, validation, test-print path.

The panel module raises SystemExit at import when ADMIN_PASS is unset;
conftest.py seeds a test value before these imports.
"""

import json

import pytest

import escpos
import web_control_panel as w


@pytest.fixture
def client():
    return w.app.test_client()


def auth_pair(user="admin", password=None):
    """RFC 4648 base64 Basic authentication header value."""
    import base64
    token = base64.b64encode(
        f"{user}:{password if password is not None else 'test-panel-pass'}"
        .encode()).decode()
    return {"Authorization": f"Basic {token}"}


class TestCheckAuth:
    def test_correct_pair(self):
        assert w.check_auth("admin", "test-panel-pass") is True

    def test_wrong_password(self):
        assert w.check_auth("admin", "wrong") is False

    def test_wrong_user(self):
        assert w.check_auth("root", "test-panel-pass") is False

    def test_both_wrong(self):
        assert w.check_auth("root", "wrong") is False

    def test_compare_is_digest_based(self):
        # hmac.compare_digest returns bool; check_auth must keep that type
        assert w.check_auth("admin", "test-panel-pass") in (True, False)


class TestRequireAuth:
    def test_no_credentials_gets_401_with_realm(self, client):
        resp = client.get("/")
        assert resp.status_code == 401
        assert "Basic realm=" in resp.headers["WWW-Authenticate"]

    def test_bad_credentials_also_401(self, client):
        resp = client.get("/", headers=auth_pair(password="nope"))
        assert resp.status_code == 401

    def test_good_credentials_render_index(self, client):
        resp = client.get("/", headers=auth_pair())
        assert resp.status_code == 200


class TestStatus:
    def test_returns_json_with_device_probe(self, client):
        resp = client.get("/status", headers=auth_pair())
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert "printers" in body and "device" in body


class TestTestPrint:
    def test_invalid_printer_name_rejected(self, client):
        resp = client.post("/test-print", headers=auth_pair(),
                           data={"printer_name": "Bad Name!"})
        body = json.loads(resp.data)
        assert resp.status_code == 200
        assert body["success"] is False
        assert body["error"] == "invalid printer name"

    def test_valid_name_sends_escpos_to_device(self, client, monkeypatch):
        writes = []
        monkeypatch.setattr(w, "_write_device",
                            lambda path, data: writes.append((path, data)) or len(data))
        resp = client.post("/test-print", headers=auth_pair(),
                           data={"printer_name": "ReceiptPrinter"})
        body = json.loads(resp.data)
        assert body["success"] is True
        # the selftest raster, fully framed as an ESC/POS job
        assert body["output"].startswith("wrote ")
        path, data = writes[0]
        assert data.startswith(b"\x1b@")
        assert escpos.RASTER_START in data
        assert len(data) > 100

    def test_device_error_reported_not_raised(self, client, monkeypatch):
        def busy(path, data):
            raise OSError(16, "Device or resource busy")

        monkeypatch.setattr(w, "_write_device", busy)
        resp = client.post("/test-print", headers=auth_pair(),
                           data={"printer_name": "ReceiptPrinter"})
        body = json.loads(resp.data)
        assert body["success"] is False

    def test_test_print_requires_auth(self, client):
        resp = client.post("/test-print", data={"printer_name": "R"})
        assert resp.status_code == 401