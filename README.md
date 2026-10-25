# PrintMQTTify (ZJ-58 / ZJ-80 / POS58 edition)

PrintMQTTify prints MQTT messages on a USB thermal receipt printer. It runs in Docker. Send it a message from Home Assistant, Node-RED or any MQTT client, and a receipt comes out.

It works with Zijiang **ZJ-58**, the widely sold **POS58**, and clones that speak ESC/POS. It prints on **48 mm** wide paper by default. For 80 mm paper, see [Paper width](#paper-width). A typical use is a Home Assistant shopping list, reminder or alert.

## What you get

- A listener on one MQTT topic (default `printer/commands`). Each JSON message prints one receipt.
- Paper and paper-low state, and printer availability, published to MQTT for Home Assistant.
- A list of dropped and failed jobs, published to MQTT.
- A small web control panel on port 8080 (always on) with a status check and a **Send Test Print** button.

What it does not do yet:

- **Proof that 80 mm works.** 80 mm paper is supported by one setting, but it has not been tested on a real printer.
- **Long receipts.** About **10 lines** of message text fit on one receipt. Text below that is cut off and a warning is logged. See [Long receipts](#long-receipts).
- **Printers that only receive.** The paper sensors need a printer that can answer back. If yours cannot, printing still works and the sensors stay empty.

---

## Install

You need Docker and an MQTT broker (for example Mosquitto). The image is published for 64-bit x86 (`linux/amd64`), 64-bit ARM (`linux/arm64`) and 32-bit ARMv6 (`linux/arm/v6`). ARMv6 is what a Pi Zero W and Pi 1 have; newer Pis running a 32-bit Raspberry Pi OS use the same ARMv6 image, and 64-bit Raspberry Pi OS uses the arm64 one. All of them come from one Alpine base, so there is no separate ARM image to look after. Find your printer's device with `lsusb` and `dmesg | grep usb`. It is usually `/dev/usb/lp0` or `/dev/usb/lp1`. If you have two USB printers, the numbers can swap after a reboot. See [`docs/hardware-notes.md`](./docs/hardware-notes.md).

**1. Get the files.**

```bash
git clone https://github.com/rainyvalley/ReceiptMQTT.git
cd ReceiptMQTT
```

**2. Pick an image.**

- **Prebuilt (recommended).** `docker-compose.yml` already points at `ghcr.io/rainyvalley/receiptmqtt:latest`. Compose pulls the right architecture for your host on the first start. Other tags: `beta` (test builds) and `sha-<commit>`.
- **Build it yourself.** Run `docker build -t ghcr.io/rainyvalley/receiptmqtt:local .` in the repo, then put `IMAGE_TAG=local` in your `.env` so Compose uses that local image. The build works on every supported architecture, including a Pi Zero W, where it takes a while on one 1 GHz core.

**3. Create `.env`** next to `docker-compose.yml`. **`MQTT_PASSWORD` and `ADMIN_PASS` are required.** Compose will not start without them.

```bash
MQTT_BROKER=192.0.2.10          # your broker's address
MQTT_USERNAME=your-username
MQTT_PASSWORD=your-password
ADMIN_PASS=your-panel-password  # password for the web panel
PRINTER_DEVICE=/dev/usb/lp0     # your printer (default /dev/usb/lp1)
# WEB_PUBLISH_BIND=127.0.0.1    # recommended: panel on this machine only
```

Every other setting is in [Settings](#settings).

On a Pi Zero W (426 MB RAM) give the container a lower ceiling than the default 512 MB, so the host's OOM killer does not have to choose between the container and the rest of the system:

```bash
MEM_LIMIT=384m                   # in .env
```

**4. Give the container the printer.** Create `docker-compose.override.yml` next to `docker-compose.yml`. The path must be the same as `PRINTER_DEVICE`.

```yaml
services:
  printmqttify:
    devices:
      - "/dev/usb/lp0:/dev/usb/lp0"
```

Map the `/dev/usb/lpN` file, never a `/dev/bus/usb/...` path. Those numbers change when you replug the printer.

**5. Start it.**

```bash
docker compose up -d
```

**6. Print something.**

```bash
mosquitto_pub -h <broker> -u <user> -P <pass> -t printer/commands \
  -m '{"printer_name": "ReceiptPrinter", "title": "Test", "message": "Hello, World!"}'
```

Nothing came out? Run `docker logs printmqttify_container` and read [If something looks wrong](#if-something-looks-wrong).

You can also open `http://<host>:8080`, log in with `ADMIN_USER` (default `admin`) and `ADMIN_PASS`, and press **Send Test Print**. That prints a test pattern without MQTT.

### Without Compose

```bash
docker run --name printmqttify_container -d \
  -p 127.0.0.1:8080:8080 \
  --device=/dev/usb/lp0:/dev/usb/lp0 \
  --cap-drop ALL \
  --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --cap-add SETUID --cap-add SETGID --cap-add KILL --cap-add NET_BIND_SERVICE \
  --security-opt no-new-privileges:true \
  --env-file .env \
  ghcr.io/rainyvalley/receiptmqtt:latest
```

Put the settings in `.env` as above, not on the command line, so they stay out of your shell history. Plain `docker run` uses the program's own defaults for feeding and cutting (see `FEED_DIST` and `CUTTING` below), which differ from Compose.

---

## Settings

Set these in `.env`. Compose passes them on. A setting that is missing or empty uses its default.

**Broker**

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_BROKER` | `your-broker-ip` | Broker address. |
| `MQTT_PORT` | `1883` | Broker port. 8883 is usual for TLS. A bad value falls back to 1883. |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | placeholder, required | Broker login. With no login the container still starts but logs a warning. |
| `MQTT_TLS` | off | `1` turns on TLS to the broker, using the system CA list. |
| `MQTT_TLS_CA` | system CAs | Path inside the container to a CA file, for a private CA. Mount it read-only. |
| `MQTT_TLS_INSECURE` | off | Skip the broker name check. Lab use only. A warning is logged. |
| `MQTT_TOPIC` | `printer/commands` | Topic that print jobs arrive on. |
| `MQTT_PAPER_TOPIC`, `MQTT_PAPER_LOW_TOPIC` | `printer/paper`, `printer/paper_low` | Paper sensor topics. |
| `MQTT_LAST_ERROR_TOPIC`, `MQTT_DROPPED_TOTAL_TOPIC`, `MQTT_FAILED_TOTAL_TOPIC` | `printer/last_error`, `printer/dropped_total`, `printer/failed_total` | Topics for [dropped and failed jobs](#dropped-and-failed-jobs). |

The availability topic is always `printer/availability`.

**Printer and paper**

| Variable | Default | Meaning |
|---|---|---|
| `PRINTER_DEVICE` | `/dev/usb/lp1` | The printer's device file. The older name `PAPER_DEVICE` is used if `PRINTER_DEVICE` is not set. |
| `PRINTER_NAME` | `ReceiptPrinter` | Name of the small monitoring queue the availability check looks at. It is not used for printing. |
| `FEED_DIST` | `0` with Compose, `2` with plain `docker run` | How many extra paper feeds after each receipt. Each is about 3 mm. |
| `CUTTING` | `0` with Compose, `1` with plain `docker run` | `0` no cut. `1` cut after each page. `2` cut at the end of the job. |
| `BLANK_SPACE` | `1` | `1` does not feed blank space at the end of a receipt. `0` feeds it. |
| `CASH_DRAWER1`, `CASH_DRAWER2` | `0` | `0` off. `1` open the drawer before printing. `2` open it after. |
| `PAPER_WIDTH_MM` | `48` | Paper width: `48` or `80`. See [Paper width](#paper-width). |
| `RASTER_WIDTH_PX` | empty | Page width in dots. Leave it empty: `PAPER_WIDTH_MM` sets it (384 for 48 mm, 576 for 80 mm). |
| `RASTER_HEIGHT_PX` | `480` | Longest receipt in dot lines. 480 is about 60 mm. See [Long receipts](#long-receipts). |
| `RASTER_DPI_X`, `RASTER_DPI_Y` | `203` | Print resolution. |

**Queue and limits**

| Variable | Default | Meaning |
|---|---|---|
| `PRINT_QUEUE_SIZE` | `16` | Jobs that may wait behind the one printing. When full, the newest message is dropped. |
| `MAX_JOBS_PER_MINUTE` | `30` | Jobs accepted per minute. The rest are dropped and reported. |

A number that is not a whole number, or is too small, is ignored and the default is used. A line in the log says so.

**Web panel**

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_USER`, `ADMIN_PASS` | `admin`, required | Panel login. |
| `WEB_PUBLISH_BIND` | `0.0.0.0` | Compose only. Host address the panel port is published on. `127.0.0.1` means this machine only. |
| `WEB_TLS_CERT` / `WEB_TLS_KEY` | off | Paths inside the container to a certificate and its key. Set both to serve the panel over HTTPS. See [Turn on HTTPS](#turn-on-https). |
| `WEB_BIND` | `0.0.0.0` | Address the panel listens on inside the container. Leave it alone with Docker's normal networking. |
| `LOGIN_MAX_FAILURES` | `5` | Wrong passwords in a row before an address is locked out. `0` turns the lockout off. |
| `LOGIN_LOCKOUT_SECONDS` | `60` | How long the lockout lasts. |
| `TEST_PRINT_COOLDOWN` | `10` | Seconds you must wait between test prints. `0` turns it off. |

**Container (Compose only)**

| Variable | Default | Meaning |
|---|---|---|
| `MEM_LIMIT` | `512m` | Memory ceiling for the container. Lower it to `384m` on a Pi Zero W, which has 426 MB in total. |

### How jobs are handled

- Jobs print one at a time, in the order they arrive. A slow print never stalls the MQTT connection.
- If `PRINT_QUEUE_SIZE` jobs are already waiting, or more than `MAX_JOBS_PER_MINUTE` came in the last minute, the **newest** message is dropped. A warning is logged and the drop is [published to MQTT](#dropped-and-failed-jobs).
- On `docker stop` the job being printed gets up to 10 seconds to finish. Jobs still waiting are discarded and logged.
- If the broker is down when the container starts, it keeps trying with a growing delay. It also reconnects by itself after an outage.

### Paper width

Set **`PAPER_WIDTH_MM`** in `.env` (or in `docker-compose.yml`) and restart the container:

```
PAPER_WIDTH_MM=80
```

| Value | Dots | Paper |
|---|---|---|
| `48` (default) | 384 | ZJ-58 / POS58 |
| `80` | 576 | ZJ-80 and other 80 mm printers |

Any other value is ignored and 48 is used, with a line in the log.

**80 mm has not been tested on a real printer.** It is covered by unit tests only. If a receipt comes out cut off or too narrow, set `PAPER_WIDTH_MM` back to `48` and open an issue.

### Long receipts

By default a receipt is at most 480 dot lines, about 60 mm. That is the title, the divider and about 10 lines of message text. Anything lower is cut, and the log says:

`Receipt raster needs N lines; capped to RASTER_HEIGHT_PX=480, lower text is cut`

To print longer receipts, raise `RASTER_HEIGHT_PX` in `.env` (for example `1600` for about 200 mm), then run `docker compose up -d` again. Nothing is published to MQTT when a receipt is cut. Only the log shows it.

---

## Security

Read this before you leave the panel or the broker open on your network.

| What | Default | What to do |
|---|---|---|
| **Who may print** | Anyone who can publish to the command topic prints. The container does not check who sent a message. | In your broker, allow only Home Assistant to publish to `printer/commands`, and only this container to publish to the other `printer/` topics. This is set up in the broker, not here. |
| **Broker login and print jobs** | Plain MQTT on port 1883. The password and every message can be read on the network. | Use a TLS listener: `MQTT_PORT=8883`, `MQTT_TLS=1`, and `MQTT_TLS_CA` for a private CA. The container logs a warning when it connects with no login, or without TLS to a broker that is not on this machine. |
| **Web panel** | Plain HTTP, published on every interface. The password can be read on the network. | [Turn on HTTPS](#turn-on-https), or set `WEB_PUBLISH_BIND=127.0.0.1` and reach the panel through an SSH tunnel. A note is logged at start while the panel is plain HTTP on a non-local address. |
| **Panel password guessing** | 5 wrong passwords from one address lock that address out for 60 seconds. | Use a long, unique `ADMIN_PASS`. Behind a reverse proxy all users share the proxy's address. |
| **Test print** | One test print per 10 seconds. The button sends a header that other websites cannot add. | If you call `/test-print` with curl, add `-H "X-Requested-With: PrintMQTTify"`. |
| **Flooding** | At most 16 jobs wait and 30 are accepted per minute, counted for all senders together (MQTT does not tell the container who sent a message). Each message is cut to 65,536 characters and to the lines that fit on one receipt before any drawing starts. Every drop is logged and published to `printer/last_error`. | Lower `PRINT_QUEUE_SIZE` or `MAX_JOBS_PER_MINUTE` if you want it stricter. |
| **Container rights** | The app runs as an unprivileged user with almost all Linux capabilities dropped. | If the device file is not group-writable on the host, the app runs as root instead and logs a warning. Make the device mode `660` and group `lp` on the host to avoid that. |

CUPS runs inside the container only for the availability check. It listens on the container's loopback and publishes no port.

---

### Turn on HTTPS

The panel serves HTTPS when you give it a certificate and key. Without them it stays plain HTTP, so existing installs keep working.

**1. Make a certificate.** Run this on the host, in the folder with `docker-compose.yml`. Add your host name or IP after `certs` if you use one.

```bash
scripts/make-cert.sh certs printer.local
```

It writes `certs/panel.crt` and `certs/panel.key`. To use your own certificate instead, skip this step.

**2. Mount the folder and set two lines in `.env`.** In `docker-compose.override.yml` add `volumes: ["./certs:/certs:ro"]` under the service. In `.env`:

```
WEB_TLS_CERT=/certs/panel.crt
WEB_TLS_KEY=/certs/panel.key
```

**3. Restart** with `docker compose up -d`, then open `https://<host>:8080`.

| You see | It means | Do this |
|---|---|---|
| Browser warns about the certificate | It is self-signed. | Accept it once, or import `panel.crt` into your browser. |
| Container exits with "not readable by the app user" | The files are missing or private to root. | Check the mount path and that the files are readable (mode 644). |
| Log says "Using plain HTTP" | Only one of the two settings is set, or the cert or key file cannot be read. | Set both, and make the files readable by the app user. |

With curl, add `--cacert certs/panel.crt`. The MQTT connection has its own TLS settings; see `MQTT_TLS` above.

## If something looks wrong

Run `docker logs printmqttify_container`.

| You see | It means | Do this |
|---|---|---|
| `Wrote N ESC/POS bytes to /dev/usb/lpN.` | The job reached the printer. | Nothing. If paper did not move, check the cable and that the printer is on. |
| `Error printing job: ...` | The job failed, often because the device path is wrong. | Check `PRINTER_DEVICE` and the `devices:` line match. See [`docs/troubleshooting.md`](./docs/troubleshooting.md). |
| `Device busy (attempt 1/8); retrying shortly` | Something else was using the printer for a moment. | Nothing. It retries for about 2 seconds. |
| `Receipt raster needs N lines; capped...` | The receipt is too long. | See [Long receipts](#long-receipts). |
| `WARNING: print queue full ...` | Jobs arrive faster than they print. | Send fewer, or raise `PRINT_QUEUE_SIZE`. |
| `WARNING: more than 30 jobs in a minute ...` | Too many messages in one minute. | Find what is sending them, or raise `MAX_JOBS_PER_MINUTE`. |
| `Error decoding JSON` or `Missing 'printer_name'` | The message was refused. | Fix the payload. Compare it with the example in [Printing a message](#printing-a-message). |
| `Cannot reach MQTT broker ...; retrying in Ns` | The broker address, port or network is wrong, or the broker is down. | Check `MQTT_BROKER` and `MQTT_PORT`. |
| `Failed to connect, return code 4` (or `5`) | The broker refused the login (4) or the account is not allowed (5). | Check `MQTT_USERNAME` and `MQTT_PASSWORD`. |
| `No paper status from ... (printer may be unidirectional)` | The printer does not answer the paper query. | Printing is fine. The paper sensors stay empty. |
| `WARNING: ... is not writable by the unprivileged 'receipt' user` | The app is running as root. | See the container rights row in [Security](#security). |
| `WARNING: connecting to the MQTT broker without a username and password` | No broker login is set. | Set `MQTT_USERNAME` and `MQTT_PASSWORD`. |

The paper sensors and the availability topic:

| Topic value | It means |
|---|---|
| `printer/availability` is `online` | The container is running, its monitoring queue is enabled, and the device file exists. |
| `printer/availability` is `offline` | The container stopped, or the device file is missing (for example, the printer was unplugged). It does not detect a printer that is plugged in but switched off or jammed. |
| `printer/paper` is `ON` / `OFF` | Paper present / out of paper. |
| `printer/paper_low` is `ON` / `OFF` | The near-end sensor has tripped / has not. Some printers have no such sensor and always say `OFF`. |

The printer is asked about paper every 60 seconds. When it does not answer, the last value stays.

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

Both carry `device_class: problem`, so "on" means something needs attention. The `availability_topic` makes them show as unavailable, not stale, when the container is down or the printer's device node has disappeared (for example, the USB cable was pulled).

### Dropped and failed jobs

Jobs the printer never printed are published, not just logged. Topics (all retained):

| Topic | Payload |
|---|---|
| `printer/last_error` | JSON: `timestamp` (UTC ISO 8601), `kind` (`dropped` = queue full or too many jobs per minute, `failed` = the job raised or the message was refused), `message`, `dropped_total`, `failed_total`. |
| `printer/dropped_total` | Integer, jobs dropped since the container started. |
| `printer/failed_total` | Integer, jobs that failed or were refused since the container started. |

`last_error` is retained, so Home Assistant still shows the most recent problem after a restart. The next error replaces it.

- `message` holds at most the first 40 characters of the job title and, for failures, a short error text. The message body is never published.
- The counters start at `0` when the container starts. They are published again, with their current values, each time the container reconnects to the broker.
- A message that is not valid JSON, is not a JSON object, or has no `printer_name` is refused. It counts as `failed` and shows up in `last_error`.
- You can rename the topics with `MQTT_LAST_ERROR_TOPIC`, `MQTT_DROPPED_TOTAL_TOPIC`, `MQTT_FAILED_TOTAL_TOPIC`, `MQTT_PAPER_TOPIC` and `MQTT_PAPER_LOW_TOPIC` (see [Settings](#settings)).
- If the broker cannot be reached, the error is logged and printing carries on.

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

Merge these under your existing `mqtt:` key (do not repeat it). Home Assistant does not find these sensors on its own, so add them by hand like the paper ones. An automation that triggers when `sensor.receipt_printer_failed_jobs` or `sensor.receipt_printer_dropped_jobs` goes up can notify you.

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

`title` prints bold above a divider, `message` below it, wrapped to the roll width.

- `printer_name` must be in the payload but does not pick a printer. Every job goes to `PRINTER_DEVICE`. Use `ReceiptPrinter`.
- Line breaks in `message` are kept. Runs of spaces and tabs become one space, so you cannot indent.
- The title is one line. Line breaks in it become spaces.
- Letters, numbers, accented Western letters, curly quotes and dashes print. Emoji and non-Latin scripts do not: they come out as placeholder marks.
- Messages over 65,536 characters are cut.
- Retained messages on the command topic are ignored. Publish with `retain: false`, as above.

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

The paper sensors go `unavailable` when the container is down or the printer's device node (for example `/dev/usb/lp1`) is gone. This automation cannot see a printer that is plugged in but jammed or switched off.

---

---

## Credits & thanks

This project wouldn't exist without the starting points these two gave it — huge thanks to both:

- **[Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify)** — the original MQTT-to-CUPS print client this project grew out of. Released under Creative Commons Zero v1.0 (CC0-1.0). Thank you for the foundation.
- **[klirichek/zj-58](https://github.com/klirichek/zj-58)** — the ZJ-58/ZJ-80 CUPS driver that got these printers working in the first place. The driver itself is no longer used here, but [`app/escpos.py`](./app/escpos.py) is a Python port of its ESC/POS output logic. Licensed BSD-2-Clause, © Aleksey N. Vinogradov (klirichek); the notice is kept in [`LICENSE.zj-58`](./LICENSE.zj-58). Thank you for reverse-engineering these printers.

If you find this useful, please go star both repositories above.

---

---

## A note on AI assistance

Parts of this fork — code, configuration, and documentation — were written with the help of generative AI. Everything in here was reviewed, tested against real hardware, and corrected by a human before being committed; nothing was accepted on the model's say-so. The debugging that produced most of these changes is recorded in [`docs/hardware-notes.md`](./docs/hardware-notes.md).

---

## License

This project is BSD 2-Clause, © rosco@rosco.cloud; see [`LICENSE`](./LICENSE). The PrintMQTTify code it grew from is Creative Commons Zero v1.0 (CC0-1.0) and stays so upstream. The Python port of the ZJ-58/ZJ-80 ESC/POS output logic ([`app/escpos.py`](./app/escpos.py)) remains BSD-2-Clause, © Aleksey N. Vinogradov (klirichek), under the terms in [`LICENSE.zj-58`](./LICENSE.zj-58).
