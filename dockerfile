# Base image
# trixie ships CUPS 2.5.x: PPD-based printer drivers (lpadmin -P, *cupsFilter)
# are gone from the scheduler, which is fine here - the queue is raw and the
# ESC/POS emission lives in app/escpos.py, not in a CUPS filter.
FROM debian:trixie-slim

# Set environment variables for flexibility
ENV CUPS_CONF_DIR=/etc/cups \
    APP_DIR=/app

# Runtime packages.
# Python deps come from apt, not pip: Debian marks the system Python
# environment as externally managed (PEP 668) and bare `pip3 install` fails.
#
# No CUPS printer driver is installed: printing is PDF -> ghostscript
# pbmraw -> app/escpos.py -> lp on a raw CUPS queue, so ghostscript replaces
# the compiled filter and the image is identical on amd64 and arm64.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cups \
    cups-client \
    ghostscript \
    dbus \
    python3 \
    python3-paho-mqtt \
    python3-flask \
    python3-reportlab \
    && rm -rf /var/lib/apt/lists/*

# Expose the CUPS web interface
EXPOSE 631

# Copy the pre-configured CUPS configs and entrypoint script
COPY configs/cupsd.conf $APP_DIR/cupsd.conf
COPY configs/cups-files.conf $APP_DIR/cups-files.conf
COPY entrypoint.sh $APP_DIR/entrypoint.sh

# Copy the templates directory
COPY app/templates /app/templates

# Ensure permissions are correct
RUN chmod 644 $APP_DIR/cupsd.conf && \
    chmod +x $APP_DIR/entrypoint.sh

# Copy the MQTT handler script
COPY app/printer_mqtt_handler.py $APP_DIR/printer_mqtt_handler.py
# Copy the ESC/POS raster encoder (the Python stand-in for the retired C filter)
COPY app/escpos.py $APP_DIR/escpos.py
# Copy the web control panel script
COPY app/web_control_panel.py $APP_DIR/web_control_panel.py
# A file for the control panel's test print button (raw queue: bytes go
# straight to the printer)
RUN printf "PrintMQTTify test page\n" > /app/test_print.txt
WORKDIR $APP_DIR

# Use entrypoint script for runtime configuration and startup
ENTRYPOINT ["/app/entrypoint.sh"]