"""Dropped/failed job reporting to MQTT, using a fake client."""

import json
import threading

import pytest

import printer_mqtt_handler as h


class FakeClient:
    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    def publish(self, topic, payload, qos=0, retain=False):
        if self.fail:
            raise RuntimeError("broker gone")
        self.published.append((topic, payload, qos, retain))


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    h.shutdown_worker()
    c = FakeClient()
    monkeypatch.setattr(h, "_CLIENT", c)
    yield c
    h.shutdown_worker()


def last_error(client):
    msgs = [p for p in client.published if p[0] == h.last_error_topic]
    return json.loads(msgs[-1][1])


def test_queue_full_drop_is_published(fake_client):
    gate = threading.Event()
    w = h.PrintWorker(maxsize=1, reporter=h.report_event)
    try:
        w.submit(gate.wait, label="busy")
        while w._queue.qsize():   # worker has picked up the first job
            pass
        w.submit(lambda: None, label="queued")
        assert w.submit(lambda: None, label="x" * 500) is False
    finally:
        gate.set()
        w.stop(timeout=5)
    body = last_error(fake_client)
    assert body["kind"] == "dropped"
    assert body["dropped_total"] == 1 and body["failed_total"] == 0
    assert "timestamp" in body
    assert len(body["message"]) < 100          # title is capped
    topics = {p[0]: p for p in fake_client.published}
    assert topics[h.dropped_total_topic][1] == "1"
    assert topics[h.dropped_total_topic][3] is True   # retained


def test_failed_job_is_published_without_payload_body(fake_client):
    def boom():
        raise OSError("device exploded")
    boom.label = "Receipt"
    w = h.PrintWorker(maxsize=2, reporter=h.report_event)
    try:
        w.submit(boom, label="Receipt")
        w.flush()
    finally:
        w.stop(timeout=5)
    body = last_error(fake_client)
    assert body["kind"] == "failed" and body["failed_total"] == 1
    assert "device exploded" in body["message"]
    assert {p[0]: p for p in fake_client.published}[h.failed_total_topic][1] == "1"


def test_on_message_failure_reports_title_only(fake_client, monkeypatch):
    def bad_print(title, message, printer_name):
        raise RuntimeError("gs failed")
    monkeypatch.setattr(h, "print_job", bad_print)

    class Msg:
        topic = "printer/commands"
        payload = json.dumps({"printer_name": "P", "title": "T" * 100,
                              "message": "SECRET-BODY"}).encode()
    h.on_message(None, None, Msg())
    h.flush_jobs()
    raw = fake_client.published[0][1]
    assert "SECRET-BODY" not in raw
    assert "T" * (h.ERROR_TITLE_CAP + 1) not in raw


def test_publish_errors_are_swallowed(monkeypatch, capsys):
    monkeypatch.setattr(h, "_CLIENT", FakeClient(fail=True))
    h.report_event("failed", "t", "boom", 0, 1)    # must not raise
    assert "Could not publish" in capsys.readouterr().out


def test_no_client_is_a_noop(monkeypatch):
    monkeypatch.setattr(h, "_CLIENT", None)
    h.report_event("dropped", "t", "", 1, 0)


def test_reporter_failure_does_not_break_worker():
    def bad_reporter(*a):
        raise RuntimeError("nope")
    ran = []
    w = h.PrintWorker(maxsize=2, reporter=bad_reporter)
    try:
        w.submit(lambda: 1 / 0)
        w.submit(lambda: ran.append(1))
        w.flush()
    finally:
        w.stop(timeout=5)
    assert ran == [1]


def test_on_connect_publishes_initial_counters(fake_client):
    class C(FakeClient):
        def subscribe(self, *a, **k):
            pass
    c = C()
    h._CLIENT = c
    old = h.AVAILABILITY_THREAD
    h.AVAILABILITY_THREAD = threading.Thread(target=lambda: None)
    h.AVAILABILITY_THREAD.start()
    try:
        h.AVAILABILITY_THREAD = type("T", (), {"is_alive": lambda s: True})()
        h.on_connect(c, None, None, 0)
    finally:
        h.AVAILABILITY_THREAD = old
    sent = {p[0]: p[1] for p in c.published}
    assert sent[h.dropped_total_topic] == "0"
    assert sent[h.failed_total_topic] == "0"
