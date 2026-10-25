"""Web control panel for PrintMQTTify.

Endpoints:
  /            status, test print and the settings form
  /status      lpstat -p output plus a device-open probe, as JSON
  /settings    GET current values, POST changes (paper, broker, login, topics,
               device)
  /test-print  write the escpos selftest raster straight to PRINTER_DEVICE
  /test-message  publish a print job through the broker (end-to-end check)

CUPS itself is loopback-only now that printing is a raw queue, so this panel
is the only network-facing surface besides MQTT. It is protected with HTTP
Basic auth using ADMIN_USER / ADMIN_PASS and speaks plain HTTP unless WEB_TLS_CERT and
WEB_TLS_KEY are set (then the entrypoint serves HTTPS); otherwise put a reverse
proxy with TLS in front of it, or keep it on localhost. Repeated wrong
passwords from one address are locked out for a while, and /test-print has a
cooldown.

The settings form stores paper width, printer device, broker login and the
MQTT topic prefix in one JSON file. Environment variables set in Compose/.env still
win and are shown read-only, so the form only replaces editing `.env` for
values that are not set there.

The entrypoint serves this module with gunicorn (ONE worker: the lockout and
cooldown state live in this process). `python3 web_control_panel.py` still
starts Flask's development server, for local use only.
"""

import functools
import hmac
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import escpos
import printer_mqtt_handler as handler
import settings_store

try:
    import paho.mqtt.client as mqtt
except ImportError:  # tests and pipeline-only installs without paho
    mqtt = None
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

cups_admin_user = os.getenv("ADMIN_USER", "admin")
cups_admin_pass = os.environ.get("ADMIN_PASS", "")
if not cups_admin_pass:
    raise SystemExit("ADMIN_PASS is not set; control panel cannot start safely.")

def _env_number(name, default, minimum=0):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"Ignoring invalid {name}={raw!r}; using {default}")
        return default
    if value < minimum:
        print(f"Ignoring {name}={value} (minimum {minimum}); using {default}")
        return default
    return value


# Login lockout: this many wrong passwords from one address within the window
# locks that address out for LOCKOUT_SECONDS. Set LOGIN_MAX_FAILURES=0 to turn
# it off.
LOGIN_MAX_FAILURES = _env_number("LOGIN_MAX_FAILURES", 5)
LOGIN_WINDOW_SECONDS = 300
LOCKOUT_SECONDS = _env_number("LOGIN_LOCKOUT_SECONDS", 60, minimum=1)
# Minimum gap between two test prints (seconds). 0 turns it off.
TEST_PRINT_COOLDOWN = _env_number("TEST_PRINT_COOLDOWN", 10)

_THROTTLE_LOCK = threading.Lock()
_failures = {}          # address -> [monotonic times of recent failures]
_locked_until = {}      # address -> monotonic time the lockout ends
_last_test_print = [None]
MAX_TRACKED_ADDRESSES = 1024


def reset_state():
    """Forget lockouts and the test-print cooldown (used by tests)."""
    with _THROTTLE_LOCK:
        _failures.clear()
        _locked_until.clear()
        _last_test_print[0] = None


def _lockout_remaining(addr):
    """Seconds left on this address's lockout (0 = not locked)."""
    if LOGIN_MAX_FAILURES <= 0:
        return 0
    with _THROTTLE_LOCK:
        end = _locked_until.get(addr)
        if end is None:
            return 0
        left = end - time.monotonic()
        if left <= 0:
            del _locked_until[addr]
            _failures.pop(addr, None)
            return 0
        return int(left) + 1


def _record_failure(addr):
    if LOGIN_MAX_FAILURES <= 0:
        return
    now = time.monotonic()
    with _THROTTLE_LOCK:
        if len(_failures) >= MAX_TRACKED_ADDRESSES and addr not in _failures:
            # forget the oldest bookkeeping rather than grow without bound
            _failures.pop(next(iter(_failures)))
        recent = [t for t in _failures.get(addr, [])
                  if now - t < LOGIN_WINDOW_SECONDS]
        recent.append(now)
        _failures[addr] = recent
        if len(recent) >= LOGIN_MAX_FAILURES:
            _locked_until[addr] = now + LOCKOUT_SECONDS
            print(f"Control panel: {len(recent)} wrong passwords from {addr}; "
                  f"locked out for {LOCKOUT_SECONDS}s")


def _record_success(addr):
    with _THROTTLE_LOCK:
        _failures.pop(addr, None)


def tls_paths(env=None):
    """(cert, key) from WEB_TLS_CERT / WEB_TLS_KEY, or None for plain HTTP."""
    env = os.environ if env is None else env
    cert = (env.get("WEB_TLS_CERT") or "").strip()
    key = (env.get("WEB_TLS_KEY") or "").strip()
    return (cert, key) if cert and key else None


