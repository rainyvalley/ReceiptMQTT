# Base image
FROM debian:bookworm-slim

# Set environment variables for flexibility
ENV CUPS_CONF_DIR=/etc/cups \
    APP_DIR=/app \
    DRIVER_ARCHIVE=/tmp/sewoocupsinstall_amd64.tar.gz \
    DRIVER_TMP_DIR=/tmp/sewoocupsinstall_amd64

# Runtime packages.
# Python deps come from apt, not pip: bookworm marks the system Python
# environment as externally managed (PEP 668) and bare `pip3 install` fails.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cups \
    cups-client \
    cups-ppdc \
    libcupsimage2 \
    avahi-daemon \
    dbus \
    python3 \
    python3-paho-mqtt \
    python3-flask \
    python3-reportlab \
    && rm -rf /var/lib/apt/lists/*

# Expose the CUPS web interface
EXPOSE 631

# Copy the pre-configured CUPS config, PPD and entrypoint script
COPY configs/cupsd.conf $APP_DIR/cupsd.conf
COPY entrypoint.sh $APP_DIR/entrypoint.sh

# Copy the templates directory
COPY app/templates /app/templates

# Build and install the ZJ-58 ESC/POS filter.
# setup.sh is bypassed deliberately: it stops and starts CUPS, which is
# meaningless during a build, and it falls back to the prebuilt binary when
# the compile fails. Compiling here means rastertozj is linked against this
# image's own libcupsimage.
COPY drivers/SEWOO/sewoocupsinstall_amd64.tar.gz $DRIVER_ARCHIVE
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libcups2-dev libcupsimage2-dev && \
    tar -zxf $DRIVER_ARCHIVE -C /tmp && \
    cd $DRIVER_TMP_DIR && \
    make && \
    install -m 755 -o root -g root rastertozj /usr/lib/cups/filter/rastertozj && \
    mkdir -p /usr/share/cups/model/zjiang && \
    cp ppd/zj58.ppd ppd/zj80.ppd /usr/share/cups/model/zjiang/ && \
    cd / && rm -rf $DRIVER_TMP_DIR $DRIVER_ARCHIVE && \
    apt-get purge -y build-essential libcups2-dev libcupsimage2-dev && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Ensure permissions are correct
RUN chmod 644 $APP_DIR/cupsd.conf && chmod +x $APP_DIR/entrypoint.sh

# Copy the MQTT handler script
COPY app/printer_mqtt_handler.py $APP_DIR/printer_mqtt_handler.py
# Copy the web control panel script
COPY app/web_control_panel.py $APP_DIR/web_control_panel.py
WORKDIR $APP_DIR

# Use entrypoint script for runtime configuration and startup
ENTRYPOINT ["/app/entrypoint.sh"]
