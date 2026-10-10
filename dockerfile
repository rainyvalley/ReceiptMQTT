# Base image
# trixie ships CUPS 2.4.10; the queue is raw and the ESC/POS emission lives
# in app/escpos.py, not in a CUPS filter, so the stack survives 2.5/3.x.
FROM debian:trixie-slim

ENV APP_DIR=/app

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

# CUPS is loopback-only (see configs/cupsd.conf) and is monitoring-only;
# 631 is deliberately NOT exposed, do not add it back.

# Copy the pre-configured CUPS configs, entrypoint and app code in one
# layer (BuildKit COPY --chmod: CI builds with setup-buildx-action)
COPY --chmod=644 configs/cupsd.conf configs/cups-files.conf $APP_DIR/
COPY --chmod=755 entrypoint.sh $APP_DIR/entrypoint.sh
COPY app/ $APP_DIR/
WORKDIR $APP_DIR

# Use entrypoint script for runtime configuration and startup
ENTRYPOINT ["/app/entrypoint.sh"]