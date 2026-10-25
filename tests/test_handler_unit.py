"""Unit tests for printer_mqtt_handler: MQTT intake, device retries, PDF maths.

Offline throughout: the send path is exercised via monkeypatched writes and
ghostscript is replaced by a fake subprocess returning canned PBM raster.
"""

import errno
import json
import subprocess
import threading
import time
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

    def publish(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def fresh_worker():
    """Each test gets its own print worker (and none leaks out)."""
    h.shutdown_worker()
    yield
    h.shutdown_worker()


@pytest.fixture
def recorded_jobs(monkeypatch):
    calls = []

    def fake_print_job(title, message):
        calls.append((title, message))

    monkeypatch.setattr(h, "print_job", fake_print_job)
    return calls


# ------------------------------------------------------------------ on_message

class TestOnMessage:
    def test_good_payload_dispatches_print_job(self, recorded_jobs):
        raw = json.dumps({"printer_name": "ReceiptPrinter",
                          "title": "Alert", "message": "hello"}).encode()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert recorded_jobs == [("Alert", "hello")]

    def test_retained_message_not_printed(self, recorded_jobs):
        raw = json.dumps({"printer_name": "P", "message": "old"}).encode()
        msg = FakeMsg(raw)
        msg.retain = True
        h.on_message(None, None, msg)
        h.flush_jobs()
        assert recorded_jobs == []

    def test_overlong_first_word_adds_no_blank_line(self, tmp_path):
        seen = []
        orig = h.canvas.Canvas.drawString
        h.canvas.Canvas.drawString = lambda self, x, y, t, *a, **k: seen.append(t)
        try:
            h.generate_pdf("T", "W" * 80 + " tail", str(tmp_path / "a.pdf"))
        finally:
            h.canvas.Canvas.drawString = orig
        assert "" not in seen

    def test_defaults_for_missing_title_and_message(self, recorded_jobs):
        raw = json.dumps({"printer_name": "P"}).encode()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert recorded_jobs == [("Print Job", "No message provided")]

    def test_bad_json_never_reaches_print_job(self, recorded_jobs, capsys):
        h.on_message(None, None, FakeMsg(b"{not json"))
        h.flush_jobs()
        assert recorded_jobs == []
        assert "Error decoding JSON" in capsys.readouterr().out

    def test_missing_printer_name_rejected(self, recorded_jobs, capsys):
        raw = json.dumps({"title": "T", "message": "m"}).encode()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert recorded_jobs == []
        assert "Missing 'printer_name'" in capsys.readouterr().out

    def test_empty_printer_name_rejected(self, recorded_jobs):
        raw = json.dumps({"printer_name": ""}).encode()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert recorded_jobs == []

    def test_oversized_message_truncated(self, recorded_jobs):
        raw = json.dumps({"printer_name": "P",
                          "message": "x" * (h.MAX_MESSAGE_CHARS + 5000)}).encode()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert len(recorded_jobs) == 1
        assert len(recorded_jobs[0][1]) == h.MAX_MESSAGE_CHARS

    @pytest.mark.parametrize("title,message", [
        (123, 4.5), (None, None), (["a", "b"], {"k": "v"}), (True, False)])
    def test_non_string_title_and_message_coerced(self, recorded_jobs, title, message):
        raw = json.dumps({"printer_name": "P", "title": title,
                          "message": message}).encode()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert recorded_jobs == [(str(title), str(message))]

    def test_long_title_capped(self, recorded_jobs):
        raw = json.dumps({"printer_name": "P", "title": "T" * 1000,
                          "message": "m"}).encode()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert recorded_jobs[0][0] == "T" * h.MAX_TITLE_CHARS

    def test_non_string_message_truncated_after_coercion(self, recorded_jobs):
        raw = json.dumps({"printer_name": "P",
                          "message": [1] * h.MAX_MESSAGE_CHARS}).encode()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert len(recorded_jobs[0][1]) == h.MAX_MESSAGE_CHARS

    def test_invalid_utf8_bytes_dropped_not_replaced(self, recorded_jobs):
        # one raw 0xFF byte inside the JSON string; decode(errors="ignore")
        # must drop it, not turn it into U+FFFD (which the WinAnsi fonts in
        # reportlab cannot encode and would silently kill the receipt)
        raw = b'{"printer_name": "P", "message": "ok \xff done"}'
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        assert len(recorded_jobs) == 1
        message = recorded_jobs[0][1]
        assert "�" not in message
        assert message == "ok  done"           # byte dropped, not replaced

    def test_print_job_failure_is_caught(self, monkeypatch):
        def boom(title, message):
            raise RuntimeError("gs exploded")
        # on_message resolves print_job from the module namespace at call
        # time, so patch the real one this time instead of the fixture.
        monkeypatch.setattr(h, "print_job", boom)
        raw = json.dumps({"printer_name": "P"}).encode()
        h.on_message(None, None, FakeMsg(raw))   # must not raise
        h.flush_jobs()


# ----------------------------------------------------------- send_raw_to_printer

class TestSendRawToPrinter:
    PAYLOAD = b"\x1b@\x1d\x76\x30"

    def test_direct_success(self, monkeypatch):
        written = {}
        monkeypatch.setattr(h, "_write_device",
                            lambda path, data: written.update(path=path,
                                                              data=data) or len(data))
        monkeypatch.setattr(h, "printer_device", "/tmp/dev")
        h.send_raw_to_printer(self.PAYLOAD)
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
        h.send_raw_to_printer(self.PAYLOAD)
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
            h.send_raw_to_printer(self.PAYLOAD)
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
            h.send_raw_to_printer(self.PAYLOAD)
        assert exc.value.errno == errno.EIO
        assert len(attempts) == 1

    def test_no_device_configured_raises(self, monkeypatch):
        monkeypatch.setattr(h, "printer_device", "")
        with pytest.raises(RuntimeError, match="PRINTER_DEVICE"):
            h.send_raw_to_printer(self.PAYLOAD)

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

    def test_gs_call_is_time_bounded(self, monkeypatch):
        seen = {}

        def run(cmd, *a, **k):
            seen.update(k)
            raise subprocess.TimeoutExpired(cmd, k.get("timeout"))

        monkeypatch.setattr(h.subprocess, "run", run)
        with pytest.raises(RuntimeError, match="gs timed out"):
            h.pdf_to_escpos("/tmp/x.pdf", escpos.EscposSettings())
        assert seen["timeout"] == h.GS_TIMEOUT


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


class TestOnConnectAvailability:
    def test_publishes_online_immediately(self, monkeypatch):
        published = []
        client = FakeClient()
        client.publish = lambda *a, **k: published.append((a, k))
        monkeypatch.setattr(h, "AVAILABILITY_THREAD",
                            types.SimpleNamespace(is_alive=lambda: True))
        monkeypatch.setattr(h, "LAST_AVAILABILITY", "online")
        h.on_connect(client, None, None, 0)
        assert (("printer/availability", "online"),
                {"qos": 1, "retain": True}) in published

    def test_no_publish_after_shutdown(self, monkeypatch):
        published = []
        client = FakeClient()
        client.publish = lambda *a, **k: published.append(a)
        monkeypatch.setattr(h, "AVAILABILITY_THREAD",
                            types.SimpleNamespace(is_alive=lambda: True))
        h.SHUTDOWN.set()
        try:
            h.on_connect(client, None, None, 0)
        finally:
            h.SHUTDOWN.clear()
        assert not any(a[0] == "printer/availability" for a in published)


class TestConnectWithRetry:
    def test_retries_with_doubling_backoff_until_connected(self):
        attempts, waits = [], []

        class C:
            def connect(self, host, port, keepalive):
                attempts.append((host, port, keepalive))
                if len(attempts) < 4:
                    raise ConnectionRefusedError("down")

        assert h.connect_with_retry(C(), "b", 1883, 60,
                                    stop=threading.Event(),
                                    sleep=waits.append) is True
        assert len(attempts) == 4
        assert waits == [1.0, 2.0, 4.0]

    def test_backoff_is_capped(self):
        waits = []

        class C:
            n = 0

            def connect(self, *a):
                C.n += 1
                if C.n < 10:
                    raise OSError("down")

        h.connect_with_retry(C(), "b", 1, stop=threading.Event(),
                             sleep=waits.append)
        assert max(waits) == h.CONNECT_BACKOFF_MAX

    def test_shutdown_stops_retrying(self):
        stop = threading.Event()

        class C:
            def connect(self, *a):
                raise OSError("down")

        assert h.connect_with_retry(C(), "b", 1, stop=stop,
                                    sleep=lambda d: stop.set()) is False


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

    def test_page_width_follows_the_print_area(self, tmp_path, monkeypatch):
        # 80 mm paper prints 72 mm; the page must not be laid out to the roll
        # width or the right margin lands outside the raster.
        monkeypatch.setattr(h.escpos, "PRINT_AREA_MM", 72)
        page_height = h.generate_pdf("T", "hi", str(tmp_path / "w.pdf"))
        assert page_height == pytest.approx(72 * h.mm + 1)

    def test_line_count_drives_height(self, tmp_path):
        pdf = tmp_path / "lines.pdf"
        message = "\n".join(["abcdefghij"] * 12)
        page_height = h.generate_pdf("T", message, str(pdf))
        # title + footer add two lines beyond the message itself
        assert page_height == pytest.approx(
            self.MARGIN * h.mm + (12 + 2) * self.LINE_HEIGHT)

    def test_blank_lines_kept_as_vertical_gaps(self, tmp_path):
        pdf_a = tmp_path / "a.pdf"
        pdf_b = tmp_path / "b.pdf"
        solid = "\n".join(["abcdefghij"] * 10)          # 10 text lines
        gapped = "\n".join(["abcdefghij", ""] * 5)      # 5 text + 5 blank
        height_solid = h.generate_pdf("T", solid, str(pdf_a))
        height_gapped = h.generate_pdf("T", gapped, str(pdf_b))
        assert height_gapped == pytest.approx(height_solid)

    def test_overlong_word_is_hard_broken_to_content_width(self, tmp_path):
        content_width = (48 - 2 * self.MARGIN) * h.mm
        word = "W" * 60
        chunks = h.break_word(word, content_width)
        assert "".join(chunks) == word and len(chunks) > 1
        assert all(h.pdfmetrics.stringWidth(c, "Helvetica", 10)
                   <= content_width for c in chunks)
        # and generate_pdf allots one line per chunk
        height = h.generate_pdf("T", word, str(tmp_path / "w.pdf"))
        expect = self.MARGIN * h.mm + (len(chunks) + 2) * self.LINE_HEIGHT
        assert height == pytest.approx(max(expect, 48 * h.mm + 1))

    def test_long_paragraph_wraps_into_more_lines(self, tmp_path):
        pdf = tmp_path / "wrap.pdf"
        page_height = h.generate_pdf(
            "T", " ".join(["abcdefghij"] * 40), str(pdf))
        # 40 words cannot fit on one line at the fixed 48 mm page width
        assert page_height > self.MARGIN * h.mm + 3 * self.LINE_HEIGHT

    def test_height_grows_monotonically_with_content(self, tmp_path):
        pdf = tmp_path / "m.pdf"
        heights = [h.generate_pdf("T", " ".join(["w"] * n), str(pdf))
                   for n in (1, 150, 800)]
        assert heights[0] < heights[1] <= heights[2]


# ------------------------------------------------------------------ shutdown

class RecordingClient:
    """Records the order of publish/disconnect calls and the calling thread."""

    def __init__(self, publish_error=None):
        self.events = []
        self.threads = []
        self.publish_error = publish_error

    def publish(self, topic, payload, qos=0, retain=False):
        self.events.append(("publish", topic, payload, qos, retain))
        self.threads.append(threading.current_thread())
        client = self

        class Info:
            def wait_for_publish(self, timeout=None):
                client.events.append(("wait", timeout))
                if client.publish_error:
                    raise client.publish_error

        return Info()

    def disconnect(self):
        self.events.append(("disconnect",))
        self.threads.append(threading.current_thread())


@pytest.fixture
def shutdown_state():
    h.SHUTDOWN.clear()
    yield
    h.SHUTDOWN.clear()


class TestGracefulShutdown:
    def test_offline_published_and_awaited_before_disconnect(self, shutdown_state):
        c = RecordingClient()
        h.graceful_shutdown(c)
        assert c.events == [
            ("publish", h.availability_topic, "offline", 1, True),
            ("wait", 2),
            ("disconnect",),
        ]

    def test_disconnect_still_happens_if_publish_fails(self, shutdown_state, capsys):
        c = RecordingClient(publish_error=RuntimeError("not connected"))
        h.graceful_shutdown(c)
        assert c.events[-1] == ("disconnect",)
        assert "Could not publish offline" in capsys.readouterr().out

    def test_only_availability_topic_is_touched(self, shutdown_state):
        c = RecordingClient()
        h.graceful_shutdown(c)
        topics = {e[1] for e in c.events if e[0] == "publish"}
        assert topics == {h.availability_topic}

    def test_poller_does_not_republish_online_after_shutdown(
            self, shutdown_state, monkeypatch):
        h.SHUTDOWN.set()
        c = RecordingClient()
        monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="printer P is idle", stderr=""))
        monkeypatch.setattr(h, "query_paper", lambda: None)
        t = h.publish_availability(c, interval=0.01)
        t.join(2)
        assert not t.is_alive()
        assert c.events == []

    def test_signal_handler_only_sets_event_work_runs_on_helper_thread(
            self, shutdown_state, monkeypatch):
        installed = {}
        monkeypatch.setattr(h.signal, "signal",
                            lambda sig, fn: installed.setdefault(sig, fn))
        c = RecordingClient()
        stop = h.install_term_handler(c)
        handler = installed[h.signal.SIGTERM]

        handler(h.signal.SIGTERM, None)           # as the main thread would
        deadline = time.time() + 5
        while ("disconnect",) not in c.events and time.time() < deadline:
            time.sleep(0.01)
        assert stop.is_set()
        assert c.events[0][0] == "publish"
        assert c.events[-1] == ("disconnect",)
        assert threading.current_thread() not in c.threads


