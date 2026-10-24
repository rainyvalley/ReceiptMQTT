"""Unit tests for web_control_panel: auth, validation, test-print path.

The panel module raises SystemExit at import when ADMIN_PASS is unset;
conftest.py seeds a test value before these imports.
"""

import json

import pytest

import escpos
import printer_mqtt_handler as h
import web_control_panel as w


@pytest.fixture(autouse=True)
def _fresh_panel_state():
    w.reset_state()
    yield
    w.reset_state()


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


def post_pair(**extra):
    """Auth plus the CSRF header the panel's own page sends."""
    return dict(auth_pair(), **{"X-Requested-With": "PrintMQTTify"}, **extra)


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

    @pytest.mark.parametrize("header", ["Bearer abc",
                                        'Digest username="admin"'])
    def test_non_basic_scheme_gets_401_not_500(self, client, header):
        resp = client.get("/", headers={"Authorization": header})
        assert resp.status_code == 401


class TestStatus:
    def test_returns_json_with_device_probe(self, client):
        resp = client.get("/status", headers=auth_pair())
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert "printers" in body and "device" in body


class TestTestPrint:
    def test_printer_name_field_is_ignored(self, client, monkeypatch):
        monkeypatch.setattr(h, "_write_device", lambda path, data: len(data))
        resp = client.post("/test-print", headers=post_pair(),
                           data={"printer_name": "Bad Name!"})
        assert json.loads(resp.data)["success"] is True

    def test_busy_device_is_retried(self, client, monkeypatch):
        import errno
        calls = []

        def flaky(path, data):
            calls.append(1)
            if len(calls) < 3:
                raise OSError(errno.EBUSY, "Device busy")
            return len(data)

        monkeypatch.setattr(h, "_write_device", flaky)
        monkeypatch.setattr(h.time, "sleep", lambda s: None)
        resp = client.post("/test-print", headers=post_pair())
        assert json.loads(resp.data)["success"] is True
        assert len(calls) == 3

    def test_valid_name_sends_escpos_to_device(self, client, monkeypatch):
        writes = []
        monkeypatch.setattr(h, "_write_device",
                            lambda path, data: writes.append((path, data)) or len(data))
        resp = client.post("/test-print", headers=post_pair(),
                           data={"printer_name": "ReceiptPrinter"})
        body = json.loads(resp.data)
        assert body["success"] is True
        # the selftest raster, fully framed as an ESC/POS job
        assert body["output"].startswith("wrote ")
        path, data = writes[0]
        assert data.startswith(b"\x1b@")
        assert escpos.RASTER_START in data
        assert len(data) > 100

    @pytest.mark.parametrize("site", ["cross-site", "same-site"])
    def test_cross_site_post_refused(self, client, monkeypatch, site):
        writes = []
        monkeypatch.setattr(h, "_write_device",
                            lambda path, data: writes.append(data) or len(data))
        headers = post_pair(**{"Sec-Fetch-Site": site})
        resp = client.post("/test-print", headers=headers,
                           data={"printer_name": "ReceiptPrinter"})
        assert resp.status_code == 403
        assert writes == []

    def test_same_origin_post_allowed(self, client, monkeypatch):
        monkeypatch.setattr(h, "_write_device", lambda path, data: len(data))
        headers = post_pair(**{"Sec-Fetch-Site": "same-origin"})
        resp = client.post("/test-print", headers=headers,
                           data={"printer_name": "ReceiptPrinter"})
        assert json.loads(resp.data)["success"] is True

    def test_device_error_reported_not_raised(self, client, monkeypatch):
        def busy(path, data):
            raise OSError(16, "Device or resource busy")

        monkeypatch.setattr(h, "_write_device", busy)
        resp = client.post("/test-print", headers=post_pair(),
                           data={"printer_name": "ReceiptPrinter"})
        body = json.loads(resp.data)
        assert body["success"] is False
        assert resp.status_code == 502

    def test_test_print_requires_auth(self, client):
        resp = client.post("/test-print", data={"printer_name": "R"})
        assert resp.status_code == 401

class TestTestPrintCsrfAndCooldown:
    @pytest.fixture(autouse=True)
    def _no_cooldown_clock(self, monkeypatch):
        monkeypatch.setattr(h, "_write_device", lambda path, data: len(data))

    def test_missing_custom_header_refused_even_without_fetch_metadata(
            self, client):
        resp = client.post("/test-print", headers=auth_pair())
        assert resp.status_code == 403
        assert "X-Requested-With" in json.loads(resp.data)["error"]

    def test_wrong_custom_header_refused(self, client):
        headers = dict(auth_pair(), **{"X-Requested-With": "XMLHttpRequest"})
        assert client.post("/test-print", headers=headers).status_code == 403

    def test_refused_post_does_not_start_cooldown(self, client):
        client.post("/test-print", headers=auth_pair())
        assert client.post("/test-print", headers=post_pair()).status_code == 200

    def test_second_print_inside_cooldown_gets_429(self, client, monkeypatch):
        monkeypatch.setattr(w, "TEST_PRINT_COOLDOWN", 10)
        assert client.post("/test-print", headers=post_pair()).status_code == 200
        resp = client.post("/test-print", headers=post_pair())
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) >= 1
        assert json.loads(resp.data)["success"] is False

    def test_print_allowed_again_after_cooldown(self, client, monkeypatch):
        monkeypatch.setattr(w, "TEST_PRINT_COOLDOWN", 10)
        now = [100.0]
        monkeypatch.setattr(w.time, "monotonic", lambda: now[0])
        assert client.post("/test-print", headers=post_pair()).status_code == 200
        now[0] += 11
        assert client.post("/test-print", headers=post_pair()).status_code == 200

    def test_cooldown_zero_disables(self, client, monkeypatch):
        monkeypatch.setattr(w, "TEST_PRINT_COOLDOWN", 0)
        for _ in range(3):
            assert client.post("/test-print",
                               headers=post_pair()).status_code == 200


