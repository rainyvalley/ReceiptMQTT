try:
    import paho.mqtt.client as mqtt
except ImportError:  # imported for pipeline unit use (tests) without broker deps
    mqtt = None
import collections
import errno
import os
import re
import select
import subprocess
import sys
import tempfile
import time
import threading
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import mm
    from reportlab.pdfbase import pdfmetrics
except ImportError:  # pipeline unit use without reportlab installed
    canvas = None
    pdfmetrics = None
    mm = 2.834645669  # points per mm, reportlab's conversion
import json
import queue
import signal
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import escpos
import settings_store


def _env_int(name, default, minimum=1):
    """Integer env var; unset, junk or below-minimum values fall back to the default."""
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


def _env_bool(name, default=False, env=None):
    """Boolean env var: 1/true/yes/on and 0/false/no/off; unset/empty/junk -> default."""
    raw = (os.environ if env is None else env).get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    print(f"Ignoring invalid {name}={raw!r}; using {default}")
    return default


# Broker, login, printer device and paper width come from the panel settings
# file, with environment variables (Compose/.env) taking precedence. The
# panel can change them at runtime; see apply_runtime_settings().
_CONFIG = settings_store.effective()
broker = _CONFIG["MQTT_BROKER"]
username = _CONFIG["MQTT_USERNAME"] or None
password = _CONFIG["MQTT_PASSWORD"] or None
topic = _CONFIG["MQTT_TOPIC"]
availability_topic = _CONFIG["MQTT_AVAILABILITY_TOPIC"]
last_error_topic = _CONFIG["MQTT_LAST_ERROR_TOPIC"]
dropped_total_topic = _CONFIG["MQTT_DROPPED_TOTAL_TOPIC"]
failed_total_topic = _CONFIG["MQTT_FAILED_TOTAL_TOPIC"]
paper_topic = _CONFIG["MQTT_PAPER_TOPIC"]
paper_low_topic = _CONFIG["MQTT_PAPER_LOW_TOPIC"]
printer_device = _CONFIG["PRINTER_DEVICE"]
escpos.apply_paper_width(int(_CONFIG["PAPER_WIDTH_MM"]))

# Home Assistant MQTT discovery: every value the app publishes becomes a
# retained config, so the device shows up with its sensors without hand-
# written YAML. Opt out with HA_DISCOVERY=0; the topics below stay the same.
HA_DISCOVERY = _env_bool("HA_DISCOVERY", True)
HA_DISCOVERY_PREFIX = (os.getenv("HA_DISCOVERY_PREFIX") or "homeassistant").strip("/")
HA_DEVICE_NAME = (os.getenv("HA_DEVICE_NAME") or os.getenv("PRINTER_NAME")
                  or "ReceiptPrinter")


def discovery_node(topic_value=None):
    """Stable entity prefix for this instance, derived from its command topic.

    Two printers on one broker should use different command topics (see the
    README), which keeps their discovery entries apart.
    """
    value = (topic if topic_value is None else topic_value).lower()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_") or "printer"


def discovery_topics(node=None):
    """The retained discovery config topics for one instance prefix."""
    node = node or discovery_node()
    return [f"{HA_DISCOVERY_PREFIX}/{component}/receiptmqtt_{node}/{key}/config"
            for component, key in DISCOVERY_ENTITIES]


DISCOVERY_ENTITIES = (
    # component, object id
    ("binary_sensor", "availability"),
    ("binary_sensor", "paper"),
    ("binary_sensor", "paper_low"),
    ("sensor", "last_error"),
    ("sensor", "dropped_total"),
    ("sensor", "failed_total"),
)


def discovery_messages():
    """(topic, payload) retained Home Assistant discovery configs."""
    node = discovery_node()
    device = {
        "identifiers": [f"receiptmqtt_{node}"],
        "name": HA_DEVICE_NAME,
        "manufacturer": "PrintMQTTify",
        "model": "ReceiptMQTT",
    }
    offline = {"topic": availability_topic,
               "payload_available": "online",
               "payload_not_available": "offline"}
    entities = [
        # An availability entity must not use itself as availability, or Home
        # Assistant would show "unavailable" instead of "offline".
        ("binary_sensor", "availability", "Availability", availability_topic,
         {"payload_on": "online", "payload_off": "offline",
          "device_class": "connectivity"}, None),
        # device_class problem wants ON = a problem: paper OFF is out of
        # paper, paper_low ON is the near-end sensor tripped. Same polarity
        # as the hand-written YAML in the README.
        ("binary_sensor", "paper", "Paper", paper_topic,
         {"payload_on": "OFF", "payload_off": "ON",
          "device_class": "problem"}, offline),
        ("binary_sensor", "paper_low", "Paper low", paper_low_topic,
         {"payload_on": "ON", "payload_off": "OFF",
          "device_class": "problem"}, offline),
        ("sensor", "last_error", "Last error", last_error_topic, {}, offline),
        ("sensor", "dropped_total", "Dropped jobs", dropped_total_topic,
         {"state_class": "total_increasing"}, offline),
        ("sensor", "failed_total", "Failed jobs", failed_total_topic,
         {"state_class": "total_increasing"}, offline),
    ]
    messages = []
    for component, key, name, state_topic, extra, availability in entities:
        payload = {
            "name": name,
            "unique_id": f"receiptmqtt_{node}_{key}",
            "state_topic": state_topic,
            "device": device,
        }
        payload.update(extra)
        if availability is not None:
            payload["availability"] = [availability]
        config_topic = (f"{HA_DISCOVERY_PREFIX}/{component}/"
                        f"receiptmqtt_{node}/{key}/config")
        messages.append((config_topic, payload))
    return messages

