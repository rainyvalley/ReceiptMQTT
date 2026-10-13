"""Tests for the print worker: jobs leave paho's network thread."""

import json
import threading

import pytest

import printer_mqtt_handler as h


class Msg:
    topic = "printer/commands"

    def __init__(self, n):
        self.payload = json.dumps({"printer_name": "P", "title": f"t{n}",
                                   "message": f"m{n}"}).encode()


@pytest.fixture
def worker():
    w = h.PrintWorker(maxsize=3)
    yield w
    w.stop(timeout=5)


def test_jobs_run_in_order_one_at_a_time(worker):
    order, active, overlap = [], [0], []
    lock = threading.Lock()

    def job(i):
        with lock:
            active[0] += 1
            if active[0] > 1:
                overlap.append(i)
        order.append(i)
        with lock:
            active[0] -= 1

    for i in range(3):
        assert worker.submit(lambda i=i: job(i))
    worker.flush()
    assert order == [0, 1, 2]
    assert overlap == []


def test_full_queue_drops_newest_without_blocking(worker, capsys):
    started, release = threading.Event(), threading.Event()
    ran = []

    def blocker():
        started.set()
        release.wait(10)

    assert worker.submit(blocker)
    assert started.wait(5)           # worker busy; queue now empty
    for i in range(3):               # fills the 3 slots
        assert worker.submit(lambda i=i: ran.append(i))

    result = []
    t = threading.Thread(target=lambda: result.append(worker.submit(lambda: ran.append("x"))))
    t.start()
    t.join(2)
    assert not t.is_alive(), "submit blocked on a full queue"
    assert result == [False]
    assert worker.dropped == 1
    assert "dropped newest" in capsys.readouterr().out

    release.set()
    worker.flush()
    assert ran == [0, 1, 2]          # the newest ("x") was the one dropped


def test_worker_survives_job_exception(worker, capsys):
    ran = []

    def boom():
        raise RuntimeError("gs exploded")

    worker.submit(boom)
    worker.submit(lambda: ran.append("after"))
    worker.flush()
    assert ran == ["after"]
    assert "gs exploded" in capsys.readouterr().out


def test_stop_joins_idle_worker_and_discards_queue():
    w = h.PrintWorker(maxsize=3)
    started, release = threading.Event(), threading.Event()
    ran = []
    w.submit(lambda: (started.set(), release.wait(10)))
    assert started.wait(5)
    w.submit(lambda: ran.append("queued"))
    release.set()
    assert w.stop(timeout=5) is True
    assert not w._thread.is_alive()
    assert ran in ([], ["queued"])   # raced with release; must not hang


def test_stop_idle_worker_returns_promptly(worker):
    worker.submit(lambda: None)
    worker.flush()
    assert worker.stop(timeout=5) is True
    assert not worker._thread.is_alive()


def test_stop_times_out_on_stuck_job():
    w = h.PrintWorker(maxsize=1)
    started, release = threading.Event(), threading.Event()
    w.submit(lambda: (started.set(), release.wait(10)))
    assert started.wait(5)
    assert w.stop(timeout=0.2) is False   # daemon thread: exit not blocked
    release.set()
    w._thread.join(5)
    assert not w._thread.is_alive()


def test_on_message_runs_job_off_calling_thread(monkeypatch):
    seen = {}
    done = threading.Event()

    def fake_print_job(title, message, printer_name):
        seen["thread"] = threading.current_thread()
        done.set()

    monkeypatch.setattr(h, "print_job", fake_print_job)
    h.on_message(None, None, Msg(1))
    assert done.wait(5)
    assert seen["thread"] is not threading.current_thread()
    assert seen["thread"].daemon


def test_on_message_does_not_block_while_printing(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(h, "print_job", lambda *a: release.wait(10))
    t = threading.Thread(target=lambda: [h.on_message(None, None, Msg(i))
                                         for i in range(h.PRINT_QUEUE_SIZE + 5)])
    t.start()
    t.join(3)
    try:
        assert not t.is_alive(), "on_message blocked the calling thread"
        assert h.get_worker().dropped >= 1
    finally:
        release.set()
        h.flush_jobs()   # queued jobs must finish while print_job is patched


@pytest.mark.parametrize("raw,expected", [
    (None, 16), ("", 16), ("abc", 16), ("0", 16), ("-3", 16), ("4", 4)])
def test_env_int(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("X_TEST", raising=False)
    else:
        monkeypatch.setenv("X_TEST", raw)
    assert h._env_int("X_TEST", 16) == expected
