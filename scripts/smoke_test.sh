#!/usr/bin/env bash
# Hardware-free end-to-end smoke test for the raw-queue print pipeline.
#
# 1. app/escpos.py selftest: header + tail structure of the ESC/POS stream.
# 2. Raster banding structure with synthetic PBM input.
# 3. Full pipeline (reportlab -> ghostscript pbmraw -> escpos): runs when
#    reportlab is installed (CI installs it via apt inside bookworm-slim);
#    skipped silently on hosts that cannot install it (PEP 668).
#
# Optional hardware-free CUPS leg: start the container with
# PRINTER_URI=file:/tmp/out.bin, publish an MQTT test message, and check
# /tmp/out.bin for the expected bytes below.
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)/app"

echo "== escpos selftest =="
SELFTEST="$(python3 "$APP_DIR/escpos.py" --selftest | head -c 6 | xxd -p)"
[ "$SELFTEST" = "1b401d763000" ] || { echo "FAIL: selftest header wrong: $SELFTEST"; exit 1; }
echo "PASS header 1b40 + GS v 0"

echo "== raster byte structure =="
python3 - <<'PY'
import sys
sys.path.insert(0, "app")
import escpos
rows = 24
width = 384
pbm = b"P4\n%d %d\n%s" % (width, rows, b"\xff" * (48 * rows))
out, pages = escpos.render_all(pbm, escpos.EscposSettings())
assert pages == 1
assert out[:2] == b"\x1b@", "job setup missing"
assert out[-2:] == b"\x1b@", "job finish missing"
assert out.count(b"\x1d\x76\x30\x00") == 1, "expected exactly one raster band"
# header = GS v 0 0 + width lo/hi = 48,0 + height lo/hi = 24,0
band = escpos.raster_header(48, 24)
assert out[2:10] == band, out[2:10].hex()
print("PASS raster banding")
PY

PYOK="$(python3 -c "import reportlab" 2>/dev/null && echo yes || echo no)"
if [ "$PYOK" != "yes" ]; then
  echo "== full pipeline skipped: reportlab not installed (CI covers it) =="
  echo "ALL SMOKE TESTS PASSED (partial: no reportlab)"
  exit 0
fi

echo "== full pipeline (reportlab -> gs -> escpos) =="
BYTES="$(python3 - <<'PY'
import sys
sys.path.insert(0, "app")
import importlib.util
spec = importlib.util.spec_from_file_location(
    "printer_mqtt_handler", "app/printer_mqtt_handler.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
import escpos

# reportlab -> gs -> escpos, without the CUPS queue leg (no cupsd here)
import tempfile, os
settings = escpos.EscposSettings.from_env()
fd, pdf_path = tempfile.mkstemp(prefix="smoke_", suffix=".pdf")
os.close(fd)
try:
    h.generate_pdf("Smoke Test", "Line one\nLine two", pdf_path)
    esc = h.pdf_to_escpos(pdf_path, settings)
finally:
    os.unlink(pdf_path)
assert esc.startswith(b"\x1b@"), "missing ESC @"
assert b"\x1d\x76\x30\x00" in esc, "missing GS v 0 raster band"
assert esc.endswith(b"\x1bi\x1b@") or esc.endswith(b"\x1b@\x1bi"), \
    f"bad tail {esc[-8:].hex()}"
print(len(esc))
PY
)"
echo "PASS pipeline produced $BYTES ESC/POS bytes"

echo "ALL SMOKE TESTS PASSED"