# Default receipt printer settings: ghostscript renders the reportlab PDF at
# 203 dpi into a 384 px wide raster (48 mm roll), matching the old PPD's
# X48MMY60MM page. The raster height is sized per receipt from the PDF page
# height; RASTER_HEIGHT_PX is a hard cap - taller receipts are truncated
# with a loud warning, not silently cut.
RASTER_DPI_X = _env_int("RASTER_DPI_X", 203)
RASTER_DPI_Y = _env_int("RASTER_DPI_Y", 203)
RASTER_WIDTH_PX = _env_int("RASTER_WIDTH_PX", escpos.PAPER_WIDTH_PX)
RASTER_HEIGHT_PX = _env_int("RASTER_HEIGHT_PX", 480)

# Keys whose change needs a new broker connection rather than just new
# values for the next job: login details, and the topics (the subscription,
# the last will and the retained availability move with them).
RECONNECT_KEYS = ("MQTT_BROKER", "MQTT_USERNAME", "MQTT_PASSWORD",
                  "MQTT_TOPIC", "MQTT_AVAILABILITY_TOPIC", "MQTT_PAPER_TOPIC",
                  "MQTT_PAPER_LOW_TOPIC", "MQTT_LAST_ERROR_TOPIC",
                  "MQTT_DROPPED_TOTAL_TOPIC", "MQTT_FAILED_TOTAL_TOPIC")

# Topic setting -> module global.
TOPIC_GLOBALS = {
    "MQTT_TOPIC": "topic",
    "MQTT_AVAILABILITY_TOPIC": "availability_topic",
    "MQTT_PAPER_TOPIC": "paper_topic",
    "MQTT_PAPER_LOW_TOPIC": "paper_low_topic",
    "MQTT_LAST_ERROR_TOPIC": "last_error_topic",
    "MQTT_DROPPED_TOTAL_TOPIC": "dropped_total_topic",
    "MQTT_FAILED_TOTAL_TOPIC": "failed_total_topic",
}


def apply_runtime_settings(env=None, client=None):
    """Re-read panel settings into this process; returns the keys that changed.

    Environment overrides are resolved by settings_store, so a value set in
    `.env` or Compose still wins over anything saved from the panel. When a
    client is given, the old availability topic is cleared so Home Assistant
    does not keep reading a retained "online" that nothing updates any more.
    """
    global broker, username, password, printer_device, RASTER_WIDTH_PX
    global topic, availability_topic, paper_topic, paper_low_topic
    global last_error_topic, dropped_total_topic, failed_total_topic
    config = settings_store.effective(env)
    changed = []

    if broker != config["MQTT_BROKER"]:
        broker = config["MQTT_BROKER"]
        changed.append("MQTT_BROKER")
    if (username or None) != (config["MQTT_USERNAME"] or None):
        username = config["MQTT_USERNAME"] or None
        changed.append("MQTT_USERNAME")
    if (password or None) != (config["MQTT_PASSWORD"] or None):
        password = config["MQTT_PASSWORD"] or None
        changed.append("MQTT_PASSWORD")
    if printer_device != config["PRINTER_DEVICE"]:
        printer_device = config["PRINTER_DEVICE"]
        changed.append("PRINTER_DEVICE")

    for key, name in TOPIC_GLOBALS.items():
        if globals()[name] != config[key]:
            if client is not None:
                # Drop retained values that would otherwise linger: the old
                # availability topic, and the discovery configs of the old
                # entity prefix when the command topic moves.
                stale = []
                if key == "MQTT_AVAILABILITY_TOPIC":
                    stale.append(globals()[name])
                if key == "MQTT_TOPIC":
                    stale.extend(discovery_topics())
                for stale_topic in stale:
                    try:
                        client.publish(stale_topic, b"", qos=1, retain=True)
                    except Exception:
                        pass
            globals()[name] = config[key]
            changed.append(key)

    paper = int(config["PAPER_WIDTH_MM"])
    if escpos.PAPER_WIDTH_MM != paper:
        escpos.apply_paper_width(paper)
        changed.append("PAPER_WIDTH_MM")

    width = _env_int("RASTER_WIDTH_PX", escpos.PAPER_WIDTH_PX)
    if RASTER_WIDTH_PX != width:
        RASTER_WIDTH_PX = width
    return changed

# DLE EOT 4 - real-time paper sensor status request. "Real-time" means the
# printer answers immediately rather than queueing the request behind print
# data, so this works even while a job is running.
PAPER_QUERY = b"\x10\x04\x04"