# ---------------------------------------------------------- audit additions

class TestGeneratePdfLineCap:
    def test_huge_message_makes_a_bounded_page(self, tmp_path):
        height = h.generate_pdf("T", "\n" * 60000, str(tmp_path / "big.pdf"))
        # still taller than the raster cap (so the truncation warning fires)
        # but nowhere near 60000 lines
        cap_pt = h.RASTER_HEIGHT_PX * 72 / h.RASTER_DPI_Y
        assert height > cap_pt
        assert height < cap_pt + 12 * 12

    def test_truncation_is_still_warned(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            stdout=escpos.selftest_pbm(), returncode=0))
        pdf = tmp_path / "t.pdf"
        height = h.generate_pdf("T", "\n".join(["x"] * 500), str(pdf))
        h.pdf_to_escpos(str(pdf), escpos.EscposSettings(), height)
        assert "capped to RASTER_HEIGHT_PX" in capsys.readouterr().out


class TestRejectedMessagesPublished:
    @pytest.fixture
    def events(self, monkeypatch):
        seen = []
        h.shutdown_worker()
        monkeypatch.setattr(h, "report_event",
                            lambda *a: seen.append(a))
        yield seen
        h.shutdown_worker()

    @pytest.mark.parametrize("raw,detail", [
        (b"{not json", "not valid JSON"),
        (b"[1, 2]", "not a JSON object"),
        (b'"text"', "not a JSON object"),
        (b'{"title": "T"}', "missing printer_name"),
    ])
    def test_rejection_is_reported_and_counted(self, events, raw, detail):
        h.on_message(None, None, FakeMsg(raw))
        assert len(events) == 1
        kind, label, text, dropped, failed = events[0]
        assert kind == "failed" and detail in text
        assert failed == 1 and dropped == 0

    def test_message_body_not_published(self, events):
        h.on_message(None, None, FakeMsg(b'{"message": "SECRET-BODY"}'))
        assert "SECRET-BODY" not in repr(events)

    def test_title_newlines_collapsed(self, events, monkeypatch):
        jobs = []
        monkeypatch.setattr(h, "print_job", lambda t, m: jobs.append(t))
        h.on_message(None, None, FakeMsg(
            b'{"printer_name": "P", "title": "a\\nb\\tc", "message": "m"}'))
        h.flush_jobs()
        assert jobs == ["a b c"]


