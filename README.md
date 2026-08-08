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

---

## Deploy

You need Docker and a reachable MQTT broker (e.g. Mosquitto). Identify your printer's USB device path first with `lsusb` and `dmesg | grep usb` — usually something like `/dev/usb/lp0`.

**1. Clone this fork:**

```bash
git clone https://github.com/rainyvalley/PrintMQTTify.git
cd PrintMQTTify
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
      - MQTT_BROKER=192.0.2.10        # your broker IP
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
  -e MQTT_BROKER="192.0.2.10" \
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

## Credits & thanks

This project stands entirely on other people's work — huge thanks to both:

- **[Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify)** — the original MQTT-to-CUPS print client this is forked from. Released under Creative Commons Zero v1.0 (CC0-1.0). Thank you for building the thing this fork is a small tweak on.
- **[klirichek/zj-58](https://github.com/klirichek/zj-58)** — the CUPS filter that makes ZJ-58/ZJ-80 and other ESC/POS thermal printers work. Licensed BSD-2-Clause, © Aleksey N. Vinogradov (klirichek). Thank you for reverse-engineering and maintaining this driver. See [`LICENSE.zj-58`](./LICENSE.zj-58) for the full license text, which is retained here as that license requires.

If you find this useful, please go star both of the repositories above — the original authors did the hard parts.

---

## License

The PrintMQTTify portion follows the upstream project's Creative Commons Zero v1.0 (CC0-1.0) dedication. The bundled ZJ-58/ZJ-80 filter remains under its own BSD-2-Clause license (see [`LICENSE.zj-58`](./LICENSE.zj-58)).