POLL_TIMEOUT = 0.25       # select wait per attempt
POLL_ATTEMPTS = 3         # <=0.75s total hold; jobs hitting EBUSY retry

# Subprocess ceilings. gs runs on the print worker thread and lpstat on the
# availability thread: without a timeout a wedged child blocks either one
# forever (keepalive lost / availability frozen at its last value).
GS_TIMEOUT = 60
LPSTAT_TIMEOUT = 10


# Broker connection. Defaults are the historical behaviour: plain TCP 1883.
def _broker_port():
    port = _env_int("MQTT_PORT", 1883)
    if port > 65535:
        print(f"Ignoring MQTT_PORT={port} (maximum 65535); using 1883")
        return 1883
    return port


broker_port = _broker_port()


def configure_tls(client, env=None):
    """Enable TLS on the client when MQTT_TLS is on; returns True if enabled.

    System CAs are used unless MQTT_TLS_CA names a CA bundle file.
    MQTT_TLS_INSECURE=1 skips hostname verification (certificates are still
    required to chain to the CA) - for lab brokers only. All opt-in.
    """
    env = os.environ if env is None else env
    if not _env_bool("MQTT_TLS", False, env):
        return False
    ca = (env.get("MQTT_TLS_CA") or "").strip()
    if ca:
        client.tls_set(ca_certs=ca)
    else:
        client.tls_set()
    if _env_bool("MQTT_TLS_INSECURE", False, env):
        client.tls_insecure_set(True)
        print("WARNING: MQTT_TLS_INSECURE is on; broker hostname is NOT verified")
    return True


# Jobs waiting behind the one being printed. When full, the NEWEST message
# is dropped (and counted) so the MQTT network thread never blocks.
PRINT_QUEUE_SIZE = _env_int("PRINT_QUEUE_SIZE", 16)
# Jobs accepted per rolling minute; the rest are dropped and reported. Keeps a
# flood of messages from tying up the printer and ghostscript for hours.
MAX_JOBS_PER_MINUTE = _env_int("MAX_JOBS_PER_MINUTE", 30)
SHUTDOWN_JOIN_TIMEOUT = 10   # seconds to wait for the worker on exit


ERROR_TITLE_CAP = 40      # chars of job title echoed in a last_error message
ERROR_DETAIL_CAP = 120    # chars of exception text echoed

_CLIENT = None            # set by __main__ once the paho client exists


def _safe_publish(topic_, payload, retain=True, qos=1):
    """Fire-and-forget publish: paho queues it, we never wait. Any error is
    logged and swallowed - observability must not take down a worker."""
    client = _CLIENT
    if client is None:
        return
    try:
        client.publish(topic_, payload, qos=qos, retain=retain)
    except Exception as e:
        print(f"Could not publish to {topic_}: {e}")


def publish_counters(dropped, failed):
    """Retained running totals (since process start) for HA sensors."""
    _safe_publish(dropped_total_topic, str(dropped))
    _safe_publish(failed_total_topic, str(failed))


def report_event(kind, label, detail, dropped, failed):
    """Publish a dropped/failed job to MQTT (retained last_error + counters).

    Payload never carries the message body; only the title, cut to
    ERROR_TITLE_CAP chars, and (for failures) a capped exception string.
    """
    try:
        title = (label or "")[:ERROR_TITLE_CAP]
        if kind == "dropped":
            reason = (detail or "queue full")[:ERROR_DETAIL_CAP]
            text = f"{reason}, job {title!r} dropped"
        else:
            text = f"job {title!r} failed: {(detail or '')[:ERROR_DETAIL_CAP]}"
        _safe_publish(last_error_topic, json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "message": text,
            "dropped_total": dropped,
            "failed_total": failed,
        }))
        publish_counters(dropped, failed)
    except Exception as e:   # never propagate into the worker/network thread
        print(f"Could not report {kind} job: {e}")


def query_paper():
    """Ask the printer whether it has paper.

    Returns (present, low) or None when the printer did not answer -
    either it is unidirectional, the device is busy with a print job, or
    it is unplugged. None means "unknown", which is deliberately not the
    same as "out".

    present: False once the paper-end sensor trips.
    low:     True once the near-end sensor trips, on units that have one.
             Always False on units that do not.
    """
    fd = None
    try:
        fd = os.open(printer_device, os.O_RDWR | os.O_NONBLOCK)
        os.write(fd, PAPER_QUERY)
        response = b""
        for _ in range(POLL_ATTEMPTS):
            ready, _, _ = select.select([fd], [], [], POLL_TIMEOUT)
            if not ready:
                continue
            response = os.read(fd, 8)
            if response:
                break
        if not response:
            return None
        status = response[-1]
        # Bits 5 and 6 both set: paper-end sensor reports no paper.
        # Bits 2 and 3 both set: near-end (low paper) sensor has tripped.
        return ((status & 0x60) != 0x60, (status & 0x0C) == 0x0C)
    except OSError as e:
        if e.errno not in (errno.EBUSY, errno.EAGAIN, errno.ENODEV,
                           errno.ENOENT, errno.EACCES):
            print(f"Paper status query failed: {e}")
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# Set once shutdown begins. The availability poller checks it under
# _AVAIL_LOCK before every publish, so it cannot re-publish "online" after
# graceful_shutdown() has published the final "offline".
SHUTDOWN = threading.Event()
_AVAIL_LOCK = threading.Lock()
# Last availability the poller decided on; re-announced on every (re)connect.
LAST_AVAILABILITY = "online"