class TestRateLimit:
    def test_over_limit_dropped_and_reported(self):
        seen = []
        w = h.PrintWorker(100, reporter=lambda *a: seen.append(a),
                          max_per_minute=3)
        done = []
        for i in range(5):
            w.submit(lambda: done.append(1), label=f"j{i}")
        w.flush()
        assert len(done) == 3
        assert w.dropped == 2
        assert [e[0] for e in seen] == ["dropped", "dropped"]
        assert "rate limit" in repr(seen[0])
        w.stop(2)

    def test_window_slides(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(h.time, "monotonic", lambda: now[0])
        w = h.PrintWorker(100, max_per_minute=1)
        assert w.submit(lambda: None) is True
        assert w.submit(lambda: None) is False
        now[0] += 61
        assert w.submit(lambda: None) is True
        w.flush()
        w.stop(2)

    def test_zero_means_unlimited(self):
        w = h.PrintWorker(100, max_per_minute=0)
        assert all(w.submit(lambda: None) for _ in range(50))
        w.flush()
        w.stop(2)


class TestStartupWarnings:
    def test_anonymous_warns(self):
        out = h.startup_warnings(user=None, pw=None, env={})
        assert any("without a username" in w for w in out)

    def test_credentials_present_no_anonymous_warning(self):
        out = h.startup_warnings(user="u", pw="p", env={"MQTT_TLS": "1"})
        assert out == []

    def test_placeholder_username_warns(self):
        out = h.startup_warnings(user="your-username", pw="p",
                                 env={"MQTT_TLS": "1"})
        assert any("placeholder" in w for w in out)

    def test_plain_mqtt_to_remote_broker_warns(self, monkeypatch):
        monkeypatch.setattr(h, "broker", "192.0.2.10")
        assert any("MQTT_TLS is off" in w
                   for w in h.startup_warnings("u", "p", env={}))

    def test_plain_mqtt_to_localhost_is_quiet(self, monkeypatch):
        monkeypatch.setattr(h, "broker", "localhost")
        assert h.startup_warnings("u", "p", env={}) == []


class TestEnvRobustness:
    def test_empty_topic_env_uses_default(self, monkeypatch):
        import importlib
        monkeypatch.setenv("MQTT_TOPIC", "")
        monkeypatch.setenv("MQTT_AVAILABILITY_TOPIC", "")
        monkeypatch.setenv("RASTER_DPI_X", "junk")
        monkeypatch.setenv("PRINTER_DEVICE", "")
        monkeypatch.setenv("PAPER_DEVICE", "/dev/usb/lp7")
        try:
            m = importlib.reload(h)
            assert m.topic == "printer/commands"
            assert m.availability_topic == "printer/availability"
            assert m.RASTER_DPI_X == 203
            assert m.printer_device == "/dev/usb/lp7"
        finally:
            monkeypatch.undo()
            importlib.reload(h)

    def test_availability_topic_is_configurable(self, monkeypatch):
        # A second printer on the same broker needs its own retained
        # availability topic, or the two instances overwrite each other.
        import importlib
        monkeypatch.setenv("MQTT_AVAILABILITY_TOPIC", "kitchen/availability")
        try:
            m = importlib.reload(h)
            assert m.availability_topic == "kitchen/availability"
        finally:
            monkeypatch.undo()
            importlib.reload(h)

    def test_escpos_junk_value_logged(self, capsys):
        s = escpos.EscposSettings.from_env({"CUTTING": "yes"})
        assert s.cutting == 1
        assert "Ignoring invalid CUTTING" in capsys.readouterr().err


class TestAvailabilityPoll:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="printer P is idle", stderr=""))
        monkeypatch.setattr(h, "query_paper", lambda: None)
        h.SHUTDOWN.clear()
        yield
        h.SHUTDOWN.set()      # lets any poller thread exit
        time.sleep(0.05)
        h.SHUTDOWN.clear()

    def _first_publish(self, monkeypatch, device_exists):
        monkeypatch.setattr(h.os.path, "exists", lambda p: device_exists)
        c = RecordingClient()
        h.publish_availability(c, interval=60)
        deadline = time.time() + 3
        while not c.events and time.time() < deadline:
            time.sleep(0.01)
        return c.events[0][2]

    def test_missing_device_node_is_offline(self, monkeypatch):
        assert self._first_publish(monkeypatch, False) == "offline"

    def test_present_device_node_is_online(self, monkeypatch):
        assert self._first_publish(monkeypatch, True) == "online"

    def test_publish_error_does_not_end_the_thread(self, monkeypatch, capsys):
        monkeypatch.setattr(h.os.path, "exists", lambda p: True)
        attempts = []

        class Failing:
            def publish(self, *a, **k):
                attempts.append(1)
                raise ValueError("bad topic")

        t = h.publish_availability(Failing(), interval=0.01)
        deadline = time.time() + 3
        while len(attempts) < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert len(attempts) >= 3 and t.is_alive()
        assert "Availability poll failed" in capsys.readouterr().out


