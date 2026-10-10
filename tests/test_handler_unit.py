"""Unit tests for printer_mqtt_handler: MQTT intake, device retries, PDF maths.

Offline throughout: the send path is exercised via monkeypatched writes and
ghostscript is replaced by a fake subprocess returning canned PBM raster.
"""

import errno
import json
import subprocess
import types

import pytest

import escpos
import printer_mqtt_handler as h


class FakeMsg:
    """Minimum surface of a paho client message."""

    def __init__(self, payload, topic="printer/commands"):
        self.payload = payload
        self.topic = topic


class FakeClient:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, *args, **kwargs):
        self.subscriptions.append((args, kwargs))


@pytest.fixture
def recorded_jobs(monkeypatch):
    calls = []

    def fake_print_job(title, message, printer_name):
        calls.append((title, message, printer_name))

    monkeypatch.setattr(h, "print_job", fake_print_job)
    return calls


# ------------------------------------------------------------------ on_message

class TestOnMessage:
    def test_good_payload_dispatches_print_job(self, recorded_jobs):
        raw = json.dumps({"printer_name": "ReceiptPrinter",
                          "title": "Alert", "message": "hello"}).encode()
        h.on_message(None, None, FakeMsg(raw))
        assert recorded_jobs == [("Alert", "hello", "ReceiptPrinter")]

    def test_defaults_for_missing_title_and_message(self, recorded_jobs):
        raw = json.dumps({"printer_name": "P"}).encode()
        h.on_message(None, None, FakeMsg(raw))
        assert recorded_jobs == [("Print Job", "No message provided", "P")]

    def test_bad_json_never_reaches_print_job(self, recorded_jobs, capsys):
        h.on_message(None, None, FakeMsg(b"{not json"))
        assert recorded_jobs == []
        assert "Error decoding JSON" in capsys.readouterr().out

    def test_missing_printer_name_rejected(self, recorded_jobs, capsys):
        raw = json.dumps({"title": "T", "message": "m"}).encode()
        h.on_message(None, None, FakeMsg(raw))
        assert recorded_jobs == []
        assert "Missing 'printer_name'" in capsys.readouterr().out

    def test_empty_printer_name_rejected(self, recorded_jobs):
        raw = json.dumps({"printer_name": ""}).encode()
        h.on_message(None, None, FakeMsg(raw))
        assert recorded_jobs == []

    def test_oversized_message_truncated(self, recorded_jobs):
        raw = json.dumps({"printer_name": "P",
                          "message": "x" * (h.MAX_MESSAGE_CHARS + 5000)}).encode()
        h.on_message(None, None, FakeMsg(raw))
        assert len(recorded_jobs) == 1
        assert len(recorded_jobs[0][1]) == h.MAX_MESSAGE_CHARS

    def test_invalid_utf8_bytes_dropped_not_replaced(self, recorded_jobs):
        # one raw 0xFF byte inside the JSON string; decode(errors="ignore")
        # must drop it, not turn it into U+FFFD (which the WinAnsi fonts in
        # reportlab cannot encode and would silently kill the receipt)
        raw = b'{"printer_name": "P", "message": "ok \xff done"}'
        h.on_message(None, None, FakeMsg(raw))
        assert len(recorded_jobs) == 1
        message = recorded_jobs[0][1]
        assert "�" not in message
        assert message == "ok  done"           # byte dropped, not replaced

    def test_print_job_failure_is_caught(self, monkeypatch):
        def boom(title, message, printer_name):
            raise RuntimeError("gs exploded")
        # on_message resolves print_job from the module namespace at call
        # time, so patch the real one this time instead of the fixture.
        monkeypatch.setattr(h, "print_job", boom)
        raw = json.dumps({"printer_name": "P"}).encode()
        h.on_message(None, None, FakeMsg(raw))   # must not raise


# ----------------------------------------------------------- send_raw_to_printer