def publish_availability(client, interval=60):
    """Publish printer availability periodically."""
    printer_name = os.getenv("PRINTER_NAME") or "ReceiptPrinter"

    def check_printer():
        """"online" or "offline": CUPS queue enabled AND device node present."""
        try:
            result = subprocess.run(["lpstat", "-p", printer_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    timeout=LPSTAT_TIMEOUT)
            if result.returncode != 0:
                print(f"lpstat failed rc={result.returncode}: {result.stderr.strip()}")
                return "offline"
            if not result.stdout.strip() or "disabled" in result.stdout:
                return "offline"
        except Exception as e:
            print(f"Error checking printer status: {e}")
            return "offline"
        # The CUPS queue stays enabled when the USB printer is unplugged; the
        # device node is what disappears.
        if printer_device and not os.path.exists(printer_device):
            return "offline"
        # "processing" counts as online; only "disabled" means down
        return "online"

    def poll_once(state):
        global LAST_AVAILABILITY
        status = check_printer()

        # Only log and publish on change; the topic is retained
        LAST_AVAILABILITY = status
        if status != state["status"]:
            print(f"Publishing status: {status}")
            state["status"] = status
        with _AVAIL_LOCK:
            if SHUTDOWN.is_set():
                return False
            client.publish(availability_topic, str(status), qos=1, retain=True)

        # Paper sensor. Polled on the same cadence so only one thread
        # ever touches the device. An unanswered query leaves the last
        # known value retained rather than reporting a false "out".
        result = query_paper()
        if result is None:
            if not state["warned_no_sensor"] and state["paper"] is None:
                print(f"No paper status from {printer_device} "
                      "(printer may be unidirectional)")
                state["warned_no_sensor"] = True
        else:
            paper, low = result

            payload = "ON" if paper else "OFF"
            if paper != state["paper"]:
                print(f"Publishing paper: {payload}")
                state["paper"] = paper
            client.publish(paper_topic, payload, qos=1, retain=True)

            low_payload = "ON" if low else "OFF"
            if low != state["low"]:
                print(f"Publishing paper_low: {low_payload}")
                state["low"] = low
            client.publish(paper_low_topic, low_payload, qos=1, retain=True)
        return True

    def publish_status():
        state = {"status": None, "paper": None, "low": None,
                 "warned_no_sensor": False}
        while True:
            try:
                if not poll_once(state):
                    return
            except Exception as e:   # a bad publish must not end the poller
                print(f"Availability poll failed: {e}")
            time.sleep(interval)

    thread = threading.Thread(target=publish_status, daemon=True)
    thread.start()
    return thread


AVAILABILITY_THREAD = None


def graceful_shutdown(client, publish_timeout=2):
    """Announce offline, then disconnect.

    A clean DISCONNECT makes the broker discard the will, which would leave
    the retained availability stuck at "online"; so publish "offline"
    ourselves first (qos 1, retained) and wait briefly for the ack. The
    paper topics are left as they are. Runs on a helper thread, never in
    the signal handler.
    """
    with _AVAIL_LOCK:
        SHUTDOWN.set()
    try:
        info = client.publish(availability_topic, "offline", qos=1, retain=True)
        info.wait_for_publish(publish_timeout)
    except Exception as e:
        print(f"Could not publish offline before disconnect: {e}")
    # Makes loop_forever() return (disconnect also suppresses reconnect)
    client.disconnect()


CONNECT_BACKOFF_START = 1.0
CONNECT_BACKOFF_MAX = 30.0


def connect_with_retry(client, host, port, keepalive=60, stop=None, sleep=None):
    """client.connect() until it works, with doubling backoff (capped).

    Returns True once connected, False if shutdown was requested first.
    The wait is on the SHUTDOWN event so SIGTERM interrupts it at once.
    """
    stop = SHUTDOWN if stop is None else stop
    wait = stop.wait if sleep is None else sleep
    delay = CONNECT_BACKOFF_START
    while not stop.is_set():
        try:
            client.connect(host, port, keepalive)
            return True
        except Exception as e:
            print(f"Cannot reach MQTT broker {host}:{port}: {e}; "
                  f"retrying in {delay:g}s")
            wait(delay)
            delay = min(delay * 2, CONNECT_BACKOFF_MAX)
    return False


def startup_warnings(user=None, pw=None, env=None):
    """Plain-language warnings about an open MQTT setup (printed at start)."""
    env = os.environ if env is None else env
    user = username if user is None else user
    pw = password if pw is None else pw
    warnings = []
    if not (user and pw):
        warnings.append(
            "WARNING: connecting to the MQTT broker without a username and "
            "password. Anyone who can publish to the command topic can print. "
            "Set MQTT_USERNAME and MQTT_PASSWORD and limit who may publish "
            f"to '{topic}' in the broker.")
    elif user == "your-username":
        warnings.append(
            "WARNING: MQTT_USERNAME is still the placeholder 'your-username'.")
    if (not _env_bool("MQTT_TLS", False, env)
            and broker not in ("localhost", "127.0.0.1", "::1")):
        warnings.append(
            "WARNING: MQTT_TLS is off, so the broker login and every print "
            "job cross the network unencrypted. Turn on MQTT_TLS if the "
            "broker is not on this machine.")
    return warnings


def install_term_handler(client):
    """SIGTERM -> set an Event; a helper thread does the real work.

    The handler runs on the main thread, which is inside loop_forever();
    calling client.publish/disconnect from it can deadlock on paho's
    out-packet mutex, so it only sets the Event.
    """
    stop = threading.Event()

    def _watch():
        stop.wait()
        graceful_shutdown(client)

    threading.Thread(target=_watch, name="shutdown", daemon=True).start()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    return stop


def start_settings_watcher(client, reload_event, interval=2.0):
    """Apply panel changes without restarting the container.

    Paper width and printer device take effect on the next job. A broker or
    login change disconnects the client so the main loop reconnects with the
    new values; the same client object is reused, so the availability poller
    and its retained topic keep working.
    """
    def _loop():
        seen = settings_store.fingerprint()
        while not SHUTDOWN.wait(interval):
            current = settings_store.fingerprint()
            if current == seen:
                continue
            seen = current
            try:
                changed = apply_runtime_settings(client=client)
            except Exception as e:
                print(f"Ignoring settings change: {e}")
                continue
            if not changed:
                continue
            print(f"Settings changed: {', '.join(changed)}")
            if any(key in changed for key in RECONNECT_KEYS):
                reload_event.set()
                try:
                    client.disconnect()
                except Exception as e:
                    print(f"Could not reconnect for new settings yet: {e}")

    threading.Thread(target=_loop, name="settings", daemon=True).start()


def on_connect(client, userdata, flags, rc):
    """Callback for when the client connects to the MQTT broker."""
    global AVAILABILITY_THREAD
    if rc == 0:
        print("Connected to MQTT broker!")
        # QoS 1 subscription: paho delivers retained/queued messages at the
        # subscription QoS, so a QoS 0 default would drop HA's qos-2 publishes
        # that arrive while the container is disconnected. Clean session
        # still loses genuine broker-offline messages - that is inherent.
        client.subscribe(topic, qos=1)
        # Retained discovery configs: published on every connect so a broker
        # that lost them (or a renamed topic) still ends up with the right
        # entities in Home Assistant.
        if HA_DISCOVERY:
            for config_topic, config in discovery_messages():
                client.publish(config_topic, json.dumps(config), qos=1,
                               retain=True)
        # Overwrite a retained "offline" (our will, or the broker's after a
        # drop) now rather than at the next 60s poll.
        with _AVAIL_LOCK:
            if not SHUTDOWN.is_set():
                client.publish(availability_topic, LAST_AVAILABILITY,
                               qos=1, retain=True)
        # Make the retained counters exist (0) so HA sensors resolve at once.
        worker = get_worker()
        publish_counters(worker.dropped, worker.failed)
        # Start publishing availability - ONCE. paho calls on_connect again
        # on every auto-reconnect; spawning the thread here would accumulate
        # duplicate 60s pollers per outage (and multiply device openers).
        if AVAILABILITY_THREAD is None or not AVAILABILITY_THREAD.is_alive():
            AVAILABILITY_THREAD = publish_availability(client)
    else:
        reasons = {1: "unacceptable protocol version", 2: "identifier rejected",
                   3: "server unavailable", 4: "bad username or password",
                   5: "not authorized"}
        print(f"Failed to connect, return code {rc} "
              f"({reasons.get(rc, 'unknown reason')})")



class PrintWorker:
    """Runs print jobs one at a time, in arrival order, off the network thread.

    paho runs on_message on its network thread; gs and the device write
    can take many seconds there and would stall keepalives and every other
    message. Callbacks only enqueue; this single daemon thread does the work.
    """

    _STOP = object()   # sentinel: wakes a blocked worker on shutdown

    def __init__(self, maxsize, reporter=None, max_per_minute=0):
        self._reporter = reporter   # (kind, label, detail, dropped, failed)
        self._max_per_minute = max_per_minute   # 0 = no limit
        self._accepted = collections.deque()    # monotonic times of accepted jobs
        self._queue = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.dropped = 0
        self.failed = 0

    def _report(self, kind, label, detail):
        if self._reporter is None:
            return
        try:
            with self._lock:
                dropped, failed = self.dropped, self.failed
            self._reporter(kind, label, detail, dropped, failed)
        except Exception as e:
            print(f"Error reporting {kind} job: {e}")

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run, name="print-worker", daemon=True)
                self._thread.start()

    def submit(self, job, label=""):
        """Queue job (a zero-arg callable); False if dropped because full.

        label (the job title) is only used in drop/failure reports.

        Never blocks. Starts the worker on first use.
        """
        self.start()
        if not self._rate_ok():
            with self._lock:
                self.dropped += 1
                total = self.dropped
            print(f"WARNING: more than {self._max_per_minute} jobs in a minute; "
                  f"dropped newest message ({total} dropped since start)")
            self._report("dropped", label, "rate limit")
            return False
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
                total = self.dropped
            print(f"WARNING: print queue full ({self._queue.maxsize} waiting); "
                  f"dropped newest message ({total} dropped since start)")
            self._report("dropped", label, "")
            return False

    def _rate_ok(self):
        """Sliding one-minute window; records the job when it is accepted."""
        if self._max_per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            while self._accepted and now - self._accepted[0] > 60:
                self._accepted.popleft()
            if len(self._accepted) >= self._max_per_minute:
                return False
            self._accepted.append(now)
            return True

    def reject(self, label, detail):
        """Count and publish a message that was refused before queueing."""
        with self._lock:
            self.failed += 1
        self._report("failed", label, detail)

    def _run(self):
        while not self._stop.is_set():
            job = self._queue.get()
            try:
                if job is self._STOP or self._stop.is_set():
                    return
                try:
                    job()
                except Exception as e:   # one bad job must not kill the worker
                    print(f"Error printing job: {e}")
                    with self._lock:
                        self.failed += 1
                    self._report("failed", getattr(job, "label", ""),
                                 f"{type(e).__name__}: {e}")
            finally:
                self._queue.task_done()

    def flush(self):
        """Block until every queued job has finished (tests/diagnostics)."""
        self._queue.join()

    def stop(self, timeout=SHUTDOWN_JOIN_TIMEOUT):
        """Stop after the job in flight; queued jobs are discarded.

        Returns True if the thread exited within timeout.
        """
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        discarded = 0
        try:   # make room for the sentinel by discarding waiting jobs
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
                discarded += 1
        except queue.Empty:
            pass
        if discarded:
            print(f"Shutdown: discarded {discarded} queued print job(s)")
        try:
            self._queue.put_nowait(self._STOP)
        except queue.Full:
            pass
        thread.join(timeout)
        if thread.is_alive():
            print(f"Print worker still busy after {timeout}s; exiting anyway")
            return False
        return True


