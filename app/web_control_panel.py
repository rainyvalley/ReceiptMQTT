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
import os
import subprocess

from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

cups_admin_user = os.getenv("ADMIN_USER", "admin")
cups_admin_pass = os.environ.get("ADMIN_PASS", "")
if not cups_admin_pass:
    raise SystemExit("ADMIN_PASS is not set; control panel cannot start safely.")

MQTT_TOPIC = os.getenv("MQTT_TOPIC", "printer/commands")


def check_auth(username, password):
    return username == cups_admin_user and password == cups_admin_pass


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


@app.route('/status', methods=['GET'])
@require_auth
def status():
    """Get the current status of the system."""
    try:
        # Get printers from CUPS
        printers = subprocess.check_output(["lpstat", "-p"], text=True)
        return jsonify({"printers": printers.strip().split("\n")})
    except Exception as e:
        return jsonify({"error": "lpstat failed: see container logs"})


@app.route('/test-print', methods=['POST'])
@require_auth
def test_print():
    """Send a test print command against the bundled test page."""
    printer_name = request.form.get("printer_name", "ReceiptPrinter")
    if not printer_name.isalnum():
        return jsonify({"success": False, "error": "invalid printer name"})
    try:
        result = subprocess.run(["lp", "-d", printer_name, "/app/test_print.txt"],
                                check=True, capture_output=True, text=True)
        return jsonify({"success": True, "output": result.stdout})
    except subprocess.CalledProcessError as e:
        return jsonify({"success": False, "error": "print failed: see container logs"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)