#!/usr/bin/env bash
# Hardware-free smoke test for the legacy (PPD + compiled filter) stack.
#
# The legacy handler's runtime leg needs a CUPS daemon and a configured
# queue (it calls lp), which CI does not have. This gate therefore covers
# what CI can exercise: source integrity of the files the image COPYs,
# module import, and PDF generation in-process when real reportlab exists.
#
# Replaces the raw-queue smoke test (escpos selftest/gs pipeline), which
# tests code that no longer exists in this stack.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== source integrity: files the dockerfile COPYs =="
for f in app/printer_mqtt_handler.py app/web_control_panel.py \
         app/templates/index.html configs/cupsd.conf configs/cups-files.conf \
         configs/ReceiptPrinter.ppd entrypoint.sh dockerfile \
         drivers/SEWOO/sewoocupsinstall_amd64.tar.gz; do
  [ -f "$f" ] || { echo "FAIL: missing $f"; exit 1; }
done
echo "PASS all build inputs present"

echo "== python compile =="
python3 -m py_compile app/printer_mqtt_handler.py app/web_control_panel.py
echo "PASS py_compile"

echo "== real reportlab available? =="
if python3 -c "import reportlab" 2>/dev/null; then
  HAS_RL=yes
else
  HAS_RL=no
fi
echo "reportlab: $HAS_RL"

echo "== legacy handler unit import =="
python3 - <<PYEOF
import sys, types
# Hosts without broker deps: stub paho.mqtt.client so the module body can
# be exec'd (paho uses real submodule import semantics; wire the parent
# attrs explicitly).
paho = types.ModuleType("paho")
sub_mqtt = types.ModuleType("paho.mqtt")
client = types.ModuleType("paho.mqtt.client")
paho.mqtt = sub_mqtt
sub_mqtt.client = client
paho.__path__ = []
sub_mqtt.__path__ = []
sys.modules["paho"] = paho
sys.modules["paho.mqtt"] = sub_mqtt
sys.modules["paho.mqtt.client"] = client

try:
    import reportlab  # noqa: F401
except ImportError:
    # reportlab is apt-only (PEP 668); stub it on hosts that lack it.
    rl = types.ModuleType("reportlab")
    pdfgen = types.ModuleType("reportlab.pdfgen")
    lib = types.ModuleType("reportlab.lib")
    pagesizes = types.ModuleType("reportlab.lib.pagesizes")
    rl.pdfgen = pdfgen
    pdfgen.canvas = types.ModuleType("reportlab.pdfgen.canvas")
    pdfgen.canvas.Canvas = object
    rl.lib = lib
    lib.pagesizes = pagesizes
    pagesizes.mm = 2.834645669
    for name, mod in [("reportlab", rl), ("reportlab.pdfgen", pdfgen),
                      ("reportlab.pdfgen.canvas", pdfgen.canvas),
                      ("reportlab.lib", lib),
                      ("reportlab.lib.pagesizes", pagesizes)]:
        sys.modules[name] = mod

import importlib.util
spec = importlib.util.spec_from_file_location(
    "printer_mqtt_handler", "app/printer_mqtt_handler.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
print("PASS module import")
PYEOF

if [ "$HAS_RL" = yes ]; then
  echo "== generate_pdf output check (writes /tmp/print_job.pdf) =="
  python3 - <<'PY'
import importlib.util, os
spec = importlib.util.spec_from_file_location(
    "printer_mqtt_handler", "app/printer_mqtt_handler.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

pdf_path = h.generate_pdf("Smoke Test", "Line one\nLine two")
assert pdf_path and os.path.exists(pdf_path), f"no PDF at {pdf_path}"
with open(pdf_path, "rb") as fh:
    head = fh.read(5)
assert head == b"%PDF-", f"not a PDF: {head!r}"
print(f"PASS generate_pdf produced {pdf_path}")
os.unlink(pdf_path)
PY
else
  echo "== generate_pdf skipped: reportlab missing here (CI installs it) =="
fi

echo "ALL SMOKE TESTS PASSED"