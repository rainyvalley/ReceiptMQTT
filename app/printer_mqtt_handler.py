try:
    import paho.mqtt.client as mqtt
except ImportError:  # imported for pipeline unit use (tests) without broker deps
    mqtt = None
import errno
import os
import select
import subprocess
import tempfile
import time
import threading
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import mm
except ImportError:  # pipeline unit use without reportlab installed
    canvas = None
    mm = 2.834645669  # points per mm, reportlab's conversion
import json

import escpos

# Read broker, username, and password from environment variables
broker = os.getenv("MQTT_BROKER", "localhost")
username = os.getenv("MQTT_USERNAME")
password = os.getenv("MQTT_PASSWORD")
topic = os.getenv("MQTT_TOPIC", "printer/commands")
availability_topic = "printer/availability"
paper_topic = os.getenv("MQTT_PAPER_TOPIC", "printer/paper")
paper_low_topic = os.getenv("MQTT_PAPER_LOW_TOPIC", "printer/paper_low")
paper_device = os.getenv("PAPER_DEVICE", "/dev/usb/lp1")

# Default receipt printer settings: ghostscript renders the reportlab PDF at
# 203 dpi into a 384 px wide raster (48 mm roll), matching the old PPD's
# X48MMY60MM page. PRINTER_RASTER_HEIGHT picks the page height in raster
# lines; it only caps gs page allocation - actual receipts are cut to content.
RASTER_DPI_X = int(os.getenv("RASTER_DPI_X", "203"))
RASTER_DPI_Y = int(os.getenv("RASTER_DPI_Y", "203"))
RASTER_WIDTH_PX = int(os.getenv("RASTER_WIDTH_PX", "384"))
RASTER_HEIGHT_PX = int(os.getenv("RASTER_HEIGHT_PX", "480"))

# DLE EOT 4 - real-time paper sensor status request. "Real-time" means the
# printer answers immediately rather than queueing the request behind print
# data, so this works even while a job is running.
PAPER_QUERY = b"\x10\x04\x04"


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
        fd = os.open(paper_device, os.O_RDWR | os.O_NONBLOCK)
        os.write(fd, PAPER_QUERY)
        ready, _, _ = select.select([fd], [], [], 1.0)
        if not ready:
            return None
        response = os.read(fd, 8)
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
                result = subprocess.run(["lpstat", "-p", printer_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
            client.publish(availability_topic, str(status), qos=1, retain=True)

            # Paper sensor. Polled on the same cadence so only one thread
            # ever touches the device. An unanswered query leaves the last
            # known value retained rather than reporting a false "out".
            result = query_paper()
            if result is None:
                if not warned_no_sensor and last_paper is None:
                    print(f"No paper status from {paper_device} "
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


def on_connect(client, userdata, flags, rc):
    """Callback for when the client connects to the MQTT broker."""
    if rc == 0:
        print("Connected to MQTT broker!")
        client.subscribe(topic)
        # Start publishing availability
        publish_availability(client)
    else:
        print(f"Failed to connect, return code {rc}")



def on_message(client, userdata, msg):
    """Callback for when a message is received."""
    payload_text = msg.payload.decode(errors="replace")
    print(f"Received message on topic {msg.topic}")
    try:
        payload = json.loads(payload_text)
        printer_name = payload.get("printer_name")
        title = payload.get("title", "Print Job")
        message = payload.get("message", "No message provided")

        if not printer_name:
            raise ValueError("Missing 'printer_name' in payload")

        # PDF -> pbmraw raster -> ESC/POS -> lp raw queue
        escpos_bytes = print_job(title, message, printer_name)
        print(f"Printed {len(escpos_bytes)} ESC/POS bytes.")

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
    # A throwaway canvas measures text; the real one is written below.
    measure = canvas.Canvas(os.devnull, pagesize=(page_width, 58))
    measure.setFont("Helvetica", 10)

    lines = []
    for part in message.split('\n'):
        words = part.split()
        current_line = ""

        for word in words:
            # Check if adding the next word exceeds the width
            test_line = f"{current_line} {word}".strip()
            text_width = measure.stringWidth(test_line)

            if text_width <= content_width:
                current_line = test_line
            else:
                # Line is too long, add the current line to lines and start a new one
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


def pdf_to_escpos(pdf_path, settings):
    """Rasterize a PDF with ghostscript pbmraw and encode it as ESC/POS.

    pbmraw emits packed 1 bpp rows, black = 1 - the same bit order and
    polarity as both CUPS raster and ESC/POS GS v 0 raster data.
    """
    gs = subprocess.run(
        ["gs", "-dQUIET", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dNOPROMPT",
         f"-sOutputFile=%stdout",
         f"-sDEVICE=pbmraw", f"-r{RASTER_DPI_X}x{RASTER_DPI_Y}",
         f"-g{RASTER_WIDTH_PX}x{RASTER_HEIGHT_PX}",
         "-dTextAlphaBits=4", "-dGraphicsAlphaBits=1",
         pdf_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    out, _pages = escpos.render_all(gs.stdout, settings)
    return out


def send_raw_to_printer(printer_name, escpos_bytes):
    """Queue a pre-rendered ESC/POS bytestring on a raw CUPS queue."""
    fd, raw_path = tempfile.mkstemp(prefix="print_job_", suffix=".escpos")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(escpos_bytes)
        result = subprocess.run(
            ["lp", "-d", printer_name, raw_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        print(f"Queued: {result.stdout.decode().strip()}")
    except subprocess.CalledProcessError as e:
        print(f"Failed to print. Error: {e.stderr.decode()}")
        raise
    finally:
        try:
            os.unlink(raw_path)
        except OSError:
            pass


def print_job(title, message, printer_name):
    """Full pipeline for one MQTT message: PDF, raster, ESC/POS, queue."""
    settings = escpos.EscposSettings.from_env()

    fd, pdf_path = tempfile.mkstemp(prefix="print_job_", suffix=".pdf")
    os.close(fd)
    try:
        generate_pdf(title, message, pdf_path)
        escpos_bytes = pdf_to_escpos(pdf_path, settings)
    finally:
        try:
            os.unlink(pdf_path)
        except OSError:
            pass

    send_raw_to_printer(printer_name, escpos_bytes)
    return escpos_bytes


if __name__ == "__main__":
    # Create an MQTT client instance
    client = mqtt.Client(protocol=mqtt.MQTTv311)

    # Set username and password if provided
    if username and password:
        client.username_pw_set(username, password)

    # Assign callback functions
    client.on_connect = on_connect
    client.on_message = on_message

    # Connect to the broker
    try:
        client.connect(broker, 1883, 60)
        # Start the MQTT loop
        client.loop_forever()
    except Exception as e:
        print(f"Failed to start MQTT handler: {e}")