def startup_warnings(env=None):
    """Plain-language notes about the panel's exposure (printed at start)."""
    env = os.environ if env is None else env
    cert = (env.get("WEB_TLS_CERT") or "").strip()
    key = (env.get("WEB_TLS_KEY") or "").strip()
    if bool(cert) != bool(key):
        return ["WARNING: set both WEB_TLS_CERT and WEB_TLS_KEY to turn on "
                "HTTPS; only one is set."]
    if cert:
        return []
    bind = web_bind(env)
    if bind in ("127.0.0.1", "::1", "localhost"):
        return []
    return [
        "NOTE: the control panel speaks plain HTTP, so its password is "
        "readable by anyone on the network path. Set WEB_TLS_CERT and "
        "WEB_TLS_KEY for HTTPS, keep it on localhost "
        f"(WEB_PUBLISH_BIND=127.0.0.1) or use a TLS reverse proxy (listening on {bind})."]


CSRF_HEADER_VALUE = "PrintMQTTify"


def check_auth(username, password):
    # constant-time compares on bytes: compare_digest raises TypeError on a
    # str with non-ASCII characters (Basic auth is latin-1-decoded), so
    # encode first. '&' (not `and`) so both fields are always compared.
    return hmac.compare_digest(username.encode(), cups_admin_user.encode()) & \
        hmac.compare_digest(password.encode(), cups_admin_pass.encode())


def current_device():
    """The device path in force now (panel changes apply without a restart)."""
    return settings_store.effective()["PRINTER_DEVICE"]


def new_mqtt_client():
    """paho client for both 1.x and 2.x, matching the handler's choice."""
    if mqtt is None:
        raise RuntimeError("paho-mqtt is not installed")
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                           protocol=mqtt.MQTTv311)
    except AttributeError:
        return mqtt.Client(protocol=mqtt.MQTTv311)