class TestSendRawToPrinter:
    PAYLOAD = b"\x1b@\x1d\x76\x30"

    def test_direct_success(self, monkeypatch):
        written = {}
        monkeypatch.setattr(h, "_write_device",
                            lambda path, data: written.update(path=path,
                                                              data=data) or len(data))
        monkeypatch.setattr(h, "printer_device", "/tmp/dev")
        h.send_raw_to_printer("R", self.PAYLOAD)
        assert written == {"path": "/tmp/dev", "data": self.PAYLOAD}

    def test_busy_then_success_retries(self, monkeypatch):
        attempts = []
        real_len = len(self.PAYLOAD)

        def flaky(path, data):
            attempts.append(path)
            if len(attempts) < 4:
                raise OSError(errno.EBUSY, "Device busy")
            return real_len

        sleeps = []
        monkeypatch.setattr(h, "_write_device", flaky)
        monkeypatch.setattr(h, "printer_device", "/tmp/dev")
        monkeypatch.setattr(h.time, "sleep", lambda s: sleeps.append(s))
        h.send_raw_to_printer("R", self.PAYLOAD)
        assert len(attempts) == 4
        assert sleeps == [h.DEVICE_BUSY_BACKOFF] * 3

    def test_persistent_busy_raises_last_error(self, monkeypatch):
        attempts = []

        def busy(path, data):
            attempts.append(path)
            raise OSError(errno.EBUSY, "Device busy")

        monkeypatch.setattr(h, "_write_device", busy)
        monkeypatch.setattr(h, "printer_device", "/tmp/dev")
        monkeypatch.setattr(h.time, "sleep", lambda s: None)
        with pytest.raises(OSError) as exc:
            h.send_raw_to_printer("R", self.PAYLOAD)
        assert exc.value.errno == errno.EBUSY
        assert len(attempts) == h.DEVICE_BUSY_RETRIES

    def test_other_errno_fails_immediately(self, monkeypatch):
        attempts = []

        def io_error(path, data):
            attempts.append(path)
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(h, "_write_device", io_error)
        monkeypatch.setattr(h, "printer_device", "/tmp/dev")
        monkeypatch.setattr(h.time, "sleep", lambda s: None)
        with pytest.raises(OSError) as exc:
            h.send_raw_to_printer("R", self.PAYLOAD)
        assert exc.value.errno == errno.EIO
        assert len(attempts) == 1

    def test_no_device_configured_raises(self, monkeypatch):
        monkeypatch.setattr(h, "printer_device", "")
        with pytest.raises(RuntimeError, match="PRINTER_DEVICE"):
            h.send_raw_to_printer("R", self.PAYLOAD)

    def test_partial_writes_drained(self, monkeypatch):
        # os.write may accept fewer bytes than handed to it; the loop in
        # _write_device must keep going until everything is written.
        chunks = []
        monkeypatch.setattr(h.os, "write",
                            lambda fd, data: chunks.append(data) or 2)
        monkeypatch.setattr(h.os, "open", lambda *a, **k: 99)
        monkeypatch.setattr(h.os, "close", lambda fd: None)
        n = h._write_device("/tmp/dev", b"abcdef")
        assert n == 6
        # each write is handed the not-yet-written remainder
        assert chunks == [b"abcdef", b"cdef", b"ef"]


# ------------------------------------------------------------- pdf_to_escpos

class TestPdfToEscpos:
    @staticmethod
    def fake_gs(monkeypatch, pbm, returncode=0):
        """Stub subprocess.run (ghostscript); records the gs command line."""
        calls = []

        def run(cmd, *a, **k):
            calls.append(cmd)
            if returncode != 0:
                raise subprocess.CalledProcessError(returncode, "gs",
                                                    b"", b"cups filter gone")
            return types.SimpleNamespace(returncode=0, stdout=pbm, stderr=b"")

        monkeypatch.setattr(h.subprocess, "run", run)
        return calls

    def test_raster_height_sized_from_page(self, monkeypatch):
        calls = self.fake_gs(monkeypatch, b"P4\n8 1\n\x80")
        out = h.pdf_to_escpos("/tmp/x.pdf", escpos.EscposSettings(),
                              page_height_pt=72)
        # 72 pt at 203 dpi = 203 lines, +4 safety -> 207
        assert f"-g{h.RASTER_WIDTH_PX}x{72 * h.RASTER_DPI_Y // 72 + 4}" in calls[0]
        assert out.startswith(b"\x1b@")
        assert escpos.RASTER_START in out

    def test_raster_height_capped_at_limit(self, monkeypatch):
        calls = self.fake_gs(monkeypatch, b"P4\n8 1\n\x80")
        h.pdf_to_escpos("/tmp/x.pdf", escpos.EscposSettings(),
                        page_height_pt=99999)
        assert f"-g{h.RASTER_WIDTH_PX}x{h.RASTER_HEIGHT_PX}" in calls[0]

    def test_without_page_height_uses_default(self, monkeypatch):
        calls = self.fake_gs(monkeypatch, b"P4\n8 1\n\x80")
        h.pdf_to_escpos("/tmp/x.pdf", escpos.EscposSettings())
        assert f"-g{h.RASTER_WIDTH_PX}x{h.RASTER_HEIGHT_PX}" in calls[0]

    def test_gs_failure_becomes_runtime_error(self, monkeypatch):
        self.fake_gs(monkeypatch, b"", returncode=1)
        with pytest.raises(RuntimeError, match="gs failed rc=1"):
            h.pdf_to_escpos("/tmp/x.pdf", escpos.EscposSettings())


