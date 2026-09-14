import paho.mqtt.client as mqtt
import os
import subprocess
import time
import threading
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import mm
import json

# Read broker, username, and password from environment variables
broker = os.getenv("MQTT_BROKER", "localhost")
username = os.getenv("MQTT_USERNAME")
password = os.getenv("MQTT_PASSWORD")
topic = os.getenv("MQTT_TOPIC", "printer/commands")
availability_topic = "printer/availability"


def publish_availability(client, interval=60):
    """Publish printer availability periodically."""
    printer_name = os.getenv("PRINTER_NAME", "ReceiptPrinter")

    def publish_status():
        last_status = None
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
    print(f"Received message: {msg.payload.decode()} on topic {msg.topic}")
    try:
        payload = json.loads(msg.payload.decode())
        print(f"Parsed payload: {payload}")
        printer_name = payload.get("printer_name")
        title = payload.get("title", "Print Job")
        message = payload.get("message", "No message provided")

        if not printer_name:
            raise ValueError("Missing 'printer_name' in payload")

        # Generate a formatted PDF
        pdf_path = generate_pdf(title, message)

        # Send the PDF to the printer
        send_to_printer(printer_name, pdf_path)

    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
    except Exception as e:
        print(f"Error handling message: {e}")


def generate_pdf(title, message):
    """Generate a PDF optimized for thermal receipt printers."""
    try:
        # Fixed page width; height is dynamic
        page_width = 58 * mm  # 80mm in points
        margin = 5 * mm  # Margins for the receipt
        content_width = page_width - (2 * margin)

        # Split message into wrapped lines based on content width
        lines = []
        c = canvas.Canvas("/tmp/temp.pdf", pagesize=(page_width, 58))  # Create a temporary canvas for measuring

        # Set font to measure text width
        c.setFont("Helvetica", 10)
        
        # Split the message into wrapped lines
        for part in message.split('\n'):
            words = part.split()
            current_line = ""

            for word in words:
                # Check if adding the next word exceeds the width
                test_line = f"{current_line} {word}".strip()  # Allow leading space
                text_width = c.stringWidth(test_line)

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
        calculated_height = margin + (len(lines) + 3) * line_height  # Extra lines for title and footer

        # **FIX:** Ensure the page height is always greater than the width for portrait orientation
        page_height = max(calculated_height, page_width + 1)

        pdf_path = "/tmp/print_job.pdf"
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
        return pdf_path
    except Exception as e:
        print(f"Error generating PDF: {e}")
        return None


def send_to_printer(printer_name, pdf_path):
    """Send the generated PDF to the printer."""
    try:
        result = subprocess.run(
            ["lp", "-d", printer_name, pdf_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True
        )
        print(f"Printed successfully: {result.stdout.decode()}")
    except subprocess.CalledProcessError as e:
        print(f"Failed to print. Error: {e.stderr.decode()}")


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
