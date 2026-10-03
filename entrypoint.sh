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

# Start D-Bus (avahi-daemon will not start without it)
echo "Starting D-Bus..."
mkdir -p /var/run/dbus
service dbus start || true

# Start Avahi Daemon
echo "Starting Avahi Daemon..."
service avahi-daemon start || true

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

# Ensure ReceiptPrinter exists and is configured as a raw queue.
# Printing is done entirely in-app (PDF -> ghostscript pbmraw ->
# app/escpos.py -> ESC/POS bytes), so CUPS needs no PPD and no filter
# chain; it only moves bytes to the file-device backend.
PRINTER_NAME=${PRINTER_NAME:-ReceiptPrinter}
PRINTER_URI=${PRINTER_URI:-file:/dev/usb/lp1}

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
tail -f /var/log/cups/error_log &

# Start the Flask web control panel
echo "Starting Flask web control panel..."
python3 /app/web_control_panel.py &

# Start the MQTT handler
echo "Starting MQTT handler..."
python3 /app/printer_mqtt_handler.py
if [ $? -ne 0 ]; then
  echo "Failed to start MQTT handler."
  exit 1
fi