class TestFloodStaysResponsive:
    def test_flood_of_huge_messages_is_bounded_and_fast(self, monkeypatch):
        """64K-newline messages: intake returns at once, the page is small,
        the queue and per-minute limits drop the excess and report it."""
        h.shutdown_worker()
        events = []
        monkeypatch.setattr(h, "report_event", lambda *a: events.append(a))
        monkeypatch.setattr(h, "MAX_JOBS_PER_MINUTE", 5)
        monkeypatch.setattr(h, "PRINT_QUEUE_SIZE", 3)
        heights = []

        def fake_pipeline(title, message):
            import os, tempfile
            fd, path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            try:
                heights.append(h.generate_pdf(title, message, path))
            finally:
                os.unlink(path)

        monkeypatch.setattr(h, "print_job", fake_pipeline)
        raw = json.dumps({"printer_name": "P",
                          "message": "\n" * 70000}).encode()
        start = time.time()
        for _ in range(50):
            h.on_message(None, None, FakeMsg(raw))
        assert time.time() - start < 5          # network thread not blocked
        h.flush_jobs()
        assert 1 <= len(heights) <= 5            # per-minute limit held
        cap_pt = h.RASTER_HEIGHT_PX * 72 / h.RASTER_DPI_Y
        assert all(x < cap_pt + 200 for x in heights)
        assert sum(1 for e in events if e[0] == "dropped") >= 45
        # worker still alive and accepting after the flood
        monkeypatch.setattr(h, "MAX_JOBS_PER_MINUTE", 0)
        h.shutdown_worker()
        h.on_message(None, None, FakeMsg(raw))
        h.flush_jobs()
        h.shutdown_worker()


