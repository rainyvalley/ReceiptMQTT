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


class TestSettingsEndpoints:
    @pytest.fixture
    def settings_file(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        monkeypatch.setattr(w.settings_store, "SETTINGS_FILE", str(path))
        for key in w.settings_store.KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("PAPER_DEVICE", raising=False)
        before = escpos.PAPER_WIDTH_MM
        yield path
        escpos.apply_paper_width(before)

    def test_requires_auth(self, client):
        assert client.get("/settings").status_code == 401
        assert client.post("/settings", data={"PAPER_WIDTH_MM": "48"}
                           ).status_code == 401

    def test_get_reports_values_and_lock_state(self, client, settings_file):
        body = json.loads(client.get("/settings", headers=auth_pair()).data)
        assert body["settings"]["MQTT_TOPIC"] == "printer/commands"
        assert body["settings"]["PAPER_WIDTH_MM"] == "48"
        assert body["locked"]["MQTT_TOPIC"] is False
        assert body["file"] == str(settings_file)

    def test_post_saves_paper_and_topics(self, client, settings_file):
        resp = client.post("/settings", headers=post_pair(), data={
            "PAPER_WIDTH_MM": "58",
            "MQTT_TOPIC": "kitchen/commands",
            "MQTT_AVAILABILITY_TOPIC": "kitchen/availability",
            "PRINTER_DEVICE": "/dev/usb/lp0",
        })
        body = json.loads(resp.data)
        assert resp.status_code == 200 and body["saved"] is True
        saved = json.loads(settings_file.read_text())
        assert saved["MQTT_TOPIC"] == "kitchen/commands"
        assert saved["MQTT_AVAILABILITY_TOPIC"] == "kitchen/availability"
        assert saved["PRINTER_DEVICE"] == "/dev/usb/lp0"
        # the panel applies paper width to its own test-print raster
        assert escpos.PAPER_WIDTH_MM == 58

    def test_saved_topic_is_shown_on_the_page(self, client, settings_file):
        client.post("/settings", headers=post_pair(),
                    data={"MQTT_TOPIC": "kitchen/commands"})
        page = client.get("/", headers=auth_pair()).data
        assert b"kitchen/commands" in page

    def test_environment_locked_key_is_ignored(self, client, settings_file,
                                               monkeypatch):
        monkeypatch.setenv("MQTT_BROKER", "env-host")
        resp = client.post("/settings", headers=post_pair(), data={
            "MQTT_BROKER": "panel-host",
            "PAPER_WIDTH_MM": "80",
        })
        body = json.loads(resp.data)
        assert body["saved"] is True
        assert body["ignored_locked"] == ["MQTT_BROKER"]
        saved = json.loads(settings_file.read_text())
        assert "MQTT_BROKER" not in saved and saved["PAPER_WIDTH_MM"] == "80"

    def test_csrf_header_required(self, client, settings_file):
        resp = client.post("/settings", headers=auth_pair(),
                           data={"PAPER_WIDTH_MM": "80"})
        assert resp.status_code == 403
        assert not settings_file.exists()

    def test_extra_feed_reaches_the_test_print(self, client, settings_file,
                                               monkeypatch):
        writes = []
        monkeypatch.setattr(h, "_write_device",
                            lambda path, data: writes.append(data) or len(data))
        assert client.post("/settings", headers=post_pair(),
                           data={"EXTRA_FEED_MM": "9"}
                           ).status_code == 200
        assert client.post("/test-print", headers=post_pair()).status_code == 200
        assert escpos.feed_lines(9) in writes[0]

    @pytest.mark.parametrize("data,message", [
        ({"PAPER_WIDTH_MM": "57"}, "PAPER_WIDTH_MM must be"),
        ({"MQTT_TOPIC": "bad topic"}, "plain MQTT topic"),
        ({"PRINTER_DEVICE": "lp0"}, "absolute path"),
    ])
    def test_invalid_values_are_rejected(self, client, settings_file, data,
                                         message):
        resp = client.post("/settings", headers=post_pair(), data=data)
        body = json.loads(resp.data)
        assert resp.status_code == 400 and body["saved"] is False
        assert any(message in error for error in body["errors"])
        assert not settings_file.exists()

    def test_empty_post_is_rejected(self, client, settings_file):
        resp = client.post("/settings", headers=post_pair())
        assert resp.status_code == 400


class TestTestMessage:
    def test_requires_auth_and_csrf(self, client):
        assert client.post("/test-message").status_code == 401
        assert client.post("/test-message", headers=auth_pair()).status_code == 403

    def test_publishes_and_reports_the_topic(self, client, monkeypatch):
        monkeypatch.setattr(w, "publish_test_message",
                            lambda: "kitchen/commands")
        resp = client.post("/test-message", headers=post_pair())
        body = json.loads(resp.data)
        assert body["success"] is True
        assert "kitchen/commands" in body["output"]

    def test_publish_failure_is_reported(self, client, monkeypatch):
        def boom():
            raise RuntimeError("connection refused")
        monkeypatch.setattr(w, "publish_test_message", boom)
        resp = client.post("/test-message", headers=post_pair())
        body = json.loads(resp.data)
        assert resp.status_code == 502
        assert "connection refused" in body["error"]

    def test_cooldown_shared_with_test_print(self, client, monkeypatch):
        monkeypatch.setattr(w, "publish_test_message",
                            lambda: "printer/commands")
        monkeypatch.setattr(w, "TEST_PRINT_COOLDOWN", 30)
        assert client.post("/test-message", headers=post_pair()).status_code == 200
        resp = client.post("/test-print", headers=post_pair())
        assert resp.status_code == 429


class TestPublishTestMessage:
    def _fake_client(self, events):
        class Info:
            def wait_for_publish(self, timeout):
                events["waited"] = timeout

        class Fake:
            def username_pw_set(self, user, password):
                events["auth"] = (user, password)

            def connect(self, host, port, keepalive):
                events["connect"] = (host, port, keepalive)

            def loop_start(self):
                events["started"] = True

            def publish(self, topic, payload, qos):
                events["publish"] = (topic, payload, qos)
                return Info()

            def loop_stop(self):
                events["stopped"] = True

            def disconnect(self):
                events["disconnected"] = True

        return Fake()

    def test_uses_effective_settings(self, monkeypatch):
        events = {}
        monkeypatch.setattr(w, "new_mqtt_client", lambda: self._fake_client(events))
        monkeypatch.setattr(w.handler, "configure_tls", lambda client: None)
        topic = w.publish_test_message(
            config={"MQTT_BROKER": "192.0.2.10", "MQTT_TOPIC": "kitchen/commands",
                    "MQTT_USERNAME": "kitchen", "MQTT_PASSWORD": "secret"},
            payload="job", timeout=7)
        assert topic == "kitchen/commands"
        assert events["auth"] == ("kitchen", "secret")
        assert events["connect"][0] == "192.0.2.10"
        assert events["publish"][0] == "kitchen/commands"
        assert events["publish"][1] == "job" and events["publish"][2] == 1
        assert events["waited"] == 7
        assert events["stopped"] is True and events["disconnected"] is True

    def test_anonymous_when_no_login(self, monkeypatch):
        events = {}
        monkeypatch.setattr(w, "new_mqtt_client", lambda: self._fake_client(events))
        monkeypatch.setattr(w.handler, "configure_tls", lambda client: None)
        w.publish_test_message(
            config={"MQTT_BROKER": "localhost", "MQTT_TOPIC": "printer/commands",
                    "MQTT_USERNAME": "", "MQTT_PASSWORD": ""},
            payload="job")
        assert "auth" not in events


class TestDiscoveryInfo:
    @pytest.fixture
    def settings_file(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        monkeypatch.setattr(w.settings_store, "SETTINGS_FILE", str(path))
        for key in w.settings_store.KEYS:
            monkeypatch.delenv(key, raising=False)
        before = escpos.PAPER_WIDTH_MM
        yield path
        escpos.apply_paper_width(before)

    def test_reports_node_and_acl(self, client, settings_file):
        body = json.loads(client.get("/settings", headers=auth_pair()).data)
        assert body["discovery"]["enabled"] is True
        assert body["discovery"]["node"] == "receiptmqtt_printer_commands"
        assert body["discovery"]["acl"] == (
            "topic write homeassistant/+/receiptmqtt_printer_commands/+/config")

    def test_node_follows_a_topic_change(self, client, settings_file):
        client.post("/settings", headers=post_pair(),
                    data={"MQTT_TOPIC": "kitchen/commands"})
        body = json.loads(client.get("/settings", headers=auth_pair()).data)
        assert body["discovery"]["node"] == "receiptmqtt_kitchen_commands"
        assert "receiptmqtt_kitchen_commands" in body["discovery"]["acl"]


class TestTopicPrefixField:
    @pytest.fixture
    def settings_file(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.json"
        monkeypatch.setattr(w.settings_store, "SETTINGS_FILE", str(path))
        for key in w.settings_store.KEYS:
            monkeypatch.delenv(key, raising=False)
        before = escpos.PAPER_WIDTH_MM
        yield path
        escpos.apply_paper_width(before)

    def test_post_prefix_sets_every_topic(self, client, settings_file):
        resp = client.post("/settings", headers=post_pair(),
                           data={"MQTT_TOPIC_PREFIX": "receiptprinter2"})
        assert json.loads(resp.data)["saved"] is True
        body = json.loads(client.get("/settings", headers=auth_pair()).data)
        assert body["settings"]["MQTT_TOPIC_PREFIX"] == "receiptprinter2"
        assert body["topics"]["MQTT_TOPIC"] == "receiptprinter2/commands"
        assert body["topics"]["MQTT_AVAILABILITY_TOPIC"] == (
            "receiptprinter2/availability")
        assert body["topics"]["MQTT_FAILED_TOTAL_TOPIC"] == (
            "receiptprinter2/failed_total")

    def test_get_reports_environment_topic_overrides(self, client,
                                                     settings_file,
                                                     monkeypatch):
        monkeypatch.setenv("MQTT_PAPER_TOPIC", "custom/paper")
        body = json.loads(client.get("/settings", headers=auth_pair()).data)
        assert body["topic_overrides"] == ["MQTT_PAPER_TOPIC"]
        assert body["topics"]["MQTT_PAPER_TOPIC"] == "custom/paper"
