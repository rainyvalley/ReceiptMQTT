# PrintMQTTify (ZJ-58 / ZJ-80 / POS58 edition)

A Docker-based bridge between MQTT and a CUPS printer, aimed at cheap ESC/POS thermal receipt printers (Zijiang **ZJ-58** / **ZJ-80**, the widely rebadged **POS58**, and compatible clones). Publish a message to an MQTT topic — from Home Assistant, Node-RED, or anything else — and it prints.

This is a fork of [Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify) with two main changes: the bundled SEWOO driver is replaced with the **ZJ-58/ZJ-80** CUPS filter from [klirichek/zj-58](https://github.com/klirichek/zj-58), and the printed output is cleaned up (branding removed from every message, vertical separators and text wrapping added).

---

## What it does

- Runs a CUPS server in a container and listens on an MQTT topic for print jobs.
- Formats incoming messages for narrow thermal roll paper (58 mm / 80 mm).
- Works with USB ESC/POS printers via the ZJ-58/ZJ-80 filter. The 58 mm PPD also drives POS58-class clones, which report varied USB vendor strings (e.g. `STMicroelectronics` / `POS58 Printer USB`) but take the same ESC/POS.
- Optional web control panel for basic settings.

Typical use: printing Home Assistant shopping lists, reminders, or automation alerts to a receipt printer.

### Added in this fork

- ZJ-58/ZJ-80 ESC/POS filter in place of the bundled SEWOO driver, on a Debian bookworm base.
- The printer queue is created on container start, so it survives rebuilds.
- Shorter receipts: a 60 mm page size, against the stock PPD's 210 mm minimum.
- Paper and paper-low state published to MQTT, alongside availability.
- A stalled job no longer disables the queue.

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

**5. Add the printer in CUPS.** Open `https://<host-ip>:631`, log in with your `ADMIN_USER` / `ADMIN_PASS`, go to **Administration → Add Printer**, select the USB printer, and choose the **ZJ-58** driver for 58 mm rolls (including POS58 clones) or **ZJ-80** for 80 mm. Print a test page to confirm.

**6. Send a test message.** See [Home Assistant](#home-assistant) below, or from a shell:

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
- **[klirichek/zj-58](https://github.com/klirichek/zj-58)** — the CUPS filter that makes ZJ-58/ZJ-80 and other ESC/POS thermal printers work. Licensed BSD-2-Clause, © Aleksey N. Vinogradov (klirichek). Thank you for reverse-engineering and maintaining this driver. See [`LICENSE.zj-58`](./LICENSE.zj-58) for the full license text, which is retained here as that license requires.

If you find this useful, please go star both of the repositories above — the original authors did the hard parts.

---

## A note on AI assistance

Parts of this fork — code, configuration, and documentation — were written with the help of generative AI. Everything in here was reviewed, tested against real hardware, and corrected by a human before being committed; nothing was accepted on the model's say-so. The debugging that produced most of these changes is recorded in [`docs/hardware-notes.md`](./docs/hardware-notes.md).

---

## License

The PrintMQTTify portion follows the upstream project's Creative Commons Zero v1.0 (CC0-1.0) dedication. The bundled ZJ-58/ZJ-80 filter remains under its own BSD-2-Clause license (see [`LICENSE.zj-58`](./LICENSE.zj-58)).