# ---------------------------------------------------------- HA discovery

class TestDiscovery:
    def _messages(self, monkeypatch):
        monkeypatch.setattr(h, "topic", "kitchen/commands")
        monkeypatch.setattr(h, "HA_DISCOVERY_PREFIX", "homeassistant")
        return dict(h.discovery_messages())

    def test_every_published_value_has_an_entity(self, monkeypatch):
        messages = self._messages(monkeypatch)
        assert len(messages) == 6
        node = h.discovery_node()
        assert node == "kitchen_commands"
        for key in ("availability", "paper", "paper_low", "last_error",
                    "dropped_total", "failed_total"):
            assert any(t.endswith(f"/{key}/config") for t in messages)
        for payload in messages.values():
            assert payload["unique_id"].startswith(f"receiptmqtt_{node}_")
            assert payload["device"]["identifiers"] == [f"receiptmqtt_{node}"]

    def test_state_topics_match_the_app_topics(self, monkeypatch):
        messages = self._messages(monkeypatch)
        by_suffix = {t.rsplit("/", 2)[-2]: p for t, p in messages.items()}
        assert by_suffix["paper"]["state_topic"] == h.paper_topic
        assert by_suffix["paper_low"]["state_topic"] == h.paper_low_topic
        assert by_suffix["last_error"]["state_topic"] == h.last_error_topic
        assert by_suffix["dropped_total"]["state_topic"] == h.dropped_total_topic
        assert by_suffix["failed_total"]["state_topic"] == h.failed_total_topic
        assert by_suffix["availability"]["state_topic"] == h.availability_topic

    def test_availability_entity_can_report_offline(self, monkeypatch):
        messages = self._messages(monkeypatch)
        payload = next(p for t, p in messages.items()
                       if t.endswith("/availability/config"))
        # Using itself as availability would hide "offline" behind
        # Home Assistant's own "unavailable".
        assert "availability" not in payload
        assert payload["payload_on"] == "online"
        assert payload["payload_off"] == "offline"

    def test_connect_publishes_retained_configs(self, monkeypatch):
        published = []
        client = FakeClient()
        client.publish = lambda *a, **k: published.append((a, k))
        monkeypatch.setattr(h, "AVAILABILITY_THREAD",
                            types.SimpleNamespace(is_alive=lambda: True))
        h.on_connect(client, None, None, 0)
        configs = [(a, k) for a, k in published
                   if a[0].startswith(h.HA_DISCOVERY_PREFIX + "/")]
        assert len(configs) == 6
        assert all(k.get("retain") is True for _a, k in configs)

    def test_discovery_can_be_disabled(self, monkeypatch):
        published = []
        client = FakeClient()
        client.publish = lambda *a, **k: published.append(a)
        monkeypatch.setattr(h, "HA_DISCOVERY", False)
        monkeypatch.setattr(h, "AVAILABILITY_THREAD",
                            types.SimpleNamespace(is_alive=lambda: True))
        h.on_connect(client, None, None, 0)
        assert not any(a[0].startswith("homeassistant/") for a in published)


