"""Proton IMAP IDLE listener and the shared listener plumbing, on a scripted fake connection."""
import pytest

from hermes_auto import listening
from hermes_auto.cli import fast_sources
from hermes_auto.config import defaults
from hermes_auto.store import Store
from hermes_auto_proton import Source
from hermes_auto_proton.listener import ProtonListener, RawLines


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeLines:
    """Scripted server lines; None = a read timeout. Records what the client sent."""

    def __init__(self, script):
        self.script, self.sent = list(script), []

    def send(self, data):
        self.sent.append(data)

    def readline(self, timeout):
        if not self.script:
            raise ConnectionError("script ended")
        return self.script.pop(0)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


class StopAfter:
    """`stop.is_set()` turns true after `n` checks: the IDLE loop reads exactly `n` lines."""

    def __init__(self, n):
        self.n = n

    def is_set(self):
        self.n -= 1
        return self.n < 0


def test_idle_round_records_new_mail_and_ends_cleanly(tmp_path):
    clock, ran = Clock(1000.0), []
    listener = ProtonListener(tmp_path, {}, clock=clock, runner=lambda: ran.append(clock()) or (0, ""))
    lines = FakeLines([b"+ idling\r\n", b"* 12 EXISTS\r\n", b"* 3 RECENT\r\n", None, b"HAI1 OK IDLE done\r\n"])
    listener.idle_round(lines, stop=StopAfter(3), wait=0)
    assert lines.sent == [b"HAI1 IDLE\r\n", b"DONE\r\n"]
    state = listening.read_state(tmp_path, "proton")
    assert state["nudge_at"] == 1000.0 and state["events_since_start"] == 1
    listener.ticks.pump()
    assert ran == []  # quiet period
    clock.now += 15
    listener.ticks.pump()
    assert ran == [1015.0]


def test_idle_round_fails_loudly_on_refusal_and_bye(tmp_path):
    listener = ProtonListener(tmp_path, {}, runner=lambda: (0, ""))
    with pytest.raises(ConnectionError):
        listener.idle_round(FakeLines([b"HAI1 BAD no idle\r\n"]))
    with pytest.raises(ConnectionError):
        listener.idle_round(FakeLines([b"+ idling\r\n", b"* BYE shutting down\r\n"]))


def test_raw_lines_split_and_time_out():
    class Sock:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def settimeout(self, value):
            pass

        def recv(self, size):
            if not self.chunks:
                raise TimeoutError
            return self.chunks.pop(0)

        def sendall(self, data):
            pass

    lines = RawLines(Sock([b"* 1 EXI", b"STS\r\n* 2 EXISTS\r\n"]))
    assert lines.readline(1) == b"* 1 EXISTS\r\n"
    assert lines.readline(1) == b"* 2 EXISTS\r\n"
    assert lines.readline(0.01) is None


def test_proton_due_only_after_a_nudge(store, tmp_path):
    source = Source()
    assert not source.due({}, store, 0, 1000)
    listening.write_state(tmp_path, "proton", {"nudge_at": 990})
    assert not source.due({}, store, 0, 1000)  # within the quiet period
    assert source.due({}, store, 0, 1006)
    assert not source.due({}, store, 1001, 1006)  # already checked after it


def test_fast_sources_runs_proton_only_when_nudged(store, tmp_path):
    config = {**defaults(), "sources": {"proton": {"enabled": True}, "gmail": {"enabled": True}}}
    assert fast_sources(config, store, now=1000) == []
    listening.write_state(tmp_path, "proton", {"nudge_at": 900})
    assert fast_sources(config, store, now=1000) == ["proton"]


def test_tick_pump_retries_when_the_run_lock_is_busy():
    clock, calls = Clock(0.0), []

    def runner():
        calls.append(clock())
        return (1, "hermes-auto: Another hermes-auto operation is running") if len(calls) == 1 else (0, "")

    pump = listening.TickPump(runner, clock, logger=lambda message: None)
    pump.schedule(10)
    clock.now = 10
    pump.pump()
    clock.now = 20
    pump.pump()
    assert calls == [10]
    clock.now = 40
    pump.pump()
    assert calls == [10, 40]
