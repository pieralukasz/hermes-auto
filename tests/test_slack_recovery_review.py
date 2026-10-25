"""Slack listener recovery: what Socket Mode does not replay (downtime, reconnects, failed publishes)
must reach the sorter anyway, without reconnect storms and without losing a followed-thread hint."""
import json

from hermes_auto import listening
from test_slack import ME, NOW, Clock, FakeSlack, envelope


class Sink:
    """Relay stand-in: records accepted events; `fail` makes publishing raise like an unreachable relay."""

    def __init__(self):
        self.emitted, self.attempts, self.fail, self.during = [], 0, False, None

    def emit(self, source, hint=None, wake_after=None):
        self.attempts += 1
        if self.during:
            during, self.during = self.during, None
            during()
        if self.fail:
            raise RuntimeError("Relay did not accept the event")
        self.emitted.append(hint)

    def beat(self, sources, connected):
        pass

    def pump(self):
        pass


def workspace():
    fake = FakeSlack()
    fake.channel("C1", name="general")
    fake.post("C1", ME, "6.0", "my thread")
    return fake


def listener_at(home, clock, sink, fake=None):
    from hermes_auto_slack.listener import Listener
    return Listener(home, {}, http=fake or workspace(), clock=clock, sink=sink)


def thread_reply(ts, envelope_id="E1"):
    # A reply in a thread the user started: only a hint finds it before search indexes it.
    return envelope({"channel": "C1", "channel_type": "channel", "user": "UOZ", "ts": ts, "text": "secret",
                     "thread_ts": "6.0"}, envelope_id)


def pump_for(listener, clock, seconds):
    for _ in range(seconds):
        clock.now += 1
        listener.pump()


# ── Catch-up after (re)connects ─────────────────────────────────────────────

def test_hello_owes_one_hintless_catchup_nudge(tmp_path):
    clock, sink = Clock(NOW), Sink()
    listener = listener_at(tmp_path, clock, sink)
    listener.handle({"type": "hello"})
    listener.pump()
    assert sink.emitted == [None]
    pump_for(listener, clock, 120)
    assert sink.emitted == [None]


def test_catchup_owed_survives_a_failed_publish_and_a_restart(tmp_path):
    clock, sink = Clock(NOW), Sink()
    sink.fail = True
    listener_at(tmp_path, clock, sink).handle({"type": "hello"})
    listener_at(tmp_path, clock, sink).pump()
    assert sink.emitted == []
    # Restart two minutes later (well within any 600 s window); the owed catch-up is still delivered.
    clock.now += 120
    sink.fail = False
    restarted = listener_at(tmp_path, clock, sink)
    pump_for(restarted, clock, 300)
    assert sink.emitted == [None]


def test_reconnect_soon_after_a_catchup_owes_another(tmp_path):
    clock, sink = Clock(NOW), Sink()
    listener = listener_at(tmp_path, clock, sink)
    listener.handle({"type": "hello"})
    listener.pump()
    clock.now += 200  # disconnected meanwhile; events of that gap are never replayed
    listener.handle({"type": "hello"})
    pump_for(listener, clock, 120)
    assert sink.emitted == [None, None]


def test_reconnect_storm_is_rate_limited_but_the_last_catchup_arrives(tmp_path):
    clock, sink = Clock(NOW), Sink()
    listener = listener_at(tmp_path, clock, sink)
    for _ in range(60):  # a hello every 5 s for five minutes
        listener.handle({"type": "hello"})
        pump_for(listener, clock, 5)
    during_storm = len(sink.emitted)
    assert during_storm <= 6
    pump_for(listener, clock, 120)
    assert len(sink.emitted) == during_storm + 1
    pump_for(listener, clock, 600)
    assert len(sink.emitted) == during_storm + 1


def test_an_accepted_hint_after_hello_settles_the_catchup(tmp_path):
    clock, sink = Clock(NOW), Sink()
    listener = listener_at(tmp_path, clock, sink)
    listener.handle({"type": "hello"})
    assert listener.handle(thread_reply("8.0")) == {"envelope_id": "E1"}
    pump_for(listener, clock, 120)
    assert sink.emitted == [{"channel": "C1", "thread_ts": "6.0", "why": "thread", "ts": "8.0"}]
    assert "pending_hints" not in listening.read_state(tmp_path, "slack")


# ── Hints whose publish failed ──────────────────────────────────────────────

