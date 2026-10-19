# PrintMQTTify (ZJ-58 / ZJ-80 / POS58 edition)

A Docker-based bridge between MQTT and a USB thermal printer, aimed at cheap ESC/POS thermal receipt printers (Zijiang **ZJ-58** / **ZJ-80**, the widely rebadged **POS58**, and compatible clones). Publish a message to an MQTT topic — from Home Assistant, Node-RED, or anything else — and it prints.

This started as a fork of [Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify), and its printer support started from the **ZJ-58/ZJ-80** CUPS driver by [klirichek/zj-58](https://github.com/klirichek/zj-58). Neither driver is used any more: the ESC/POS encoding was rewritten in Python (`app/escpos.py`, ported from klirichek's filter) and printing goes straight to the device. Printed output is cleaned up too (branding removed, vertical separators, text wrapping).

---

## What it does

- Listens on an MQTT topic for print jobs.
- Formats incoming messages for narrow thermal roll paper (58 mm / 80 mm).
- Renders each receipt to a bitmap (reportlab -> ghostscript), encodes it as ESC/POS in-app (`app/escpos.py`), and writes the bytes straight to the printer device (`PRINTER_DEVICE`, e.g. `/dev/usb/lp1`). No printer driver, no PPDs, no CUPS filters, and CUPS is not in the print path, so it works identically on amd64 and arm64.
- Polls the printer's paper sensors over the same device and publishes paper, paper-low and availability state to MQTT.
- Keeps a small loopback-only CUPS instance purely for monitoring (`lpstat` feeds the availability topic); it never sees print data. CUPS 2.4.10+ silently drops jobs on raw `file:` queues, which is why printing bypasses it — see [`docs/hardware-notes.md`](./docs/hardware-notes.md).
- Works with USB ESC/POS printers: ZJ-58/ZJ-80 and POS58-class clones, which report varied USB vendor strings (e.g. `STMicroelectronics` / `POS58 Printer USB`) but take the same ESC/POS.
- Optional web control panel (Basic-auth protected) for status and test prints.

Typical use: printing Home Assistant shopping lists, reminders, or automation alerts to a receipt printer.

### Added in this fork

- rastertozj's ESC/POS emission ported to Python (`app/escpos.py`), so the deprecated PPD/filter model is gone entirely.
- Direct device writes: bytes either reach the printer or the log says why (busy devices are retried; no silent "completed" jobs).
- Shorter receipts: page height tracks the message length; tall receipts are capped at `RASTER_HEIGHT_PX` raster lines (default 480 = the old 60 mm page) and the handler logs a loud warning when the cap truncates, instead of cutting silently.
- Paper and paper-low state published to MQTT, alongside availability.
- Prebuilt multi-arch images published by CI; hardware-free pipeline smoke test on every push.

---

## Deploy

You need Docker and a reachable MQTT broker (e.g. Mosquitto). Identify your printer's USB device path first with `lsusb` and `dmesg | grep usb` — usually `/dev/usb/lp0` or `/dev/usb/lp1` (the default is `lp1`). If you have more than one USB printer the numbers can swap across reboots; see [`docs/hardware-notes.md`](./docs/hardware-notes.md).

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

Tags available: `latest` (tracks `main`), `beta` (tracks the `beta` branch - test builds before they land on `main`), `sha-<commit>` for every build, and semver tags (`1.2.3`, `1.2`) if you cut `v*` tags. CI also runs the pipeline smoke test on every push before publishing. There is no printer driver inside the image at all: printing is done in-app (reportlab -> ghostscript -> ESC/POS) and written straight to the printer device, so the image is identical on amd64 and arm64.

### Option B: build locally

```bash
docker compose build
# or
docker build -t ghcr.io/rainyvalley/receiptmqtt:local .
```

To use a locally built image with Compose, set `IMAGE_TAG=local` in your `.env` (see step 3).

**3. Configure your settings.**

The tracked `docker-compose.yml` ships with placeholder values on purpose — do **not** commit real credentials into it. Settings go in two local, git-ignored files that Compose picks up automatically.

`.env` (next to `docker-compose.yml`) holds the values. `MQTT_PASSWORD` and `ADMIN_PASS` are **required** — Compose refuses to start without them, and they must be in `.env` (or your shell environment), not in the override file:

```bash
MQTT_BROKER=192.0.2.10          # your broker IP
MQTT_USERNAME=your-username
MQTT_PASSWORD=your-password
ADMIN_PASS=your-panel-password    # Basic-auth password for the control panel
PRINTER_DEVICE=/dev/usb/lp0       # your printer's device path (default /dev/usb/lp1)
# MQTT_PORT=1883                  # optional: broker port (8883 is typical for TLS)
# MQTT_TLS=1                      # optional: TLS to the broker (default off)
# MQTT_TLS_CA=/certs/ca.pem       # optional: CA bundle inside the container (default: system CAs)
# MQTT_TLS_INSECURE=1             # optional: skip hostname check (lab only, default off)
# WEB_PUBLISH_BIND=127.0.0.1      # optional: publish the panel on localhost only (default 0.0.0.0)
# PRINT_QUEUE_SIZE=16             # optional: max jobs waiting behind the one printing (default 16)
# IMAGE_TAG=beta                  # or `local` for a locally built image
```

Map the printer into the container via `.env`/compose (the override file is local and git-ignored). The device path must match `PRINTER_DEVICE`:

```yaml
services:
  printmqttify:
    devices:
      - "/dev/usb/lp0:/dev/usb/lp0"
```

Map the `/dev/usb/lpN` node, never a `/dev/bus/usb/NNN/NNN` path — those renumber when the printer is replugged.

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
  --ulimit nofile=65536:65536 \
  -e PRINTER_DEVICE="/dev/usb/lp0" \
  -e MQTT_BROKER="192.0.2.10" \
  -e MQTT_USERNAME="your-username" \
  -e MQTT_PASSWORD="your-password" \
  -e MQTT_TOPIC="printer/commands" \
  -e ADMIN_USER="admin" \
  -e ADMIN_PASS="your-panel-password" \
  ghcr.io/rainyvalley/receiptmqtt:latest
```

Flags: `--device` gives the container access to the printer, `-p 8080:8080` publishes the (Basic-auth-protected) control panel. CUPS is loopback-only inside the container, monitoring-only, and needs no published port — the app writes ESC/POS bytes straight to `PRINTER_DEVICE`, which must match the `--device` path. Replace the placeholder values with your own — and note these are visible in your shell history, so the `.env` method above is preferable for anything sensitive.

**5. Send a test message.** See [Home Assistant](#home-assistant) below, or from a shell:

```bash
mosquitto_pub -h <broker> -u <user> -P <pass> -t printer/commands \
  -m '{"printer_name": "ReceiptPrinter", "title": "Test", "message": "Hello, World!"}'
```

Check logs with `docker logs printmqttify_container` if nothing prints.

**Job queue.** Messages are printed one at a time, in arrival order, by a single background worker, so a slow render or print never stalls the MQTT connection. Up to `PRINT_QUEUE_SIZE` jobs (default `16`, integer >= 1; invalid values fall back to the default) can wait behind the one printing. If the queue is full, the *newest* message is dropped and a `WARNING: print queue full ... dropped newest message` line with a running drop count is logged and the drop is published to MQTT (see [Dropped and failed jobs](#dropped-and-failed-jobs)). Jobs that raise while printing are likewise logged and published. On `docker stop` (SIGTERM) the job in flight gets up to 10 s to finish; jobs still waiting are discarded and logged. With `docker run`, pass `-e PRINT_QUEUE_SIZE=32` to change it.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_PORT` | `1883` | Broker port (invalid or out-of-range values fall back to 1883). |
| `MQTT_TLS` | off | `1`/`true`/`yes`/`on` enables TLS to the broker using the system CA store. |
| `MQTT_TLS_CA` | unset | Path (inside the container) to a CA bundle to trust instead of the system CAs. Mount it read-only. |
| `MQTT_TLS_INSECURE` | off | Skip broker hostname verification. Lab use only; logs a warning. |
| `WEB_BIND` | `0.0.0.0` | Address the control panel binds to inside the container. Leave it alone under Docker bridge networking; see below. |
| `WEB_PUBLISH_BIND` | `0.0.0.0` | Compose only: host address the panel port is published on (`127.0.0.1` = this machine only). |

### Securing the deployment

All of the above are opt-in; with nothing set, behaviour is unchanged (plain MQTT on 1883, panel on every interface). To harden it: point the app at a TLS listener (`MQTT_PORT=8883`, `MQTT_TLS=1`, plus `MQTT_TLS_CA` for a private CA), keep the panel local-only by setting `WEB_PUBLISH_BIND=127.0.0.1` in `.env` (the same as editing the mapping to `127.0.0.1:8080:8080`, or `-p 127.0.0.1:8080:8080` with `docker run`; reach it through an SSH tunnel or reverse proxy), and use strong, unique values for `MQTT_PASSWORD` and `ADMIN_PASS`. `WEB_BIND` only matters when running the app outside Docker's bridge networking (host networking or bare metal); inside a bridge-networked container it must stay `0.0.0.0` or the published port cannot reach it.

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

### Dropped and failed jobs

Jobs the printer never printed are published, not just logged. Topics (all QoS 1, retained):

| Topic | Payload |
|---|---|
| `printer/last_error` | JSON: `timestamp` (UTC ISO 8601), `kind` (`dropped` = queue full, `failed` = the job raised), `message`, `dropped_total`, `failed_total`. |
| `printer/dropped_total` | Integer, jobs dropped since the container started. |
| `printer/failed_total` | Integer, jobs that failed since the container started. |

`last_error` is retained so Home Assistant still shows the most recent problem after a restart; it is only replaced by the next error. The counters are published as `0` on every connect, so they reset when the container restarts. `message` contains at most the first 40 characters of the job title and, for failures, a short error string; the message body is never published. Topic names can be changed with `MQTT_LAST_ERROR_TOPIC`, `MQTT_DROPPED_TOTAL_TOPIC` and `MQTT_FAILED_TOTAL_TOPIC`. Publishing is fire-and-forget: a broker problem is logged and never blocks printing.

```yaml
mqtt:
  sensor:
    - name: "Receipt Printer Last Error"
      unique_id: receipt_printer_last_error
      state_topic: "printer/last_error"
      value_template: "{{ value_json.kind }}"
      json_attributes_topic: "printer/last_error"
    - name: "Receipt Printer Dropped Jobs"
      unique_id: receipt_printer_dropped_total
      state_topic: "printer/dropped_total"
      state_class: total_increasing
    - name: "Receipt Printer Failed Jobs"
      unique_id: receipt_printer_failed_total
      state_topic: "printer/failed_total"
      state_class: total_increasing
```

Merge these under your existing `mqtt:` key (do not repeat it); the project publishes no MQTT discovery, so sensors are defined by hand like the paper ones. An automation triggering on a rise in `sensor.receipt_printer_failed_jobs` or `..._dropped_jobs` can notify you.

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

`title` prints bold above a divider, `message` below it, wrapped to the roll width. `printer_name` is required in the payload but doesn't choose a printer: every job goes to `PRINTER_DEVICE`. Keep it as `ReceiptPrinter`.

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

This project wouldn't exist without the starting points these two gave it — huge thanks to both:

- **[Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify)** — the original MQTT-to-CUPS print client this project grew out of. Released under Creative Commons Zero v1.0 (CC0-1.0). Thank you for the foundation.
- **[klirichek/zj-58](https://github.com/klirichek/zj-58)** — the ZJ-58/ZJ-80 CUPS driver that got these printers working in the first place. The driver itself is no longer used here, but [`app/escpos.py`](./app/escpos.py) is a Python port of its ESC/POS output logic. Licensed BSD-2-Clause, © Aleksey N. Vinogradov (klirichek); the notice is kept in [`LICENSE.zj-58`](./LICENSE.zj-58). Thank you for reverse-engineering these printers.

If you find this useful, please go star both repositories above.

---

## A note on AI assistance

Parts of this fork — code, configuration, and documentation — were written with the help of generative AI. Everything in here was reviewed, tested against real hardware, and corrected by a human before being committed; nothing was accepted on the model's say-so. The debugging that produced most of these changes is recorded in [`docs/hardware-notes.md`](./docs/hardware-notes.md).

---

## License

The PrintMQTTify portion follows the upstream project's Creative Commons Zero v1.0 (CC0-1.0) dedication. The Python port of the ZJ-58/ZJ-80 ESC/POS output logic ([`app/escpos.py`](./app/escpos.py)) remains BSD-2-Clause, © Aleksey N. Vinogradov (klirichek), under the terms in [`LICENSE.zj-58`](./LICENSE.zj-58).
