# PrintMQTTify (ZJ-58 / ZJ-80 edition)

A Docker-based bridge between MQTT and a CUPS printer, aimed at cheap ESC/POS thermal receipt printers (Zijiang **ZJ-58** / **ZJ-80** and compatible clones). Publish a message to an MQTT topic — from Home Assistant, Node-RED, or anything else — and it prints.

This is a fork of [Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify) with two main changes: the bundled SEWOO driver is replaced with the **ZJ-58/ZJ-80** CUPS filter from [klirichek/zj-58](https://github.com/klirichek/zj-58), and the printed output is cleaned up (branding removed from every message, vertical separators and text wrapping added).

---

## What it does

- Runs a CUPS server in a container and listens on an MQTT topic for print jobs.
- Formats incoming messages for narrow thermal roll paper (58 mm / 80 mm).
- Works with USB ESC/POS printers via the ZJ-58/ZJ-80 filter.
- Optional web control panel for basic settings.

Typical use: printing Home Assistant shopping lists, reminders, or automation alerts to a receipt printer.

### Added in this fork

- **The printer is created on container start.** The queue used to exist only inside the container's `printers.conf` and was lost on every rebuild. `entrypoint.sh` now runs `lpadmin` itself, so a fresh container comes up ready to print. Overridable with `PRINTER_NAME`, `PRINTER_URI`, `PRINTER_PPD`, `PRINTER_PAGESIZE`.
- **Short receipts.** The stock PPD's shortest page is 210 mm, and `rastertozj` pads every job out to the full declared page — so a two-line reminder fed roughly eight inches of paper. `configs/ReceiptPrinter.ppd` adds 60 mm and 105 mm page sizes.
- **The queue can no longer disable itself silently.** `MaxJobTime 300` and `ErrorPolicy abort-job` mean a stalled job costs you one receipt instead of taking the printer offline until someone notices.
- **Honest availability reporting.** The old check looked for the literal string `idle` in unqualified `lpstat` output, which also missed while a job was printing. It now names the printer, surfaces `lpstat` failures instead of swallowing them, and treats only `disabled` as down. Published retained to `printer/availability`, with an MQTT last-will so a dead container shows offline.
- **Paper sensors.** The printer is polled with `DLE EOT 4` and the result published to `printer/paper` and `printer/paper_low` (both retained). Needs a bidirectional printer; see [`docs/hardware-notes.md`](./docs/hardware-notes.md).
- **File backend support.** On some hosts CUPS' libusb backend enumerates the printer but cannot claim it — jobs complete with bytes "sent" and nothing reaches the paper. Setting `PRINTER_URI=file:/dev/usb/lp0` writes to the kernel character device instead. `configs/cups-files.conf` carries the `FileDevice Yes` this requires.
- **Debian bookworm base**, with `rastertozj` genuinely compiled at build time rather than falling back to the prebuilt binary in the driver tarball.

---

## Deploy

You need Docker and a reachable MQTT broker (e.g. Mosquitto). Identify your printer's USB device path first with `lsusb` and `dmesg | grep usb` — usually something like `/dev/usb/lp0`.

**1. Clone this fork:**

```bash
git clone https://github.com/rainyvalley/ReceiptMQTT.git
cd ReceiptMQTT
```

**2. Build the image:**

```bash
docker build -t printmqttify .
```

**3. Configure your broker details.**

The tracked `docker-compose.yml` ships with placeholder values on purpose — do **not** commit real credentials into it. Put your real broker IP, username, and password in a local `docker-compose.override.yml` (git-ignored), which Compose merges automatically:

```yaml
services:
  printmqttify:
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
  --privileged \
  -p 631:631 \
  -p 8080:8080 \
  --device=/dev/usb/lp0:/dev/usb/lp0 \
  --ulimit nofile=65536:65536 \
  -e MQTT_BROKER="192.168.0.71" \
  -e MQTT_USERNAME="your-username" \
  -e MQTT_PASSWORD="your-password" \
  -e MQTT_TOPIC="printer/commands" \
  -e ADMIN_USER="admin" \
  -e ADMIN_PASS="your-cups-admin-pass" \
  printmqttify
```

Flags: `--privileged` and `--device` give the container USB access to the printer, `-p 631:631` exposes the CUPS web interface, `-p 8080:8080` the control panel, and `--ulimit nofile=65536:65536` avoids file-descriptor issues on newer Docker. Replace the placeholder values with your own — and note these are visible in your shell history, so the Compose override method above is preferable for anything sensitive.

**5. Add the printer in CUPS.** Open `https://<host-ip>:631`, log in with your `ADMIN_USER` / `ADMIN_PASS`, go to **Administration → Add Printer**, select the USB printer, and choose the **ZJ-58** (or **ZJ-80**) driver. Print a test page to confirm.

**6. Send a test message** (Home Assistant example):

```yaml
service: mqtt.publish
data:
  topic: "printer/commands"
  payload: '{"printer_name": "ZJ-58", "message": "Hello, World!"}'
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
- **[klirichek/zj-58](https://github.com/klirichek/zj-58)** — the CUPS filter that makes ZJ-58/ZJ-80 and other ESC/POS thermal printers work. Licensed BSD-2-Clause, © Aleksey N. Vinogradov (klirichek). Thank you for reverse-engineering and maintaining this driver. See [`LICENSE.zj-58`](./LICENSE.zj-58) for the full license text, which is retained here as that license requires.

If you find this useful, please go star both of the repositories above — the original authors did the hard parts.

---

## A note on AI assistance

Parts of this fork — code, configuration, and documentation — were written with the help of generative AI. Everything in here was reviewed, tested against real hardware, and corrected by a human before being committed; nothing was accepted on the model's say-so. The debugging that produced most of these changes is recorded in [`docs/hardware-notes.md`](./docs/hardware-notes.md).

---

## License

The PrintMQTTify portion follows the upstream project's Creative Commons Zero v1.0 (CC0-1.0) dedication. The bundled ZJ-58/ZJ-80 filter remains under its own BSD-2-Clause license (see [`LICENSE.zj-58`](./LICENSE.zj-58)).