# --------------------------------------------------------------- on_connect

class TestOnConnect:
    def test_success_subscribes_and_starts_availability(self, monkeypatch):
        monkeypatch.setattr(h, "AVAILABILITY_THREAD", None)
        spawned = []

        def fake_thread(client):
            spawned.append(client)
            return types.SimpleNamespace(is_alive=lambda: False)

        monkeypatch.setattr(h, "publish_availability", fake_thread)
        client = FakeClient()
        h.on_connect(client, None, None, 0)
        assert client.subscriptions == [(("printer/commands",), {"qos": 1})]
        assert spawned == [client]

    def test_reconnect_does_not_spawn_second_thread(self, monkeypatch):
        alive = types.SimpleNamespace(is_alive=lambda: True)
        monkeypatch.setattr(h, "AVAILABILITY_THREAD", alive)
        monkeypatch.setattr(
            h, "publish_availability",
            lambda client: pytest.fail("thread spawned twice"))
        client = FakeClient()
        h.on_connect(client, None, None, 0)
        assert len(client.subscriptions) == 1   # resubscribed, but no thread

    def test_failure_does_not_subscribe(self):
        client = FakeClient()
        h.on_connect(client, None, None, 4)
        assert client.subscriptions == []


# ---------------------------------------------------- DLE-EOT paper query
# query_paper is device-bound (os.open + select on PRINTER_DEVICE), so it is
# pinned at the wire level only: the request must be DLE EOT 4.
class TestPaperQuery:
    def test_query_bytes_are_dle_eot_4(self):
        assert h.PAPER_QUERY == b"\x10\x04\x04"
        assert h.POLL_TIMEOUT <= 0.5    # realtime request, short select wait


# ---------------------------------------------------------------- generate_pdf

class TestGeneratePdf:
    LINE_HEIGHT = 12            # points, hardcoded in generate_pdf
    MARGIN = 5                  # mm, hardcoded in generate_pdf

    def test_short_page_hits_height_floor(self, tmp_path):
        pdf = tmp_path / "short.pdf"
        page_height = h.generate_pdf("T", "hi", str(pdf))
        assert pdf.exists() and pdf.stat().st_size > 0
        assert page_height == pytest.approx(48 * h.mm + 1)

    def test_line_count_drives_height(self, tmp_path):
        pdf = tmp_path / "lines.pdf"
        message = "\n".join(["abcdefghij"] * 20)
        page_height = h.generate_pdf("T", message, str(pdf))
        # title + footer add two lines beyond the message itself
        assert page_height == pytest.approx(
            self.MARGIN * h.mm + (20 + 2) * self.LINE_HEIGHT)

    def test_blank_lines_kept_as_vertical_gaps(self, tmp_path):
        pdf_a = tmp_path / "a.pdf"
        pdf_b = tmp_path / "b.pdf"
        solid = "\n".join(["abcdefghij"] * 10)          # 10 text lines
        gapped = "\n".join(["abcdefghij", ""] * 5)      # 5 text + 5 blank
        height_solid = h.generate_pdf("T", solid, str(pdf_a))
        height_gapped = h.generate_pdf("T", gapped, str(pdf_b))
        assert height_gapped == pytest.approx(height_solid)

    def test_long_paragraph_wraps_into_more_lines(self, tmp_path):
        pdf = tmp_path / "wrap.pdf"
        page_height = h.generate_pdf(
            "T", " ".join(["abcdefghij"] * 40), str(pdf))
        # 40 words cannot fit on one line at the fixed 48 mm page width
        assert page_height > self.MARGIN * h.mm + 3 * self.LINE_HEIGHT

    def test_height_grows_monotonically_with_content(self, tmp_path):
        pdf = tmp_path / "m.pdf"
        heights = [h.generate_pdf("T", " ".join(["w"] * n), str(pdf))
                   for n in (1, 200, 800)]
        assert heights[0] < heights[1] < heights[2]
