#!/bin/bash
set -u

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

if ! id -u "$ADMIN_USER" > /dev/null 2>&1; then
  echo "Creating admin user..."
  adduser --disabled-password --gecos "" "$ADMIN_USER"
  echo "$ADMIN_USER:$ADMIN_PASS" | chpasswd
  usermod -aG lpadmin "$ADMIN_USER"
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
if service cups start; then
  echo "CUPS service started successfully."
else
  echo "Failed to start CUPS service."
  exit 1
fi

# Wait for CUPS to accept commands (max ~20 s). A fixed sleep is racy: on
# a slow host the lpadmin calls below would fail silently and the queue
# would never exist. Poll at 0.2 s so a fast start is not padded to a
# whole second.
for _ in $(seq 1 100); do
  lpstat -r > /dev/null 2>&1 && break
  sleep 0.2
done

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

# Run the app as the unprivileged "receipt" user. It needs write access to
# the printer device: take the device's own group id (it is the host's, so it
# may differ from this image's "lp"). If the device still is not writable,
# say so and fall back to root so an existing install keeps printing.
APP_USER=receipt
APP_GROUPS=$(getent group lp | cut -d: -f3)
if [ -e "$PRINTER_DEVICE" ]; then
  DEVICE_GID=$(stat -c %g "$PRINTER_DEVICE")
  # never hand the app root's group
  [ "$DEVICE_GID" != "0" ] && APP_GROUPS="$APP_GROUPS,$DEVICE_GID"
fi
AS_APP=(setpriv --reuid="$APP_USER" --regid="$APP_USER" --groups "$APP_GROUPS")
if [ -e "$PRINTER_DEVICE" ] && ! "${AS_APP[@]}" test -w "$PRINTER_DEVICE"; then
  echo "WARNING: $PRINTER_DEVICE is not writable by the unprivileged '$APP_USER' user; running the app as root. Make the device group-writable on the host (for example mode 660, group lp) to drop root."
  AS_APP=()
fi

# Start the web control panel (gunicorn, one worker: the login lockout and
# test-print cooldown are kept in that process).
# HTTPS is opt-in: set both WEB_TLS_CERT and WEB_TLS_KEY (paths inside the
# container). With neither set the panel stays plain HTTP. A half-set or
# unreadable pair stops the start rather than silently serving plain HTTP.
TLS_ARGS=()
if [ -n "${WEB_TLS_CERT:-}" ] || [ -n "${WEB_TLS_KEY:-}" ]; then
  if [ -z "${WEB_TLS_CERT:-}" ] || [ -z "${WEB_TLS_KEY:-}" ]; then
    echo "ERROR: set both WEB_TLS_CERT and WEB_TLS_KEY, or neither."
    exit 1
  fi
  for f in "$WEB_TLS_CERT" "$WEB_TLS_KEY"; do
    if ! "${AS_APP[@]}" test -r "$f"; then
      echo "ERROR: $f is missing or not readable by the app user."
      exit 1
    fi
  done
  TLS_ARGS=(--certfile "$WEB_TLS_CERT" --keyfile "$WEB_TLS_KEY")
  echo "Control panel will use HTTPS."
fi
echo "Starting web control panel..."
"${AS_APP[@]}" python3 -u -m gunicorn --chdir /app --bind "${WEB_BIND:-0.0.0.0}:8080" "${TLS_ARGS[@]}" \
  --workers 1 --threads 4 --timeout 30 --access-logfile - \
  web_control_panel:app &

# Start the MQTT handler (-u: its stdout is a pipe; unbuffered so prints
# appear in docker logs immediately)
# exec: python becomes PID 1 so it receives docker's SIGTERM (bash as PID 1
# ignores it, and the handler's graceful shutdown never ran). setpriv execs
# the program, so it stays PID 1. A failing handler simply exits non-zero,
# which is what the container reports.
echo "Starting MQTT handler..."
exec "${AS_APP[@]}" python3 -u /app/printer_mqtt_handler.py
