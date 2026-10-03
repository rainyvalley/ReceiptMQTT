# Hardware notes (labelprinter)

Things that live outside this repo but that the container depends on.

## Two thermal printers on one host

`lsusb` shows two:

- `09c5:0588` — serial `588U0324375`. Enumerates and accepts data, but does
  not mark paper. Its firmware self-test prints, so the head works; the data
  path does not.
- `0416:5011` — STMicroelectronics POS58, serial `Printer`. **This is the one
  that prints.** It is what `PRINTER_URI` points at.

The kernel assigns `/dev/usb/lp0` and `/dev/usb/lp1` in enumeration order, and
the two printers **swap between them** across reboots and `modprobe` cycles.
Check which is which before assuming:

```sh
cat /sys/class/usbmisc/lp0/device/../serial
cat /sys/class/usbmisc/lp1/device/../serial
```

The working printer is the one whose serial is *not* `588U0324375`.

## Why the file backend instead of usb://

CUPS' libusb USB backend enumerates the device and then loops on
`+connecting-to-device` / `-connecting-to-device` forever — it can see the
printer but cannot claim the interface from inside the container. Jobs sit at
"Waiting for printer to become available" and eventually trip `MaxJobTime`.

Writing to the kernel character device works, so the queue uses
`file:/dev/usb/lp1`. That requires `FileDevice Yes`, which lives in
`cups-files.conf` (not `cupsd.conf`) — hence `configs/cups-files.conf`.

`usblp` must stay loaded. Do not blacklist it.

## docker-compose

Credentials live in `docker-compose.override.yml` (git-ignored). The service
needs USB device access, not full privilege:

```yaml
    volumes:
      - /dev/bus/usb:/dev/bus/usb
    devices:
      - "/dev/usb/lp1:/dev/usb/lp1"
```

A pinned `devices:` entry like `/dev/bus/usb/001/004` is what caused a
ten-day silent outage when the device renumbered to `001/006`.

## From PPD filter to raw queue (2026-10)

CUPS deprecated and then removed PPD-based drivers (CUPS 3.x has no
`lpadmin -P`, no `*cupsFilter`). The klirichek/zj-58 driver this fork
shipped is exactly that kind of filter: a C binary (`rastertozj`) that read
CUPS raster and PPD options, plus PPDs generated in 2016.

The pipeline is now driverless on the CUPS side:

    reportlab PDF -> ghostscript pbmraw (1bpp, 203dpi, 384px wide)
                  -> app/escpos.py (port of rastertozj's emission logic)
                  -> lp -d ReceiptPrinter on a raw queue (file:/dev/usb/lpN)

What was ported, byte-for-byte, from rastertozj.c:

- `GS v 0 0` raster bands, max 24 rows each, width capped at 384 px
  (48 bytes).
- Blank bands are skipped with `ESC J 24` — printed out only when
  `BLANK_SPACE=0`, the old `BlankSpace=0Print` choice.
- Page end: `ESC J 0x18` repeated `FEED_DIST` times (PPD default was
  `2feed9mm` -> 2), then `ESC i` cut when `CUTTING=1`
  (`CutAtTheEndOfPage`); `CUTTING=2` cuts at job end.
- Cash drawers: `ESC p 0/1 0x40 0x50` before print (`*_DRAWER*=1`) or
  after (`=2`), wrapped around `ESC @` init/reset in the same order the C
  filter used.
- gs `pbmraw` rows are packed 1 bpp, black=1 — identical bit polarity to
  CUPS raster and to ESC/POS `GS v 0`, so bands transfer without
  repacking.

The old PPD options are environment variables on the container:
`FEED_DIST`, `BLANK_SPACE`, `CUTTING`, `CASH_DRAWER1`, `CASH_DRAWER2`
(numeric choice indexes, same meaning as before; defaults 2/1/1/0/0).
CUPS itself only needs `-m raw` now, which survives on CUPS 2.4 and 3.x
alike. The `file:` backend and `FileDevice Yes` are unchanged — that leg
never touched the filter chain.

Regression gate: `scripts/smoke_test.sh` regenerates the ESC/POS stream
with real ghostscript in CI; to re-gate against real hardware, capture a
known-good receipt with `cat /dev/usb/lp1` during a print and byte-diff
against `printer_mqtt_handler.print_job()` output.

## Page length (was a PPD matter)

The old PPD page sizes (`X48MMY60MM` etc.) became ghostscript geometry:
`RASTER_WIDTH_PX=384` x `RASTER_HEIGHT_PX` raster lines at 203 dpi. The
historical lesson still applies: rastertozj padded every job to the full
declared page length, so with a 210 mm declared page a two-line receipt fed
eight inches of paper. The reportlab stage sizes each receipt's PDF to its
own content, and `RASTER_HEIGHT_PX` only caps that — short receipts stay
short. The 60 mm default lives on as the 480-line raster height.

## Paper sensor

The handler polls the printer with `DLE EOT 4` (`10 04 04`) on the same
60s cadence as the availability check, and publishes `ON`/`OFF` to
`printer/paper` and `printer/paper_low` (both retained). On
`printer/paper`, `ON` means paper is present. On `printer/paper_low`,
`ON` means the near-end sensor has tripped.

Not every unit has a near-end sensor. If yours does not, those bits stay
clear and `printer/paper_low` simply reports `OFF` forever.

This needs a **bidirectional** printer. If yours only has a bulk-out
endpoint the query is never answered, the handler logs
"No paper status ... (printer may be unidirectional)" once, and the topic
is simply never published — it does not report a false "out".

Check by hand before assuming it works:

```sh
sudo docker compose exec printmqttify python3 - <<'PY'
import os, select
fd = os.open("/dev/usb/lp1", os.O_RDWR | os.O_NONBLOCK)
os.write(fd, b"\x10\x04\x04")
print(os.read(fd, 8) if select.select([fd], [], [], 1.0)[0] else "no response")
PY
```

A one-byte response is the status. Bits 5 and 6 both set (`& 0x60 == 0x60`)
means out of paper; bits 2 and 3 both set (`& 0x0C == 0x0C`) means the
near-end sensor has tripped, on units that have one.

The query opens the device directly, so it returns EBUSY while CUPS holds
it mid-job. That is treated as "unknown" and the last retained value stands.

### Home Assistant

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
