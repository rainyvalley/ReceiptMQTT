"""Opt-in TLS / port / web bind settings; defaults keep legacy behaviour."""

import pytest

import printer_mqtt_handler as h


class FakeTlsClient:
    def __init__(self):
        self.calls = []

    def tls_set(self, *a, **k):
        self.calls.append(("tls_set", a, k))

    def tls_insecure_set(self, v):
        self.calls.append(("insecure", v))


def test_tls_off_by_default():
    c = FakeTlsClient()
    assert h.configure_tls(c, {}) is False
    assert c.calls == []


@pytest.mark.parametrize("val", ["", "0", "false", "off", "junk"])
def test_tls_off_values(val):
    c = FakeTlsClient()
    assert h.configure_tls(c, {"MQTT_TLS": val}) is False
    assert c.calls == []


@pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
def test_tls_on_uses_system_cas(val):
    c = FakeTlsClient()
    assert h.configure_tls(c, {"MQTT_TLS": val}) is True
    assert c.calls == [("tls_set", (), {})]


def test_tls_custom_ca_and_insecure():
    c = FakeTlsClient()
    h.configure_tls(c, {"MQTT_TLS": "1", "MQTT_TLS_CA": "/certs/ca.pem",
                        "MQTT_TLS_INSECURE": "1"})
    assert c.calls == [("tls_set", (), {"ca_certs": "/certs/ca.pem"}),
                       ("insecure", True)]


def test_insecure_requires_explicit_opt_in():
    c = FakeTlsClient()
    h.configure_tls(c, {"MQTT_TLS": "1"})
    assert ("insecure", True) not in c.calls


def test_port_default_and_override(monkeypatch):
    monkeypatch.delenv("MQTT_PORT", raising=False)
    assert h._broker_port() == 1883
    for val, want in [("8883", 8883), ("junk", 1883), ("70000", 1883),
                      ("", 1883), ("0", 1883)]:
        monkeypatch.setenv("MQTT_PORT", val)
        assert h._broker_port() == want


def test_web_bind_default_and_override():
    import web_control_panel as w
    assert w.web_bind({}) == "0.0.0.0"
    assert w.web_bind({"WEB_BIND": " "}) == "0.0.0.0"
    assert w.web_bind({"WEB_BIND": "127.0.0.1"}) == "127.0.0.1"