_WORKER = None
_WORKER_LOCK = threading.Lock()


def get_worker():
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = PrintWorker(PRINT_QUEUE_SIZE, reporter=report_event,
                                 max_per_minute=MAX_JOBS_PER_MINUTE)
        return _WORKER


def flush_jobs():
    get_worker().flush()


def shutdown_worker(timeout=SHUTDOWN_JOIN_TIMEOUT):
    global _WORKER
    with _WORKER_LOCK:
        worker, _WORKER = _WORKER, None
    return worker.stop(timeout) if worker else True


MAX_TITLE_CHARS = 200
MAX_MESSAGE_CHARS = 65536    # MQTT intake cap: one line ≈ 19pt of page

def on_message(client, userdata, msg):
    """Callback for when a message is received."""
    # errors="ignore": replace would turn one bad byte into U+FFFD, which
    # reportlab's WinAnsi fonts cannot encode - the whole receipt would be
    # silently dropped. Dropping bad bytes keeps the rest printable.
    # A retained command would reprint on every reconnect/restart; commands
    # are events, so ignore broker-replayed retained ones.
    if getattr(msg, "retain", False) is True:
        print(f"Ignoring retained message on topic {msg.topic}")
        return
    payload_text = msg.payload.decode(errors="ignore")
    print(f"Received message on topic {msg.topic}")
    try:
        payload = json.loads(payload_text)
        if not isinstance(payload, dict):
            raise ValueError("Payload is not a JSON object")
        printer_name = payload.get("printer_name")
        # Coerce: JSON can carry numbers/lists/null here, which would fail
        # later on the worker thread. Newlines would draw as one long line.
        title = " ".join(str(payload.get("title", "Print Job")).split())
        title = title[:MAX_TITLE_CHARS]
        message = str(payload.get("message", "No message provided"))

        if not printer_name:
            get_worker().reject(title, "missing printer_name")
            print("Error handling message: Missing 'printer_name' in payload")
            return

        # Cap intake: page height and gs raster memory scale with the
        # message; a 64KB message already means a several-meter receipt.
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS]
            print(f"Message truncated to {MAX_MESSAGE_CHARS} chars")

        # PDF -> pbmraw raster -> ESC/POS -> printer device (direct write;
        # EBUSY retries while the paper poll holds the device). Runs on the
        # print worker thread, not paho's network thread; print_job is
        # looked up at run time.
        job = lambda: print_job(title, message)
        job.label = title   # only echoed (capped) in drop/failure reports
        get_worker().submit(job, label=title)

    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
        get_worker().reject("", "message is not valid JSON")
    except Exception as e:
        print(f"Error handling message: {e}")
        get_worker().reject("", "message is not a JSON object")