def publish_test_message(config=None, payload=None, timeout=10):
    """Publish one print job through the configured broker; returns the topic.

    This is the same path real jobs take: broker login, TLS if configured,
    command topic, then the handler prints it. It proves the MQTT side works,
    which the panel's own test print (device only) cannot.
    """
    config = config or settings_store.effective()
    if payload is None:
        payload = json.dumps({
            "printer_name": os.getenv("PRINTER_NAME") or "ReceiptPrinter",
            "title": "PrintMQTTify test",
            "message": "Published from the control panel at "
                       + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    client = new_mqtt_client()
    if config["MQTT_USERNAME"] and config["MQTT_PASSWORD"]:
        client.username_pw_set(config["MQTT_USERNAME"],
                               config["MQTT_PASSWORD"])
    handler.configure_tls(client)
    client.connect(config["MQTT_BROKER"], handler.broker_port, timeout)
    client.loop_start()
    try:
        client.publish(config["MQTT_TOPIC"], payload, qos=1).wait_for_publish(
            timeout)
    finally:
        client.loop_stop()
        client.disconnect()
    return config["MQTT_TOPIC"]


def apply_local_settings():
    """Keep this process's paper profile in step with the settings file."""
    config = settings_store.effective()
    escpos.apply_paper_width(int(config["PAPER_WIDTH_MM"]))
    return config


def device_probe():
    """Can we open the printer device right now? lpstat says nothing about
    the USB device; this is what /status should really show."""
    device = current_device()
    if not device:
        return "no PRINTER_DEVICE configured"
    try:
        fd = os.open(device, os.O_WRONLY)
        os.close(fd)
        return f"{device} open OK"
    except OSError as e:
        return f"{device}: {e.strerror or e}"


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        addr = request.remote_addr or "?"
        left = _lockout_remaining(addr)
        if left:
            return Response("Too many wrong passwords; try again later", 429,
                            {"Retry-After": str(left)})
        auth = request.authorization
        # Only Basic carries a username/password pair; Bearer/Digest headers
        # parse with None fields and must get a 401, not a 500.
        if (auth is None or auth.type != "basic"
                or not check_auth(auth.username or "", auth.password or "")):
            # A browser's first request has no header at all; that is not a
            # wrong password, so do not count it.
            if auth is not None:
                _record_failure(addr)
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="PrintMQTTify"'})
        _record_success(addr)
        return f(*args, **kwargs)
    return decorated


@app.route('/')
@require_auth
def index():
    """Render the main control panel."""
    return render_template(
        'index.html',
        mqtt_topic=settings_store.effective()["MQTT_TOPIC"])


@app.route('/status')
@require_auth
def status():
    """Queue state AND the live device-probe result."""
    try:
        printers = subprocess.check_output(["lpstat", "-p"], text=True,
                                           timeout=10)
        printers = printers.strip().split("\n")
    except Exception:
        printers = ["lpstat failed: see container logs"]
    return jsonify({"printers": printers, "device": device_probe()})


@app.route('/test-print', methods=['POST'])
@require_auth
def test_print():
    """Print the internal test pattern directly to the printer device.

    The old panel sent a text file via lp onto the raw+file: queue, which
    CUPS silently discards (zero bytes to the device). This runs the same
    selftest raster the app prints, byte-for-byte.
    """
    # Browsers replay cached Basic credentials on cross-site form POSTs, so
    # any page the admin visits could fire prints (CSRF). Two checks: fetch
    # metadata marks cross-site requests, and a custom header (which a
    # cross-site page cannot add without the browser asking us first) is
    # required. The panel's own page sends it; curl users add
    # -H "X-Requested-With: PrintMQTTify".
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in (
            "same-origin", "none"):
        return jsonify({"success": False,
                        "error": "cross-site request refused"}), 403
    if request.headers.get("X-Requested-With") != CSRF_HEADER_VALUE:
        return jsonify({"success": False,
                        "error": "missing X-Requested-With header"}), 403
    wait = _cooldown_claim()
    if wait:
        return jsonify({"success": False,
                        "error": f"test print was just sent; wait {wait}s"}
                       ), 429, {"Retry-After": str(wait)}
    try:
        apply_local_settings()
        settings = escpos.EscposSettings.from_env(settings_store.runtime_env())
        pbm = escpos.selftest_pbm()
        escpos_bytes, _pages = escpos.render_all(pbm, settings)
        device = current_device()
        n = handler.write_device_retrying(device, escpos_bytes)
        return jsonify({"success": True,
                        "output": f"wrote {n} bytes to {device}"})
    except OSError as e:
        return jsonify({"success": False,
                        "error": f"{current_device()}: {e}"}), 502


def _settings_guard():
    """CSRF response for a settings POST, or None when it may proceed."""
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in (
            "same-origin", "none"):
        return jsonify({"saved": False,
                        "error": "cross-site request refused"}), 403
    if request.headers.get("X-Requested-With") != CSRF_HEADER_VALUE:
        return jsonify({"saved": False,
                        "error": "missing X-Requested-With header"}), 403
    return None


@app.route('/settings')
@require_auth
def get_settings():
    """Current values, environment locks, and the discovery ACL hint."""
    config = apply_local_settings()
    node = f"receiptmqtt_{handler.discovery_node(config['MQTT_TOPIC'])}"
    locked = settings_store.locked()
    return jsonify({"settings": config,
                    "locked": locked,
                    "topics": {key: config[key]
                               for key in settings_store.TOPIC_KEYS},
                    "topic_overrides": sorted(
                        key for key in settings_store.TOPIC_KEYS if locked[key]),
                    "file": settings_store.SETTINGS_FILE,
                    "paper_width_mm": escpos.PAPER_WIDTH_MM,
                    "print_area_mm": escpos.PRINT_AREA_MM,
                    "discovery": {
                        "enabled": handler.HA_DISCOVERY,
                        "prefix": handler.HA_DISCOVERY_PREFIX,
                        "node": node,
                        "acl": (f"topic write {handler.HA_DISCOVERY_PREFIX}/+/"
                                f"{node}/+/config"),
                    }})


@app.route('/settings', methods=['POST'])
@require_auth
def save_settings():
    """Store panel changes. Environment-locked fields are left alone."""
    refused = _settings_guard()
    if refused is not None:
        return refused
    values = {key: request.form.get(key, "")
              for key in settings_store.KEYS if key in request.form}
    if not values:
        return jsonify({"saved": False,
                        "errors": ["no settings were submitted"]}), 400
    try:
        result = settings_store.update(values)
    except OSError as e:
        return jsonify({"saved": False,
                        "error": f"could not write settings: {e}"}), 500
    if not result["saved"]:
        return jsonify(result), 400
    apply_local_settings()
    result["settings"] = settings_store.effective()
    result["locked"] = settings_store.locked()
    return jsonify(result)


@app.route('/test-message', methods=['POST'])
@require_auth
def test_message():
    """Publish a test job through the broker, so the whole MQTT path runs."""
    refused = _settings_guard()
    if refused is not None:
        return refused
    wait = _cooldown_claim()
    if wait:
        return jsonify({"success": False,
                        "error": f"a test was just sent; wait {wait}s"}
                       ), 429, {"Retry-After": str(wait)}
    apply_local_settings()
    try:
        topic_used = publish_test_message()
    except Exception as e:
        return jsonify({"success": False,
                        "error": f"could not publish: {e}"}), 502
    return jsonify({"success": True,
                    "output": f"published a test message to {topic_used}"})


def _cooldown_claim():
    """0 and records the time if a test print may go now, else seconds to wait."""
    if TEST_PRINT_COOLDOWN <= 0:
        return 0
    now = time.monotonic()
    with _THROTTLE_LOCK:
        last = _last_test_print[0]
        if last is not None and now - last < TEST_PRINT_COOLDOWN:
            return int(TEST_PRINT_COOLDOWN - (now - last)) + 1
        _last_test_print[0] = now
        return 0


def web_bind(env=None):
    """Address the panel binds to: WEB_BIND, default 0.0.0.0 (unchanged)."""
    env = os.environ if env is None else env
    return (env.get("WEB_BIND") or "").strip() or "0.0.0.0"


for _line in startup_warnings():
    print(_line)


if __name__ == '__main__':
    app.run(host=web_bind(), port=8080, ssl_context=tls_paths())