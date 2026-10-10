"""Pytest bootstrap: make app/ importable (the modules live flat in app/)."""

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

# web_control_panel refuses to start without ADMIN_PASS; the panel tests
# import it, so a test default must exist before that import.
os.environ.setdefault("ADMIN_PASS", "test-panel-pass")