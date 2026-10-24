# Troubleshooting

## Port mapping

The control panel is published on 8080. The app writes ESC/POS bytes straight
to `PRINTER_DEVICE`, so no print path uses CUPS; CUPS stays loopback-only
(`127.0.0.1:631`) and is never published — it exists for `lpstat` monitoring.

## Control panel

Cannot reach the panel:

- Ensure the container is running: `docker logs printmqttify_container`.
- The panel is protected with HTTP Basic auth; log in with your
  `ADMIN_USER` / `ADMIN_PASS` values. After 5 wrong passwords in a row the
  panel answers "429 Too many wrong passwords" for a minute.
- **Send Test Print** answers "wait Ns" if you press it twice within 10 seconds.
- The container must not be started with `ADMIN_PASS` unset: the entrypoint
  refuses to start without a password.

## Printer not printing

- Check the handler log first: `docker logs printmqttify_container` should
  show `Wrote N ESC/POS bytes to /dev/usb/lpN.` for each job. If it shows
  `Error printing job: ...` instead, the device is wrong or busy, and the
  failure is also published to `printer/last_error`.
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
- Check that your messages go to the topic the container listens on
  (`MQTT_TOPIC`, default `printer/commands`). The panel home page shows it.
- A line `Error decoding JSON` or `Missing 'printer_name'` means the message
  was refused. It is also published to `printer/last_error`.
- From a shell, publish a test message (see README) and watch
  `docker logs printmqttify_container` for `Received message` followed by
  `Printed N ESC/POS bytes.`