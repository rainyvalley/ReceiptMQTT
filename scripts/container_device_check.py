"""Container-side device check for CI: push ESC/POS through the real send
path and prove the bytes land in PRINTER_DEVICE.

Run inside the built image by the container-check workflow job:

    docker run --rm -e PRINTER_DEVICE=/tmp/out.bin \\
        -v <host dir>:/tmp -v "$PWD/scripts:/containerscripts:ro" \\
        --entrypoint python3 receiptmqtt:ci \\
        /containerscripts/container_device_check.py /tmp/out.bin

No broker is involved - the handler's send_raw_to_printer is called
directly, so this verifies the containerised runtime, the app code path
and the device write, not an MQTT round-trip.
"""

import os
import sys

sys.path.insert(0, "/app")
from printer_mqtt_handler import send_raw_to_printer  # noqa: E402

# A framed one-band job: ESC @ reset, GS v 0 0 (xDim=2, yDim=1) + one band
# byte, then ESC @ reset. 0x1d 0x76 0x30 is the DLE-V style raster header
# every ESC/POS renderer starts a band with.
PAYLOAD = b"\x1b@\x1d\x76\x30\x00\x02\x01\x55\x1b@"


def main(device_path):
    # The real PRINTER_DEVICE is an existing usblp node created on the
    # host; here the mounted-file stand-in has to exist before the send
    # path opens it O_WRONLY|O_SYNC.
    os.close(os.open(device_path, os.O_RDWR | os.O_CREAT, 0o644))
    send_raw_to_printer("ReceiptPrinter", PAYLOAD)
    with open(device_path, "rb") as fh:
        got = fh.read()
    assert got == PAYLOAD, f"device mismatch: got {got.hex()!r}"
    assert got.count(b"\x1d\x76\x30\x00") == 1, "missing raster (GS v 0) header"
    print(f"PASS: raster band bytes reached {device_path} "
          f"({len(got)} bytes, ESC @ + GS v 0 verified)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:  # keep the job log readable
        print(f"container device check FAILED: {exc}", file=sys.stderr)
        raise