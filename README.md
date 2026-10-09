# PrintMQTTify (ZJ-58 / ZJ-80 / POS58 edition)

A Docker-based bridge between MQTT and a CUPS printer, aimed at cheap ESC/POS thermal receipt printers (Zijiang **ZJ-58** / **ZJ-80**, the widely rebadged **POS58**, and compatible clones). Publish a message to an MQTT topic — from Home Assistant, Node-RED, or anything else — and it prints.

This is a fork of [Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify). The bundled SEWOO driver was first replaced with the **ZJ-58/ZJ-80** CUPS filter from [klirichek/zj-58](https://github.com/klirichek/zj-58), and that filter's ESC/POS output logic is now ported into the app itself (`app/escpos.py`), because CUPS deprecated PPD-based drivers. Printed output is cleaned up too (branding removed, vertical separators, text wrapping).

---

## What it does

- Runs a CUPS server in a container and listens on an MQTT topic for print jobs.
- Formats incoming messages for narrow thermal roll paper (58 mm / 80 mm).
- Renders each receipt to a bitmap (ghostscript) and emits ESC/POS directly, then queues the raw bytes via CUPS — no PPDs, no CUPS filters, works identically on amd64 and arm64, and on CUPS 2.4, 2.5 or 3.x (the image builds on Debian trixie with CUPS 2.5).
- Works with USB ESC/POS printers: ZJ-58/ZJ-80 and POS58-class clones, which report varied USB vendor strings (e.g. `STMicroelectronics` / `POS58 Printer USB`) but take the same ESC/POS.
- Optional web control panel (Basic-auth protected) for status and test prints.

Typical use: printing Home Assistant shopping lists, reminders, or automation alerts to a receipt printer.

### Added in this fork

- rastertozj's ESC/POS emission ported to Python (`app/escpos.py`); CUPS queue is raw, so the deprecated PPD/filter model is gone entirely.
- The printer queue is created on container start, so it survives rebuilds.
- Shorter receipts: page height tracks the message length, capped in raster lines (`RASTER_HEIGHT_PX`, default 480 = the old 60 mm page).
- Paper and paper-low state published to MQTT, alongside availability.
- A stalled job no longer disables the queue.
- Prebuilt multi-arch images published by CI; hardware-free pipeline smoke test on every push.

---

## Deploy

You need Docker and a reachable MQTT broker (e.g. Mosquitto). Identify your printer's USB device path first with `lsusb` and `dmesg | grep usb` — usually something like `/dev/usb/lp0`.

**1. Clone this fork:**

```bash
git clone https://github.com/rainyvalley/ReceiptMQTT.git
cd ReceiptMQTT
```

**2. Choose your install method.**

### Option A: pull the prebuilt image (recommended)

CI publishes a multi-arch image (`linux/amd64` and `linux/arm64`) to GHCR on every push to `main` and on version tags. Pull it:

```bash
docker pull ghcr.io/rainyvalley/receiptmqtt:latest
```

The tracked `docker-compose.yml` already points at that image, so Compose pulls it for you on first `up`.

Tags available: `latest` (tracks `main`), `beta` (tracks the `beta` branch - test builds before they land on `main`), `sha-<commit>` for every build, and semver tags (`1.2.3`, `1.2`) if you cut `v*` tags. CI also runs the pipeline smoke test on every push before publishing. There is no CUPS printer driver inside the image at all: printing is done in-app (reportlab -> ghostscript -> ESC/POS) straight to a raw CUPS queue, so the image is identical on amd64 and arm64.

### Option B: build locally

```bash
docker compose build
# or
docker build -t ghcr.io/rainyvalley/receiptmqtt:local .
```

To use a locally built image with Compose, override `image:` in your local `docker-compose.override.yml` (see step 3).

**3. Configure your broker details.**

The tracked `docker-compose.yml` ships with placeholder values on purpose — do **not** commit real credentials into it. Put your real broker IP, username, password, USB device path, and (for local builds) the `image:` override in a local `docker-compose.override.yml` (git-ignored), which Compose merges automatically:

```yaml
services:
  printmqttify:
    # only needed for a locally built image, not for the GHCR pull:
    # image: ghcr.io/rainyvalley/receiptmqtt:local
    environment:
      - MQTT_BROKER=192.168.0.71        # your broker IP
      - MQTT_USERNAME=your-username
      - MQTT_PASSWORD=your-password
      - ADMIN_PASS=your-cups-admin-pass
    devices:
      - "/dev/usb/lp0:/dev/usb/lp0"     # your printer's USB path
```

**4. Start it:**

```bash
docker compose up -d
```

### Alternative: run with `docker run`

If you'd rather skip Compose, you can start the container directly. Pass your broker details as environment variables and map your printer's USB device:

```bash
docker run --name printmqttify_container \
  -d \
  -p 8080:8080 \
  --device=/dev/usb/lp0:/dev/usb/lp0 \
  -v /dev/bus/usb:/dev/bus/usb \
  --ulimit nofile=65536:65536 \
  -e PRINTER_URI="file:/dev/usb/lp0" \
  -e MQTT_BROKER="192.168.0.71" \
  -e MQTT_USERNAME="your-username" \
  -e MQTT_PASSWORD="your-password" \
  -e MQTT_TOPIC="printer/commands" \
  -e ADMIN_USER="admin" \
  -e ADMIN_PASS="your-cups-admin-pass" \
  ghcr.io/rainyvalley/receiptmqtt:latest
```

Flags: `--device` plus the `/dev/bus/usb` bind give the container USB access to the printer, `-p 8080:8080` publishes the (Basic-auth-protected) control panel. CUPS itself is loopback-only inside the container and needs no published port. `PRINTER_URI` must match the `--device` path. Replace the placeholder values with your own — and note these are visible in your shell history, so the Compose override method above is preferable for anything sensitive.

**5. Send a test message.** See [Home Assistant](#home-assistant) below, or from a shell:

```bash
mosquitto_pub -h <broker> -u <user> -P <pass> -t printer/commands \
  -m '{"printer_name": "ReceiptPrinter", "title": "Test", "message": "Hello, World!"}'
```

Check logs with `docker logs printmqttify_container` if nothing prints.

---

## Home Assistant

### Sensors

Add to `configuration.yaml` and restart. These are not editable from the UI.

```yaml
mqtt:
  binary_sensor:
    - name: "Receipt Printer Paper"
      unique_id: receipt_printer_paper
      state_topic: "printer/paper"
      payload_on: "OFF"          # ON = a problem = out of paper
      payload_off: "ON"
      device_class: problem
      availability_topic: "printer/availability"
      payload_available: "online"
      payload_not_available: "offline"

    - name: "Receipt Printer Paper Low"
      unique_id: receipt_printer_paper_low
      state_topic: "printer/paper_low"
      payload_on: "ON"           # ON = near-end sensor tripped
      payload_off: "OFF"
      device_class: problem
      availability_topic: "printer/availability"
      payload_available: "online"
      payload_not_available: "offline"
```

Both carry `device_class: problem`, so "on" means something needs attention. The `availability_topic` means they show as unavailable rather than stale when the container is down.

### Printing a message

```yaml
  - action: mqtt.publish
    metadata: {}
    data:
      evaluate_payload: false
      qos: "2"
      retain: false
      topic: printer/commands
      payload: >-
        {"printer_name": "ReceiptPrinter", "title": "{{
        now().strftime('%Y-%m-%d %H:%M:%S') }}", "message": "Time for your
        E-Shot!"}
```

`title` prints bold above a divider, `message` below it, wrapped to the roll width. `printer_name` must match the CUPS queue name (`PRINTER_NAME`, default `ReceiptPrinter`).

Stick to plain ASCII in `message`. Em dashes and smart quotes are fine through the PDF path but not worth relying on.

### Notify when the paper runs out

Paste into a new automation via **⋮ → Edit in YAML**.

```yaml
alias: Receipt printer out of paper
description: ""
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.receipt_printer_paper
    to: "on"
    for:
      hours: 0
      minutes: 1
      seconds: 0
conditions:
  - condition: template
    value_template: >-
      {{ (as_timestamp(now()) -
      as_timestamp(state_attr('automation.receipt_printer_out_of_paper','last_triggered'),
      0)) > 3600 }}
actions:
  - action: notify.mobile_app_your_phone
    metadata: {}
    data:
      message: Receipt printer is out of paper.
```

### Notify when it is running low, and print a reminder

```yaml
alias: Receipt printer paper low
description: ""
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.receipt_printer_paper_low
    to: "on"
    for:
      hours: 0
      minutes: 1
      seconds: 0
conditions:
  - condition: state
    entity_id: binary_sensor.receipt_printer_paper
    state: "off"
  - condition: template
    value_template: >-
      {{ (as_timestamp(now()) -
      as_timestamp(state_attr('automation.receipt_printer_paper_low','last_triggered'),
      0)) > 21600 }}
actions:
  - action: notify.mobile_app_your_phone
    metadata: {}
    data:
      message: Receipt printer is low on paper.
  - action: mqtt.publish
    metadata: {}
    data:
      evaluate_payload: false
      qos: "2"
      retain: false
      topic: printer/commands
      payload: >-
        {"printer_name": "ReceiptPrinter", "title": "{{
        now().strftime('%Y-%m-%d %H:%M:%S') }}", "message": "Paper is low.
        Time to replace the roll."}
```

Three things worth knowing:

- The `last_triggered` templates reference the automation's own entity ID, which Home Assistant derives from the alias. If you rename the automation, update the template — otherwise it evaluates to `0`, the condition always passes, and you lose the cooldown.
- Sensors are polled every 60 seconds, so `for: 1 minute` means up to two minutes before a notification fires.
- The low-paper automation checks the printer is not *already* out, so you do not get both notifications at once.

### Offline alert

```yaml
alias: Receipt printer offline
description: ""
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.receipt_printer_paper
    to: unavailable
    for:
      hours: 0
      minutes: 30
      seconds: 0
actions:
  - action: notify.mobile_app_your_phone
    metadata: {}
    data:
      message: Receipt printer has been offline for 30 minutes.
```

Worth having. A silently dead printer is easy to not notice for a very long time.

---

## Credits & thanks

This project stands entirely on other people's work — huge thanks to both:

- **[Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify)** — the original MQTT-to-CUPS print client this is forked from. Released under Creative Commons Zero v1.0 (CC0-1.0). Thank you for building the thing this fork is a small tweak on.
- **[klirichek/zj-58](https://github.com/klirichek/zj-58)** — the CUPS filter whose ESC/POS output logic this project now ports in Python ([`app/escpos.py`](./app/escpos.py)); it made ZJ-58/ZJ-80 and other ESC/POS thermal printers work before CUPS deprecated PPD drivers. Licensed BSD-2-Clause, © Aleksey N. Vinogradov (klirichek). Thank you for reverse-engineering and maintaining this driver; the license notice is preserved in that repo and applies to the ported logic.

If you find this useful, please go star both of the repositories above — the original authors did the hard parts.

---

## A note on AI assistance

Parts of this fork — code, configuration, and documentation — were written with the help of generative AI. Everything in here was reviewed, tested against real hardware, and corrected by a human before being committed; nothing was accepted on the model's say-so. The debugging that produced most of these changes is recorded in [`docs/hardware-notes.md`](./docs/hardware-notes.md).

---

## License

The PrintMQTTify portion follows the upstream project's Creative Commons Zero v1.0 (CC0-1.0) dedication. The Python port of the ZJ-58/ZJ-80 ESC/POS output logic ([`app/escpos.py`](./app/escpos.py)) remains BSD-2-Clause, © Aleksey N. Vinogradov (klirichek), under the terms of the upstream [`LICENSE.zj-58`](https://github.com/klirichek/zj-58).
