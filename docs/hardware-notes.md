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
