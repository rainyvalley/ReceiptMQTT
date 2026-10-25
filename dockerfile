# Base image
# Alpine, not Debian. Debian's armhf port is compiled for ARMv7 (Thumb-2,
# VFPv3-d16), so a trixie-slim image dies with SIGILL on the ARMv6 CPU in a
# Pi Zero W. Alpine's armhf port targets ARMv6 (hard-float), so the same
# Dockerfile serves amd64, arm64 and arm/v6 from one distro and one package
# set. CUPS 2.4.x is in main; the queue is raw and the ESC/POS emission lives
# in app/escpos.py, not in a CUPS filter, so the stack survives 2.5/3.x.
FROM alpine:3.22

ENV APP_DIR=/app

# Runtime packages.
# Python deps come from apk, not pip: they are prebuilt for the image's
# Python, so no compiler or build headers end up in the image.
#
# No CUPS printer driver is installed: printing is PDF -> ghostscript
# pbmraw -> app/escpos.py -> direct write to PRINTER_DEVICE (CUPS is kept
# for lpstat monitoring only), so ghostscript replaces the compiled filter.
# bash runs entrypoint.sh and python3 -m gunicorn runs the panel; setpriv
# drops root for the app. User and group management is busybox adduser,
# so shadow is not needed.
# hadolint ignore=DL3018
RUN apk add --no-cache \
    bash \
    cups \
    cups-client \
    dbus \
    ghostscript \
    python3 \
    py3-flask \
    py3-gunicorn \
    py3-paho-mqtt \
    py3-reportlab \
    setpriv \
    && rm -rf /var/cache/apk/*

# Unprivileged account the app (MQTT handler and web panel) runs as. cupsd
# still starts as root inside the entrypoint; it then drops to this user.
# The "lp" group lets it write /dev/usb/lpN; the entrypoint adds the device's
# own group id at start, since that number comes from the host. The "receipt"
# group is primary, matching the setpriv --regid call in the entrypoint.
RUN addgroup -S receipt \
    && adduser -S -D -H -s /sbin/nologin -u 10001 -G receipt receipt \
    && addgroup receipt lp

# CUPS is loopback-only (see configs/cupsd.conf) and is monitoring-only;
# 631 is deliberately NOT exposed, do not add it back.

# Copy the pre-configured CUPS configs, entrypoint and app code in one
# layer (BuildKit COPY --chmod: CI builds with setup-buildx-action)
COPY --chmod=644 configs/cupsd.conf configs/cups-files.conf $APP_DIR/
COPY --chmod=755 entrypoint.sh $APP_DIR/entrypoint.sh
COPY app/ $APP_DIR/
# The chmod on the config COPY above leaves the directory itself 644; the
# unprivileged app user must be able to enter and read it.
RUN chmod 755 $APP_DIR
WORKDIR $APP_DIR

# Use entrypoint script for runtime configuration and startup
ENTRYPOINT ["/app/entrypoint.sh"]