def test_failed_hint_publish_is_kept_ids_only_acked_and_delivered_after_restart(tmp_path):
    clock, sink = Clock(NOW), Sink()
    sink.fail = True
    listener = listener_at(tmp_path, clock, sink)
    assert listener.handle(thread_reply("8.0")) == {"envelope_id": "E1"}  # acked: Slack will not resend
    state = listening.read_state(tmp_path, "slack")
    hint = {"channel": "C1", "thread_ts": "6.0", "why": "thread", "ts": "8.0"}
    assert list(state["pending_hints"].values()) == [hint]
    assert "secret" not in json.dumps(state)
    sink.fail = False
    restarted = listener_at(tmp_path, clock, sink)
    pump_for(restarted, clock, 400)
    assert sink.emitted == [hint]  # the full hint, not only a nudge: search may not find the reply
    assert not listening.read_state(tmp_path, "slack").get("pending_hints")


def test_pending_hints_retry_with_backoff_until_accepted(tmp_path):
    clock, sink = Clock(NOW), Sink()
    sink.fail = True
    listener = listener_at(tmp_path, clock, sink)
    listener.handle(thread_reply("8.0"))
    pump_for(listener, clock, 600)
    assert 3 <= sink.attempts <= 8  # 5, 10, 20, 40, 80, 160 ... s apart, not every second
    sink.fail = False
    pump_for(listener, clock, 301)
    assert sink.emitted == [{"channel": "C1", "thread_ts": "6.0", "why": "thread", "ts": "8.0"}]
    attempts = sink.attempts
    pump_for(listener, clock, 120)
    assert sink.attempts == attempts


def test_new_events_while_hints_are_pending_queue_behind_them(tmp_path):
    clock, sink = Clock(NOW), Sink()
    sink.fail = True
    listener = listener_at(tmp_path, clock, sink)
    listener.handle(thread_reply("8.0"))
    attempts = sink.attempts
    assert listener.handle(thread_reply("8.1", "E2")) == {"envelope_id": "E2"}
    assert sink.attempts == attempts  # no blocking publish per event while the relay is down
    pending = listening.read_state(tmp_path, "slack")["pending_hints"]
    assert [h["ts"] for h in pending.values()] == ["8.1"]  # one conversation, newest activity


def test_hints_arriving_during_a_retry_are_not_lost(tmp_path):
    clock, sink = Clock(NOW), Sink()
    fake = workspace()
    fake.post("C1", ME, "9.0", "another thread of mine")
    sink.fail = True
    listener = listener_at(tmp_path, clock, sink, fake)
    listener.handle(thread_reply("8.0"))
    sink.fail = False
    locks = []

    def meanwhile():  # the websocket thread handles events while the pump publishes
        locks.append(any(lock.locked() for lock in listening._locks.values()))
        listener.handle(thread_reply("8.5", "E2"))
        listener.handle(envelope({"channel": "C1", "channel_type": "channel", "user": "UOZ", "ts": "9.1",
                                  "text": "x", "thread_ts": "9.0"}, "E3"))

    sink.during = meanwhile
    pump_for(listener, clock, 400)
    assert locks == [False]  # no state lock held across the network call
    delivered = {(h["thread_ts"], h["ts"]) for h in sink.emitted}
    assert {("6.0", "8.5"), ("9.0", "9.1")} <= delivered
    assert not listening.read_state(tmp_path, "slack").get("pending_hints")


class Socket:
    def __init__(self):
        self.sent, self.closed = [], False

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self):
        self.closed = True


def test_an_event_that_cannot_be_kept_drops_the_connection_unacked(tmp_path, monkeypatch):
    from hermes_auto_slack import listener as module
    clock, sink = Clock(NOW), Sink()
    sink.fail = True
    listener = listener_at(tmp_path, clock, sink)

    def disk_full(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(module.listening, "update_state", disk_full)
    socket = Socket()
    module.on_message(listener, socket, json.dumps(thread_reply("8.0")))
    # Not acked, so Slack redelivers; the reconnect's hello owes a catch-up check as well.
    assert socket.sent == [] and socket.closed


def test_on_message_acks_a_handled_event(tmp_path):
    from hermes_auto_slack import listener as module
    socket = Socket()
    module.on_message(listener_at(tmp_path, Clock(NOW), Sink()), socket, json.dumps(thread_reply("8.0")))
    assert socket.sent == [{"envelope_id": "E1"}] and not socket.closed
