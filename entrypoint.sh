#!/bin/bash

##echo "Setting NOFILE limit to 65536"
##ulimit -n 65536

# Copy the custom cupsd.conf file
if [ -f /app/cupsd.conf ]; then
  echo "Copying custom cupsd.conf..."
  cp /app/cupsd.conf /etc/cups/cupsd.conf
  chmod 644 /etc/cups/cupsd.conf
  chown root:lp /etc/cups/cupsd.conf
else
  echo "Custom cupsd.conf not found!"
fi

# Copy the custom cups-files.conf (FileDevice Yes, required by the file backend)
if [ -f /app/cups-files.conf ]; then
  echo "Copying custom cups-files.conf..."
  cp /app/cups-files.conf /etc/cups/cups-files.conf
  chmod 640 /etc/cups/cups-files.conf
  chown root:lp /etc/cups/cups-files.conf
else
  echo "Custom cups-files.conf not found!"
fi

# Create admin user if it doesn't already exist
ADMIN_USER=${ADMIN_USER:-admin}
ADMIN_PASS=${ADMIN_PASS:-}

if [ -z "$ADMIN_PASS" ]; then
  echo "ADMIN_PASS is not set; refusing to start with a default CUPS admin password."
  exit 1
fi

if ! id -u $ADMIN_USER > /dev/null 2>&1; then
  echo "Creating admin user..."
  adduser --disabled-password --gecos "" $ADMIN_USER
  echo "$ADMIN_USER:$ADMIN_PASS" | chpasswd
  usermod -aG lpadmin $ADMIN_USER
else
  echo "Admin user already exists."
fi

# Start D-Bus
echo "Starting D-Bus..."
mkdir -p /var/run/dbus
service dbus start || true

# Stop any running CUPS processes
echo "Ensuring no conflicting CUPS processes..."
pkill cupsd || true

# Start CUPS service
echo "Starting CUPS service..."
service cups start
if [ $? -eq 0 ]; then
  echo "CUPS service started successfully."
else
  echo "Failed to start CUPS service."
  exit 1
fi

# Wait for CUPS to initialize
sleep 2

# Keep a CUPS queue for monitoring (lpstat -p in the availability thread).
# It is NOT in the data path: the handler writes ESC/POS bytes straight to
# PRINTER_DEVICE. CUPS 2.4.10+ silently completes jobs on raw+file: queues
# without writing anything ("File devices cannot be used with 'raw' print
# queues - a PPD is required"), so printing must not go through lp.
# The queue's file: URI needs FileDevice Yes (set in cups-files.conf);
# PRINTER_URI defaults to the same device the handler writes.
PRINTER_NAME=${PRINTER_NAME:-ReceiptPrinter}
PRINTER_DEVICE=${PRINTER_DEVICE:-${PAPER_DEVICE:-/dev/usb/lp1}}
PRINTER_URI=${PRINTER_URI:-file:$PRINTER_DEVICE}

if ! lpstat -p "$PRINTER_NAME" > /dev/null 2>&1; then
  echo "Creating $PRINTER_NAME..."
  lpadmin -p "$PRINTER_NAME" \
    -v "$PRINTER_URI" \
    -m raw \
    -D "Zijiang ZJ-58" \
    -o printer-error-policy=abort-job \
    -E
else
  echo "$PRINTER_NAME already exists."
  lpadmin -p "$PRINTER_NAME" -o printer-error-policy=abort-job
fi

lpadmin -d "$PRINTER_NAME"
cupsaccept "$PRINTER_NAME"
cupsenable "$PRINTER_NAME"

# Tail the CUPS log in the background
echo "Tailing CUPS logs..."
tail -F /var/log/cups/error_log &

# Start the Flask web control panel
echo "Starting Flask web control panel..."
python3 -u /app/web_control_panel.py &

# Start the MQTT handler (-u: its stdout is a pipe; unbuffered so prints
# appear in docker logs immediately)
echo "Starting MQTT handler..."
python3 -u /app/printer_mqtt_handler.py
if [ $? -ne 0 ]; then
  echo "Failed to start MQTT handler."
  exit 1
fi
