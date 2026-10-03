# AGENTS.md

Guidance for AI agents and human contributors working in this repository.

## What this is

A Docker bridge between MQTT and a CUPS printer for cheap ESC/POS thermal receipt printers (Zijiang ZJ-58 / ZJ-80 and POS58-class clones). Publish a JSON message to an MQTT topic and it prints. Fork of [Aesgarth/PrintMQTTify](https://github.com/Aesgarth/PrintMQTTify); origin remote is `rainyvalley/ReceiptMQTT` (note the name mismatch: directory is `PrintMQTTify`, images are `ghcr.io/rainyvalley/receiptmqtt`).

There is no CUPS printer driver in the image at all. Printing is done entirely in-app; the CUPS queue is raw and exists only to move bytes to a file device.

## Architecture / data flow

```
MQTT (printer/commands)            paho-mqtt client in app/printer_mqtt_handler.py
  -> reportlab PDF                 generate_pdf(): page sized to content, Helvetica
  -> ghostscript pbmraw            subprocess gs: 1 bpp packed, black=1, 203 dpi, 384 px wide
  -> app/escpos.py                 Python port of the rastertozj CUPS filter logic
  -> lp -d ReceiptPrinter          raw CUPS queue (lpadmin -m raw, no PPD)
  -> file:/dev/usb/lpN             file backend writes to the kernel usblp char device
  -> printer
```

Concurrently, a daemon thread polls every 60 s and publishes retained MQTT state: `printer/availability` (from `lpstat -p`), `printer/paper` and `printer/paper_low` (from a `DLE EOT 4` query written directly to the printer device, `PAPER_DEVICE`, default `/dev/usb/lp1`).

Supporting processes in the container (`entrypoint.sh`, which ends running the MQTT handler in the foreground): CUPS (loopback-only, `Listen 127.0.0.1:631`), the Flask control panel on 8080 (Basic auth, `ADMIN_USER`/`ADMIN_PASS`, status + test-print only; all real config is env vars).

### Key invariant: bit polarity and geometry

`gs -sDEVICE=pbmraw` output and ESC/POS `GS v 0` raster use identical bit ordering (packed 1 bpp, black=1), so bands transfer without repacking. The PDF page width (48 mm, hardcoded in `generate_pdf`) **must** match `RASTER_WIDTH_PX` (384) at 203 dpi, or ghostscript silently scales/crops. `app/escpos.py` caps width at 48 bytes (384 px) and bands at 24 rows; blank bands are skipped with `ESC J 24` unless `BLANK_SPACE=0`. These constants are a byte-for-byte port of `rastertozj.c` (klirichek/zj-58) — changing them breaks parity with known-good hardware output. `scripts/smoke_test.sh` is the regression gate; see `docs/hardware-notes.md` for how to re-gate against real hardware (byte-diff a captured receipt against `print_job()` output).

## Repo layout

| Path | Role |
|---|---|
| `app/printer_mqtt_handler.py` | MQTT subscriber, PDF generation, rasterization, lp queueing, availability/paper thread |
| `app/escpos.py` | ESC/POS encoder. Standalone CLI too (`python3 app/escpos.py [--selftest] [file.pbm]`); reads PBM from stdin/file, writes ESC/POS to stdout |
| `app/web_control_panel.py` | Flask panel: `/`, `/status`, `/test-print` |
| `entrypoint.sh` | Copies CUPS configs, creates admin user, starts dbus/avahi/cupsd, creates raw queue, starts panel + handler |
| `configs/cupsd.conf`, `configs/cups-files.conf` | Baked into image, copied over `/etc/cups` at start. `FileDevice Yes` lives in cups-files.conf and is what makes the `file:` backend legal |
| `dockerfile` | debian:bookworm-slim; note the lowercase name, CI references it explicitly |
| `scripts/smoke_test.sh` | The only test suite |
| `docs/hardware-notes.md` | Read this before touching anything device-related |
| `docs/troubleshooting.md`, `docs/HA Script Examples.md` | Ops documentation |
| `drivers/SEWOO/` | Legacy bundled driver, unused since the raw-queue switch |
| `app/printer_mqtt_handler.py.backup`, `app/printer_mqtt_handler.py.pdf-version`, `entrypoint.sh.bak2` | Stale copies, not used at runtime (dockerfile copies only the three current app files). Do not edit them |

## Commands

```bash
# Test (hardware-free, run from repo root)
bash scripts/smoke_test.sh

# Build — the file is named `dockerfile` (lowercase), so -f is required;
# docker's default lookup for `Dockerfile` fails otherwise
docker build -f dockerfile -t ghcr.io/rainyvalley/receiptmqtt:local .
# Tagger tip: tag as :latest and tracked compose (which has no build: key)
# will use the local image on `up` without needing an override image: entry

# Run — credentials via git-ignored docker-compose.override.yml, never in the tracked compose
docker compose up -d

# Container-side debugging
docker logs printmqttify_container
docker exec printmqttify_container lpstat -p     # queue state
docker exec printmqttify_container lpstat -v     # queue device URI

# Test message
mosquitto_pub -h <broker> -u <user> -P <pass> -t printer/commands \
  -m '{"printer_name": "ReceiptPrinter", "title": "Test", "message": "Hello, World!"}'
```

Smoke test legs: escpos selftest header check; synthetic-PBM banding structure; full pipeline (PDF → gs → escpos), which is **skipped silently** when reportlab is absent — don't trust "passed" output on a host without it. Installing the full leg: `apt` packages `python3-reportlab python3-paho-mqtt ghostscript xxd` (see PEP 668 below).

CI (`.github/workflows/docker-publish.yml`): smoke test in a `debian:bookworm-slim` container, then multi-arch (amd64 + arm64) build to GHCR on push to `main` and `v*` tags (tags `latest`, `sha-*`, semver). CUPS is never started in CI; the smoke test deliberately stops before the CUPS-queue leg.

## Configuration surface (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `MQTT_BROKER` / `MQTT_USERNAME` / `MQTT_PASSWORD` | `localhost` / none | |
| `MQTT_TOPIC` | `printer/commands` | Payload contract: JSON; `printer_name` required and must equal the CUPS queue name; `title`, `message` optional |
| `PRINTER_NAME` | `ReceiptPrinter` | CUPS queue name, created by entrypoint |
| `PRINTER_URI` | `file:/dev/usb/lp1` | Must match the `devices:` mapping passed to the container |
| `PAPER_DEVICE` | `/dev/usb/lp1` | Device the paper-sensor query opens directly |
| `MQTT_PAPER_TOPIC` / `MQTT_PAPER_LOW_TOPIC` | `printer/paper` / `printer/paper_low` | Retained, ON/OFF |
| `RASTER_DPI_X` / `RASTER_DPI_Y` | `203` / `203` | |
| `RASTER_WIDTH_PX` / `RASTER_HEIGHT_PX` | `384` / `480` | Width must stay consistent with the hardcoded 48 mm PDF page; height only caps page allocation, receipts are cut to content |
| `FEED_DIST` / `BLANK_SPACE` / `CUTTING` / `CASH_DRAWER1` / `CASH_DRAWER2` | `2` / `1` / `1` / `0` / `0` | Old PPD option choices, numeric; see `EscposSettings` and hardware-notes |
| `ADMIN_USER` / `ADMIN_PASS` | `admin` / **required** | Both `entrypoint.sh` and `web_control_panel.py` refuse to start without `ADMIN_PASS` |

`printer/availability` is fixed, not env-configurable. Config is read once at module import; everything runs through container env vars, no config files.

## Gotchas

- **Python deps come from apt, not pip.** bookworm marks the system Python as externally managed (PEP 668); bare `pip3 install` fails. This applies to the image, CI, and any host testing. There is no requirements.txt, no venv, no linter/formatter config.
- **Web panel bind address.** `web_control_panel.py` runs Flask with `host='0.0.0.0'` so the compose-published `8080:8080` actually reaches it; earlier builds bound `127.0.0.1`, which made the panel unreachable from outside the container.
- **The USB printers swap `/dev/usb/lp0` and `/dev/usb/lp1` across reboots.** Kernel enumeration order changes. Identify by serial (`cat /sys/class/usbmisc/lpN/device/../serial`); on this host the broken unit has serial `588U0324375`. Never pin `/dev/bus/usb/001/004`-style paths in compose (caused a ten-day silent outage when the device renumbered).
- **CUPS libusb backend does not work in the container** — the queue deliberately uses the `file:` backend on the kernel char device, which requires `FileDevice Yes` in cups-files.conf. Don't "fix" this by switching to `usb://`. `usblp` must stay loaded on the host.
- **Paper sensor needs a bidirectional printer.** An unanswered `DLE EOT 4` returns `None` = *unknown*, deliberately not "out of paper"; the last retained value stands. The query returns EBUSY while CUPS holds the device mid-job (also treated as unknown). Only the polling thread may ever open the device — don't add a second writer/opener.
- **MQTT failures look quiet by design.** On wrong credentials the handler logs `Failed to connect, return code 5 (not authorized)` rather than crashing; entrypoint keeps running with only the panel alive.
- **The queue never stalls the app.** Error policy is `abort-job`, `MaxJobTime 300`; a stalled job disables nothing and the availability check treats only `disabled` as offline.
- **Message text: stick to plain ASCII.** The PDF path handles UTF-8 via Helvetica metrics but rendering odd glyphs on a 384-px thermal raster is not reliable.
- **CUPS is loopback-only.** Do not re-add a 631 port publication; remote control goes through the Flask panel only.

## Conventions

- Comments explain *why*, especially hardware/history context; keep that style. `docs/hardware-notes.md` doubles as the design log for non-obvious decisions.
- Keep README/docs in sync when changing user-visible behavior (env vars, topics, deploy steps).
- Commit messages follow `Area: summary` (`CI:`, `Docs:`, `Compose:`, `README:`), first line under 72 chars.
- Licensing is split: CC0-1.0 for the PrintMQTTify portion, BSD-2-Clause (klirichek/zj-58) for the ported ESC/POS logic in `app/escpos.py`. Preserve attribution there.
- Import-time `try/except ImportError` guards around `paho` and `reportlab` exist so modules import cleanly in the CI/dependency-less smoke test; keep them when touching the top of `printer_mqtt_handler.py`.