"""Web control panel for PrintMQTTify.

Endpoints:
  /            status + test-print page (Basic auth, see below)
  /status      lpstat -p output as JSON
  /test-print  queue the bundled test page against a printer

CUPS itself is loopback-only now that printing is a raw queue, so this panel
is the only network-facing surface besides MQTT. It is protected with HTTP
Basic auth using ADMIN_USER / ADMIN_PASS. There is no settings form: real
configuration happens through container environment variables.
"""

import functools
import hmac
import os
import subprocess

import escpos
from printer_mqtt_handler import _write_device, printer_device
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

cups_admin_user = os.getenv("ADMIN_USER", "admin")
cups_admin_pass = os.environ.get("ADMIN_PASS", "")
if not cups_admin_pass:
    raise SystemExit("ADMIN_PASS is not set; control panel cannot start safely.")

MQTT_TOPIC = os.getenv("MQTT_TOPIC", "printer/commands")


def check_auth(username, password):
    # constant-time compares; the '&' forces both evaluations so username
    # and password both get the same treatment
    return hmac.compare_digest(username, cups_admin_user) and \
        hmac.compare_digest(password, cups_admin_pass)


def device_probe():
    """Can we open the printer device right now? lpstat says nothing about
    the USB device; this is what /status should really show."""
    if not printer_device:
        return "no PRINTER_DEVICE configured"
    try:
        fd = os.open(printer_device, os.O_WRONLY)
        os.close(fd)
        return f"{printer_device} open OK"
    except OSError as e:
        return f"{printer_device}: {e.strerror or e}"


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if auth is None or not check_auth(auth.username, auth.password):
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="PrintMQTTify"'})
        return f(*args, **kwargs)
    return decorated


@app.route('/')
@require_auth
def index():
    """Render the main control panel."""
    return render_template('index.html', mqtt_topic=MQTT_TOPIC)


@app.route('/status')
@require_auth
def status():
    """Queue state AND the live device-probe result."""
    try:
        printers = subprocess.check_output(["lpstat", "-p"], text=True)
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
    printer_name = request.form.get("printer_name", "ReceiptPrinter")
    if not printer_name.isalnum():
        return jsonify({"success": False, "error": "invalid printer name"})
    try:
        settings = escpos.EscposSettings.from_env()
        pbm = escpos.selftest_pbm()
        escpos_bytes, _pages = escpos.render_all(pbm, settings)
        n = _write_device(printer_device, escpos_bytes)
        return jsonify({"success": True,
                        "output": f"wrote {n} bytes to {printer_device}"})
    except OSError as e:
        return jsonify({"success": False, "error": f"{printer_device}: {e}"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)