def break_word(word, max_width, font="Helvetica", size=10):
    """Split one unbreakable word into chunks that each fit max_width.

    Always makes progress (at least one character per chunk) so a glyph
    wider than the line cannot loop forever.
    """
    chunks = []
    chunk = ""
    for ch in word:
        if chunk and pdfmetrics.stringWidth(chunk + ch, font, size) > max_width:
            chunks.append(chunk)
            chunk = ch
        else:
            chunk += ch
    if chunk:
        chunks.append(chunk)
    return chunks


def generate_pdf(title, message, pdf_path):
    """Generate a PDF optimized for thermal receipt printers."""
    # Lay the page out to the printable area, not the roll width: 48 mm paper
    # gives 384 px, a "58 mm" roll also gives 384 px (48 mm printable) and an
    # 80 mm roll gives 576 px (72 mm printable) at 203 dpi. Using the roll
    # width here would push the right margin past the raster and clip it.
    page_width = escpos.PRINT_AREA_MM * mm
    margin = 5 * mm  # Margins for the receipt
    content_width = page_width - (2 * margin)

    # Split message into wrapped lines based on content width
    lines = []
    for part in message.split('\n'):
        words = part.split()
        if not words:
            lines.append("")   # keep blank lines as real vertical gaps
            continue
        current_line = ""

        for word in words:
            # A word wider than the whole line would overflow the right
            # margin: hard-break it into line-sized chunks.
            if pdfmetrics.stringWidth(word, "Helvetica", 10) > content_width:
                if current_line:
                    lines.append(current_line)
                pieces = break_word(word, content_width)
                lines.extend(pieces[:-1])
                current_line = pieces[-1]
                continue
            # Check if adding the next word exceeds the width
            test_line = f"{current_line} {word}".strip()
            text_width = pdfmetrics.stringWidth(test_line, "Helvetica", 10)

            if text_width <= content_width:
                current_line = test_line
            else:
                # Line is too long, add the current line to lines and start a new one
                if current_line:   # first word too wide: no phantom blank line
                    lines.append(current_line)
                current_line = word

        # Append the final line if it exists
        if current_line:
            lines.append(current_line)

    # Nothing below the raster cap is ever printed, so stop here: a huge
    # message must not make a huge PDF for ghostscript to chew on.
    line_height = 12  # Line height in points
    max_lines = int(RASTER_HEIGHT_PX * 72 / RASTER_DPI_Y / line_height) + 4
    if len(lines) > max_lines:
        lines = lines[:max_lines]

    # Calculate the required height for the content
    calculated_height = margin + (len(lines) + 2) * line_height  # Extra lines for title and footer

    # Ensure the page height is always greater than the width for portrait orientation
    page_height = max(calculated_height, page_width + 1)

    c = canvas.Canvas(pdf_path, pagesize=(page_width, page_height))

    # We need to start drawing from the top of the page
    y = page_height - margin

    # Title Section
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, title)

    # Divider
    y -= line_height
    c.line(margin, y, page_width - margin, y)

    # Message Section
    y -= line_height
    c.setFont("Helvetica", 10)
    for line in lines:
        c.drawString(margin, y, line)
        y -= line_height

    # Divider
    y -= line_height
    c.line(margin, y, page_width - margin, y)

    c.save()
    print(f"PDF saved to {pdf_path}")
    return page_height


