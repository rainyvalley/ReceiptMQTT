# Broker access for Home Assistant discovery

The app publishes retained Home Assistant discovery configs under
`homeassistant/...`. If the broker refuses them, Home Assistant simply never
sees the printer, often with no error anywhere. This page shows a setup that
works, in plain Mosquitto terms.

Names like `printer_one`, `ha` and `<password>` are examples. Use your own.

## What each printer uses

All topics come from one prefix (`MQTT_TOPIC_PREFIX`, default `printer`).
For a printer set to `printer_one`, the command topic is
`printer_one/commands`:

| Topic | Direction | Purpose |
|---|---|---|
| `printer_one/commands` | in | Print jobs. The app subscribes to this and nothing else. |
| `printer_one/availability` | out | `online` / `offline`. Also the MQTT last will. |
| `printer_one/paper`, `printer_one/paper_low` | out | Paper sensors. |
| `printer_one/last_error` | out | Last rejected or failed job. |
| `printer_one/dropped_total`, `printer_one/failed_total` | out | Job counters. |
| `homeassistant/...` | out | Retained discovery configs. |

Keep the printer's own tree. Removing `printer_one/#` from the ACL denies the
subscription and stops printing, usually without a clear error.

The `homeassistant/...` topics are write-only for the app. Home Assistant reads
them; it never sends print jobs through them.

## Discovery topic names

Home Assistant reads discovery only at this shape:

```
homeassistant/<component>/receiptmqtt_<node>/<entity>/config
```

- `<component>` is `binary_sensor` (availability, paper, paper_low) or
  `sensor` (last_error, dropped_total, failed_total).
- `<node>` comes from the command topic: lowercase, and runs of characters
  that are not letters or digits become `_`. `printer_one/commands` gives
  `printer_one_commands`.

The six config topics for that printer are:

```
homeassistant/binary_sensor/receiptmqtt_printer_one_commands/availability/config
homeassistant/binary_sensor/receiptmqtt_printer_one_commands/paper/config
homeassistant/binary_sensor/receiptmqtt_printer_one_commands/paper_low/config
homeassistant/sensor/receiptmqtt_printer_one_commands/last_error/config
homeassistant/sensor/receiptmqtt_printer_one_commands/dropped_total/config
homeassistant/sensor/receiptmqtt_printer_one_commands/failed_total/config
```

`HA_NODE_ID` replaces the derived node with a fixed value. The path then uses
`receiptmqtt_<HA_NODE_ID>`. This keeps the ACL stable when the command topic
changes.

## ACL, option 1: one line per printer

Each printer login may use its own tree and write only its own discovery
configs:

```conf
user printer_one
topic readwrite printer_one/#
topic write homeassistant/+/receiptmqtt_printer_one_commands/+/config

user printer_two
topic readwrite printer_two/#
topic write homeassistant/+/receiptmqtt_printer_two_commands/+/config

# Home Assistant reads every printer and may publish print jobs only
user ha
topic read #
topic write +/commands
```

Run `mosquitto_passwd -c /etc/mosquitto/passwd printer_one` (then again for
`printer_two` and `ha`) and point the broker at the file, for example:

```conf
allow_anonymous false
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/acl
```

## ACL, option 2: a wildcard per printer

One line per printer is enough, because everything that printer writes under
`homeassistant` is discovery output:

```conf
user printer_one
topic readwrite printer_one/#
topic write homeassistant/+/+/+/config
```

The trade-off: that login can also write the other printer's discovery
configs, so a bug or a leaked credential could add or change entities in Home
Assistant. It still cannot print on, or silence, the other printer, because
print and sensor topics stay inside each login's own tree.

## Check that it works

Subscribe first, then make the app reconnect (restart the container, or save a
setting in its panel). The app republishes the configs on every connect:

```bash
mosquitto_sub -h <broker> -u ha -P <password> \
  -t 'homeassistant/+/+/+/config' -v -W 8
```

Expect six retained lines per printer. `homeassistant/#` also works and shows
topics from other integrations too.

Wildcards must fill a whole topic level. `homeassistant/+/receiptmqtt_+/+/config`
looks reasonable but is invalid; `mosquitto_sub` rejects it.

A denied publish is easy to miss. Some broker builds accept the publish from
the client's point of view and write nothing to the log. Check by delivery,
not by log lines.

## Test the printer and the ACL

Publish a print job as the Home Assistant login:

```bash
mosquitto_pub -h <broker> -u ha -P <password> \
  -t printer_one/commands \
  -m '{"printer_name": "ReceiptPrinter", "title": "Test", "message": "Hello"}'
```

Test an ACL with a probe as the printer login:

```bash
mosquitto_pub -h <broker> -u printer_one -P <password> \
  -t homeassistant/sensor/receiptmqtt_printer_one_commands/aclprobe/config \
  -m "acl probe"
```

Do not publish valid JSON to a `homeassistant/.../config` topic unless you
want Home Assistant to create an entity from it. Use a non-JSON payload for
probes, and do not retain it.

## When the topic prefix changes

The panel can change `MQTT_TOPIC_PREFIX`. The discovery paths and the ACL
line follow it. Either update the ACL, or set `HA_NODE_ID` first (for example
`HA_NODE_ID=printer_one_commands`), so the paths and ACL stay as they are.

Reload the broker after ACL changes, for example `pkill -HUP mosquitto` or
`systemctl reload mosquitto`. Existing connections stay up; the app republishes
discovery on its next reconnect.

## Two printers on one broker

Give each printer its own login, its own topic tree and its own discovery
line. A mistake on one then cannot print on, or silence, the other. The
[Two printers on two systems](../README.md#two-printers-on-two-systems)
section has a full Compose example.
