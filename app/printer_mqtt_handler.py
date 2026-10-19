try:
    import paho.mqtt.client as mqtt
except ImportError:  # imported for pipeline unit use (tests) without broker deps
    mqtt = None
import errno
import os
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

# Read broker, username, and password from environment variables
broker = os.getenv("MQTT_BROKER", "localhost")
username = os.getenv("MQTT_USERNAME")
password = os.getenv("MQTT_PASSWORD")
topic = os.getenv("MQTT_TOPIC", "printer/commands")
availability_topic = "printer/availability"
last_error_topic = os.getenv("MQTT_LAST_ERROR_TOPIC", "printer/last_error")
dropped_total_topic = os.getenv("MQTT_DROPPED_TOTAL_TOPIC", "printer/dropped_total")
failed_total_topic = os.getenv("MQTT_FAILED_TOTAL_TOPIC", "printer/failed_total")
paper_topic = os.getenv("MQTT_PAPER_TOPIC", "printer/paper")
paper_low_topic = os.getenv("MQTT_PAPER_LOW_TOPIC", "printer/paper_low")
printer_device = os.getenv("PRINTER_DEVICE", os.getenv("PAPER_DEVICE", "/dev/usb/lp1"))

# Default receipt printer settings: ghostscript renders the reportlab PDF at
# 203 dpi into a 384 px wide raster (48 mm roll), matching the old PPD's
# X48MMY60MM page. The raster height is sized per receipt from the PDF page
# height; RASTER_HEIGHT_PX is a hard cap - taller receipts are truncated
# with a loud warning, not silently cut.
RASTER_DPI_X = int(os.getenv("RASTER_DPI_X", "203"))
RASTER_DPI_Y = int(os.getenv("RASTER_DPI_Y", "203"))
RASTER_WIDTH_PX = int(os.getenv("RASTER_WIDTH_PX", "384"))
RASTER_HEIGHT_PX = int(os.getenv("RASTER_HEIGHT_PX", "480"))

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
        text = f"queue full, job {title!r} dropped" if kind == "dropped" \
            else f"job {title!r} failed: {(detail or '')[:ERROR_DETAIL_CAP]}"
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


def publish_availability(client, interval=60):
    """Publish printer availability periodically."""
    printer_name = os.getenv("PRINTER_NAME", "ReceiptPrinter")

    def publish_status():
        last_status = None
        last_paper = None
        last_low = None
        warned_no_sensor = False
        while True:
            try:
                # Check if the printer is available
                result = subprocess.run(["lpstat", "-p", printer_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        timeout=LPSTAT_TIMEOUT)
                if result.returncode != 0:
                    print(f"lpstat failed rc={result.returncode}: {result.stderr.strip()}")
                    status = "offline"
                elif not result.stdout.strip() or "disabled" in result.stdout:
                    status = "offline"
                else:
                    # "processing" counts as online; only "disabled" means down
                    status = "online"
            except Exception as e:
                print(f"Error checking printer status: {e}")
                status = "offline"

            # Only log and publish on change; the topic is retained
            if status != last_status:
                print(f"Publishing status: {status}")
                last_status = status
            with _AVAIL_LOCK:
                if SHUTDOWN.is_set():
                    return
                client.publish(availability_topic, str(status), qos=1, retain=True)

            # Paper sensor. Polled on the same cadence so only one thread
            # ever touches the device. An unanswered query leaves the last
            # known value retained rather than reporting a false "out".
            result = query_paper()
            if result is None:
                if not warned_no_sensor and last_paper is None:
                    print(f"No paper status from {printer_device} "
                          "(printer may be unidirectional)")
                    warned_no_sensor = True
            else:
                paper, low = result

                payload = "ON" if paper else "OFF"
                if paper != last_paper:
                    print(f"Publishing paper: {payload}")
                    last_paper = paper
                client.publish(paper_topic, payload, qos=1, retain=True)

                low_payload = "ON" if low else "OFF"
                if low != last_low:
                    print(f"Publishing paper_low: {low_payload}")
                    last_low = low
                client.publish(paper_low_topic, low_payload, qos=1, retain=True)

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

    def __init__(self, maxsize, reporter=None):
        self._reporter = reporter   # (kind, label, detail, dropped, failed)
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
            _WORKER = PrintWorker(PRINT_QUEUE_SIZE, reporter=report_event)
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
        printer_name = payload.get("printer_name")
        # Coerce: JSON can carry numbers/lists/null here, which would fail
        # later on the worker thread.
        title = str(payload.get("title", "Print Job"))[:MAX_TITLE_CHARS]
        message = str(payload.get("message", "No message provided"))

        if not printer_name:
            raise ValueError("Missing 'printer_name' in payload")

        # Cap intake: page height and gs raster memory scale with the
        # message; a 64KB message already means a several-meter receipt.
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS]
            print(f"Message truncated to {MAX_MESSAGE_CHARS} chars")

        # PDF -> pbmraw raster -> ESC/POS -> printer device (direct write;
        # EBUSY retries while the paper poll holds the device). Runs on the
        # print worker thread, not paho's network thread; print_job is
        # looked up at run time.
        job = lambda: print_job(title, message, printer_name)  # noqa: E731
        job.label = title   # only echoed (capped) in drop/failure reports
        get_worker().submit(job, label=title)

    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
    except Exception as e:
        print(f"Error handling message: {e}")


def generate_pdf(title, message, pdf_path):
    """Generate a PDF optimized for thermal receipt printers."""
    # Fixed page width; height is dynamic
    page_width = 48 * mm  # must match PRINTER_RASTER_WIDTH_PX or gs scales/crops
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

    # Calculate the required height for the content
    line_height = 12  # Line height in points
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


def send_raw_to_printer(printer_name, escpos_bytes):
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
    last_err = None
    for attempt in range(DEVICE_BUSY_RETRIES):
        try:
            n = _write_device(printer_device, escpos_bytes)
            print(f"Wrote {n} ESC/POS bytes to {printer_device}.")
            return
        except OSError as e:
            if e.errno == errno.EBUSY:
                last_err = e
                print(f"Device busy (attempt {attempt + 1}/{DEVICE_BUSY_RETRIES}); "
                      "retrying shortly")
                time.sleep(DEVICE_BUSY_BACKOFF)
                continue
            raise
    raise last_err


def print_job(title, message, printer_name):
    """Full pipeline for one MQTT message: PDF, raster, ESC/POS, queue."""
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

    send_raw_to_printer(printer_name, escpos_bytes)
    print(f"Printed {len(escpos_bytes)} ESC/POS bytes.")
    return escpos_bytes


if __name__ == "__main__":
    # Debian trixie ships paho-mqtt 2.x, which makes CallbackAPIVersion a
    # required first argument; bookworm's 1.x does not know it. Pick the
    # calling convention from what actually imported.
    if mqtt is not None:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                 protocol=mqtt.MQTTv311)
        except AttributeError:
            # paho 1.x: VERSION1 does not exist / argument not accepted
            client = mqtt.Client(protocol=mqtt.MQTTv311)
    else:
        client = None

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

    try:
        client.connect(broker, broker_port, 60)
        client.loop_forever()
    except Exception as e:
        print(f"Failed to start MQTT handler: {e}")
        raise SystemExit(1)
    finally:
        shutdown_worker()