def pdf_to_escpos(pdf_path, settings, page_height_pt=None):
    """Rasterize a PDF with ghostscript pbmraw and encode it as ESC/POS.

    pbmraw emits packed 1 bpp rows, black = 1 - the same bit order and
    polarity as both CUPS raster and ESC/POS GS v 0 raster data.

    page_height_pt is the page height in points from generate_pdf(); the
    -g raster height is sized from it because -dFIXEDMEDIA pins the device
    to -g and would silently clip any receipt taller than the fixed
    480-line (60 mm) default. Falls back to RASTER_HEIGHT_PX when unset
    (e.g. scripts/smoke_test.sh calls this with two arguments).
    """
    height_px = RASTER_HEIGHT_PX
    if page_height_pt is not None:
        height_px = (int(page_height_pt) * RASTER_DPI_Y) // 72 + 4
        if height_px > RASTER_HEIGHT_PX:
            print(f"Receipt raster needs {height_px} lines; capped to "
                  f"RASTER_HEIGHT_PX={RASTER_HEIGHT_PX}, lower text is cut")
            height_px = RASTER_HEIGHT_PX
    try:
        gs = subprocess.run(
            ["gs", "-dQUIET", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dNOPROMPT",
             "-sOutputFile=%stdout",
             "-sDEVICE=pbmraw", f"-r{RASTER_DPI_X}x{RASTER_DPI_Y}",
             f"-g{RASTER_WIDTH_PX}x{height_px}",
             # -dFIXEDMEDIA: pins the device to -g; without it gs resizes
             # the device to the PDF page, breaking the 384 px width.
             "-dFIXEDMEDIA",
             "-dTextAlphaBits=4", "-dGraphicsAlphaBits=1",
             pdf_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            timeout=GS_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"gs timed out after {GS_TIMEOUT}s") from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="ignore").strip()
        raise RuntimeError(f"gs failed rc={e.returncode}: "
                           f"{stderr[-500:] or '(no stderr)'}") from e
    out, _pages = escpos.render_all(gs.stdout, settings)
    return out