class TestSettingsReload:
    def test_panel_values_update_topic_and_paper(self, monkeypatch, tmp_path):
        import importlib
        path = tmp_path / "settings.json"
        monkeypatch.setattr(h.settings_store, "SETTINGS_FILE", str(path))
        for key in ("MQTT_TOPIC", "MQTT_AVAILABILITY_TOPIC", "PAPER_WIDTH_MM",
                    "PRINTER_DEVICE"):
            monkeypatch.delenv(key, raising=False)
        h.settings_store.save({"MQTT_TOPIC": "kitchen/commands",
                               "MQTT_AVAILABILITY_TOPIC": "kitchen/availability",
                               "PAPER_WIDTH_MM": "58",
                               "PRINTER_DEVICE": "/dev/usb/lp9"}, path)
        try:
            changed = h.apply_runtime_settings(env={})
            assert {"MQTT_TOPIC", "MQTT_AVAILABILITY_TOPIC", "PAPER_WIDTH_MM",
                    "PRINTER_DEVICE"} <= set(changed)
            assert h.topic == "kitchen/commands"
            assert h.availability_topic == "kitchen/availability"
            assert h.printer_device == "/dev/usb/lp9"
            assert escpos.PAPER_WIDTH_MM == 58
            assert h.discovery_node() == "kitchen_commands"
        finally:
            monkeypatch.undo()
            importlib.reload(h)

    def test_environment_still_wins_on_reload(self, monkeypatch, tmp_path):
        import importlib
        path = tmp_path / "settings.json"
        monkeypatch.setattr(h.settings_store, "SETTINGS_FILE", str(path))
        h.settings_store.save({"MQTT_TOPIC": "panel/commands"}, path)
        try:
            h.apply_runtime_settings(env={"MQTT_TOPIC": "env/commands"})
            assert h.topic == "env/commands"
        finally:
            monkeypatch.undo()
            importlib.reload(h)

    def test_topic_change_clears_stale_retained_topics(self, monkeypatch,
                                                       tmp_path):
        import importlib
        path = tmp_path / "settings.json"
        monkeypatch.setattr(h.settings_store, "SETTINGS_FILE", str(path))
        monkeypatch.setattr(h, "topic", "printer/commands")
        monkeypatch.setattr(h, "availability_topic", "printer/availability")
        published = []

        class Client:
            def publish(self, topic, payload, qos=0, retain=False):
                published.append((topic, payload, retain))

        try:
            h.settings_store.save({"MQTT_TOPIC": "kitchen/commands",
                                   "MQTT_AVAILABILITY_TOPIC": "kitchen/availability"},
                                  path)
            h.apply_runtime_settings(env={}, client=Client())
            cleared = [t for t, payload, retain in published if retain]
            assert "printer/availability" in cleared
            assert "homeassistant/binary_sensor/receiptmqtt_printer_commands/paper/config" in cleared
            assert "homeassistant/sensor/receiptmqtt_printer_commands/last_error/config" in cleared
            assert all(payload == b"" for topic, payload, retain in published
                       if retain and "printer_commands" in topic)
        finally:
            monkeypatch.undo()
            importlib.reload(h)
