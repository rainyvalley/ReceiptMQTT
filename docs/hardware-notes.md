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

Not in this repo (it holds credentials). The service needs:

```yaml
    privileged: true
    volumes:
      - /dev/bus/usb:/dev/bus/usb
      - /dev/usb:/dev/usb
```

A pinned `devices:` entry like `/dev/bus/usb/001/004` is what caused a
ten-day silent outage when the device renumbered to `001/006`.

## Page size

`configs/ReceiptPrinter.ppd` is `zj58.ppd` with two short page sizes added
(`X48MMY60MM`, `X48MMY105MM`) and the default changed to the 60mm one. The
stock PPD's shortest page was 210mm, and rastertozj pads every job to the
full declared page length — so a two-line receipt fed eight inches of paper.

Adding a size means four matching entries (`*PageSize`, `*PageRegion`,
`*ImageableArea`, `*PaperDimension`) plus a `*ru.PageSize` translation line,
or `cupstestppd` fails. Note `*PageRegion` uses width 164 where the others
use 136.

## Paper sensor

The handler polls the printer with `DLE EOT 4` (`10 04 04`) on the same
60s cadence as the availability check, and publishes `ON`/`OFF` to
`printer/paper` (retained). `ON` means paper is present.

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
      state_topic: "printer/paper"
      payload_on: "OFF"          # ON = a problem = out of paper
      payload_off: "ON"
      device_class: problem
      availability_topic: "printer/availability"
      payload_available: "online"
      payload_not_available: "offline"
```