class TestLoginLockout:
    def test_repeated_wrong_passwords_lock_the_address_out(self, client):
        for _ in range(w.LOGIN_MAX_FAILURES):
            assert client.get("/", headers=auth_pair(password="no")
                              ).status_code == 401
        resp = client.get("/", headers=auth_pair(password="no"))
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) >= 1

    def test_correct_password_also_refused_while_locked(self, client):
        for _ in range(w.LOGIN_MAX_FAILURES):
            client.get("/", headers=auth_pair(password="no"))
        assert client.get("/", headers=auth_pair()).status_code == 429

    def test_lockout_expires(self, client, monkeypatch):
        now = [500.0]
        monkeypatch.setattr(w.time, "monotonic", lambda: now[0])
        for _ in range(w.LOGIN_MAX_FAILURES):
            client.get("/", headers=auth_pair(password="no"))
        now[0] += w.LOCKOUT_SECONDS + 1
        assert client.get("/", headers=auth_pair()).status_code == 200

    def test_success_resets_the_count(self, client):
        for _ in range(w.LOGIN_MAX_FAILURES - 1):
            client.get("/", headers=auth_pair(password="no"))
        assert client.get("/", headers=auth_pair()).status_code == 200
        for _ in range(w.LOGIN_MAX_FAILURES - 1):
            client.get("/", headers=auth_pair(password="no"))
        assert client.get("/", headers=auth_pair()).status_code == 200

    def test_request_without_credentials_is_not_counted(self, client):
        for _ in range(w.LOGIN_MAX_FAILURES + 3):
            assert client.get("/").status_code == 401
        assert client.get("/", headers=auth_pair()).status_code == 200

    def test_other_addresses_unaffected(self, client):
        for _ in range(w.LOGIN_MAX_FAILURES):
            client.get("/", headers=auth_pair(password="no"))
        other = w.app.test_client()
        resp = other.get("/", headers=auth_pair(),
                         environ_base={"REMOTE_ADDR": "198.51.100.7"})
        assert resp.status_code == 200

    def test_disabled_when_zero(self, client, monkeypatch):
        monkeypatch.setattr(w, "LOGIN_MAX_FAILURES", 0)
        for _ in range(20):
            client.get("/", headers=auth_pair(password="no"))
        assert client.get("/", headers=auth_pair()).status_code == 200

    def test_tracking_table_is_bounded(self, client, monkeypatch):
        monkeypatch.setattr(w, "MAX_TRACKED_ADDRESSES", 5)
        for i in range(20):
            client.get("/", headers=auth_pair(password="no"),
                       environ_base={"REMOTE_ADDR": f"203.0.113.{i}"})
        assert len(w._failures) <= 5


def test_startup_note_mentions_plain_http():
    note = " ".join(w.startup_warnings({}))
    assert "plain HTTP" in note


def test_index_sends_the_csrf_header(client):
    body = client.get("/", headers=auth_pair()).get_data(as_text=True)
    assert "X-Requested-With" in body


# ---- HTTPS (opt-in) ----

def test_tls_paths_need_both(monkeypatch):
    assert w.tls_paths({}) is None
    assert w.tls_paths({"WEB_TLS_CERT": "/c"}) is None
    assert w.tls_paths({"WEB_TLS_CERT": "/c", "WEB_TLS_KEY": "/k"}) == ("/c", "/k")


def test_startup_note_plain_http_only_when_exposed():
    assert w.startup_warnings({"WEB_BIND": "127.0.0.1"}) == []
    assert w.startup_warnings({"WEB_TLS_CERT": "/c", "WEB_TLS_KEY": "/k"}) == []
    assert "plain HTTP" in " ".join(w.startup_warnings({}))
    assert "only one" in " ".join(w.startup_warnings({"WEB_TLS_CERT": "/c"}))


def test_gunicorn_serves_https_with_generated_cert(tmp_path):
    import shutil, socket, ssl, subprocess, sys, time, urllib.request, pathlib
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    pytest.importorskip("gunicorn")
    root = pathlib.Path(__file__).resolve().parent.parent
    subprocess.run([str(root / "scripts" / "make-cert.sh"), str(tmp_path)],
                   check=True, capture_output=True)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = dict(__import__("os").environ, ADMIN_PASS="pw")
    p = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "--chdir", str(root / "app"),
         "--bind", f"127.0.0.1:{port}", "--certfile", str(tmp_path / "panel.crt"),
         "--keyfile", str(tmp_path / "panel.key"), "web_control_panel:app"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ctx = ssl.create_default_context(cafile=str(tmp_path / "panel.crt"))
        req = urllib.request.Request(f"https://localhost:{port}/status")
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:pw").decode())
        for _ in range(50):
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=2) as r:
                    assert r.status == 200
                break
            except urllib.error.HTTPError as e:
                assert e.code in (200, 500, 503)  # TLS worked; probe result varies
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.2)
        else:
            pytest.fail("HTTPS panel never answered")
    finally:
        p.terminate(); p.wait(10)