def _write_device(path, data):
    """Write data to a character device or regular file, all of it.

    O_SYNC so bytes hit the printer before we report success. Errors,
    EBUSY included, propagate as OSError; send_raw_to_printer retries EBUSY.
    """
    fd = os.open(path, os.O_WRONLY | os.O_SYNC)
    try:
        written = 0
        while written < len(data):
            n = os.write(fd, data[written:])
            if n <= 0:
                raise OSError(errno.EIO, "device accepted 0 bytes")
            written += n
        return written
    finally:
        os.close(fd)


DEVICE_BUSY_RETRIES = 8      # EBUSY: paper poll or another job holds usblp
DEVICE_BUSY_BACKOFF = 0.25   # seconds between retries (2s worst case)


def write_device_retrying(path, data):
    """_write_device with EBUSY retried (see send_raw_to_printer); returns bytes written."""
    last_err = None
    for attempt in range(DEVICE_BUSY_RETRIES):
        try:
            return _write_device(path, data)
        except OSError as e:
            if e.errno != errno.EBUSY:
                raise
            last_err = e
            print(f"Device busy (attempt {attempt + 1}/{DEVICE_BUSY_RETRIES}); "
                  "retrying shortly")
            time.sleep(DEVICE_BUSY_BACKOFF)
    raise last_err


def send_raw_to_printer(escpos_bytes):
    """Deliver a pre-rendered ESC/POS bytestring to the printer.

    Writes directly to the printer character device (PRINTER_DEVICE).
    CUPS is deliberately out of the data path: its file: backend refuses
    to write for 'raw' queues (file devices cannot be used with raw print
    queues - a PPD is required), which silently completed jobs here with
    zero bytes delivered while reporting success. A direct write is
    verifiable: bytes reach the device or the exception says why not.

    EBUSY is retried, not fatal: the 60s paper poll holds the device for
    up to a second (usblp allows a single opener), and falling through
    from there to lp would be the old silent-loss black hole again.
    Enodev/EIO are reported loudly and left failing - no fake success.
    """
    if not printer_device:
        raise RuntimeError("PRINTER_DEVICE is not set; no printer to write to")
    n = write_device_retrying(printer_device, escpos_bytes)
    print(f"Wrote {n} ESC/POS bytes to {printer_device}.")


def print_job(title, message):
    """Full pipeline for one MQTT message: PDF, raster, ESC/POS, device."""
    settings = escpos.EscposSettings.from_env()

    fd, pdf_path = tempfile.mkstemp(prefix="print_job_", suffix=".pdf")
    os.close(fd)
    try:
        page_height_pt = generate_pdf(title, message, pdf_path)
        escpos_bytes = pdf_to_escpos(pdf_path, settings, page_height_pt)
    finally:
        try:
            os.unlink(pdf_path)
        except OSError:
            pass

    send_raw_to_printer(escpos_bytes)
    print(f"Printed {len(escpos_bytes)} ESC/POS bytes.")


if __name__ == "__main__":
    # paho-mqtt 2.x makes CallbackAPIVersion a required first argument; 1.x
    # (what Alpine ships) does not know it. Pick the calling convention from
    # what actually imported.
    if mqtt is not None:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                 protocol=mqtt.MQTTv311)
        except AttributeError:
            # paho 1.x: VERSION1 does not exist / argument not accepted
            client = mqtt.Client(protocol=mqtt.MQTTv311)
    else:
        client = None

    for line in startup_warnings():
        print(line)
    if username and password:
        client.username_pw_set(username, password)
    configure_tls(client)
    _CLIENT = client

    client.on_connect = on_connect
    client.on_message = on_message
    # LWT: if the container dies without a disconnect packet, the broker
    # flips the retained availability to offline so HA's offline alert
    # (README) can actually fire instead of the stale "online" standing.
    client.will_set(availability_topic, "offline", qos=1, retain=True)

    # docker stop -> SIGTERM -> graceful_shutdown() on a helper thread ->
    # loop_forever() returns and the cleanup below runs.
    install_term_handler(client)

    # Panel changes are picked up from the settings file: paper width and
    # printer device on the next job, broker/login by reconnecting below.
    reload_event = threading.Event()
    start_settings_watcher(client, reload_event)

    try:
        while connect_with_retry(client, broker, broker_port, 60):
            try:
                client.loop_forever()
            except Exception as e:
                if not reload_event.is_set():
                    raise
                # paho can raise when disconnect() lands while its reader is
                # between select() and recv(). The reconnect below opens a
                # fresh socket, so treat it as the end of this connection.
                print(f"MQTT loop stopped for the settings change ({e}).")
            if not reload_event.is_set():
                # Not a settings change: the shutdown path disconnected us.
                break
            reload_event.clear()
            if username and password:
                client.username_pw_set(username, password)
            else:
                client.username_pw_set(None)
            configure_tls(client)
            # The last will is only used on the next CONNECT, so set it from
            # the (possibly changed) availability topic before reconnecting.
            client.will_set(availability_topic, "offline", qos=1, retain=True)
            print("Reconnecting with the settings from the control panel...")
    except Exception as e:
        print(f"Failed to start MQTT handler: {e}")
        raise SystemExit(1)
    finally:
        shutdown_worker()
