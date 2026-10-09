# Troubleshooting

## Port mapping

The control panel is published on 8080. CUPS needs no published port: it is
loopback-only inside the container and only receives print-ready bytes from
the app in-process.

## Control panel

Cannot reach the panel:

- Ensure the container is running: `docker logs printmqttify_container`.
- The panel is protected with HTTP Basic auth; log in with your
  `ADMIN_USER` / `ADMIN_PASS` values.
- The container must not be started with `ADMIN_PASS` unset: the entrypoint
  refuses to start without a password.

## Printer not printing

- Check the handler log first: `docker logs printmqttify_container` should
  show `Wrote N ESC/POS bytes to /dev/usb/lpN.` for each job. If it shows
  `Direct write ... failed` instead, the device is wrong or busy.
- Verify the printer device matches the mapped USB device: the queue device
  (`docker exec printmqttify_container lpstat -v`) should show
  `file:/dev/usb/lpN`, and `PRINTER_DEVICE` on the container must match the
  path given to Compose (`devices:`) — see docs/hardware-notes.md, which
  also explains how the two USB printers can swap `/dev/usb/lp0` and
  `/dev/usb/lp1` across reboots.
- Printing writes directly to the device (CUPS is monitoring-only): jobs do
  NOT flow through `lp`. If `lpstat -W not-completed` shows stuck jobs, they
  are leftovers from an earlier stack — `cancel -a` clears them and changes
  nothing about printing.
- ESC/POS is emitted by the app itself (`app/escpos.py`); there is no CUPS
  print driver to install, so "wrong driver" cannot be the cause.

## MQTT issues

- Verify the MQTT broker details in the container environment variables.
- Check the MQTT topic for incoming messages.
- From a shell, publish a test message (see README) and watch
  `docker logs printmqttify_container` for `Received message` followed by
  `Printed N ESC/POS bytes.`