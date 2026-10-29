"""Push transport and scheduling under concurrency, failures, resets and dead listeners (no network)."""
import json
import time
from pathlib import Path

import pytest

from hermes_auto import cli, listening
from hermes_auto.cli import fast_sources, main
from hermes_auto.config import defaults, private_json
from hermes_auto.store import Store


class Nudged:
    """A synthetic push source: due when its listener file has a nudge newer than the last check."""

    calls = []
    behaviour = None

    def due(self, settings, store, last_poll, now):
        return listening.nudged(Path(store.path).parent, "fake", last_poll, now, 0)

    def poll(self, config, store):
        Nudged.calls.append(time.time())
        if Nudged.behaviour:
            Nudged.behaviour(Path(store.path).parent)


@pytest.fixture
def home(tmp_path, monkeypatch):
    config = {**defaults(), "desktop_only": False, "poll_sources": False,
              "sources": {"fake": {"enabled": True}}}
    private_json(tmp_path / "config.json", config)
    Store(tmp_path, initialize=True).close()
    store = Store(tmp_path)
    store.set_meta("last_poll", time.time())  # no full check due
    store.close()
    Nudged.calls, Nudged.behaviour = [], None
    monkeypatch.setattr(cli, "sources", lambda: {"fake": Nudged})
    return tmp_path


def nudge(home, at=None):
    listening.update_state(home, "fake", lambda state: state.update(nudge_at=time.time() if at is None else at))


def tick(home):
    return main(["--home", str(home), "tick"])


def config_of(home):
    return {**defaults(), **json.loads((home / "config.json").read_text())}


def test_event_arriving_during_a_check_is_not_swallowed_by_its_end(home):
    nudge(home, time.time() - 1)
    # A new event lands after the source was read but before the tick finishes.
    Nudged.behaviour = lambda h: (time.sleep(0.01), nudge(h))
    tick(home)
    Nudged.behaviour = None
    tick(home)
    assert len(Nudged.calls) == 2


def test_failed_read_keeps_the_nudge_and_backs_off(home):
    nudge(home, time.time() - 1)

    def broken(h):
        raise RuntimeError("source down")

    Nudged.behaviour = broken
    assert tick(home) == 1
    store = Store(home)
    try:
        now = time.time()
        assert fast_sources(config_of(home), store, now=now + 1) == []  # bounded: no retry every minute
        assert fast_sources(config_of(home), store, now=now + 3600) == ["fake"]  # the nudge still stands
    finally:
        store.close()


def test_success_after_a_failure_clears_the_backoff(home):
    nudge(home, time.time() - 1)
    Nudged.behaviour = lambda h: (_ for _ in ()).throw(RuntimeError("down"))
    tick(home)
    store = Store(home)
    store.set_meta("source_retry:fake", {"attempts": 1, "next_at": 0})
    store.close()
    Nudged.behaviour = None
    assert tick(home) == 0
    store = Store(home)
    try:
        assert store.get_meta("source_retry:fake", None) is None
        assert fast_sources(config_of(home), store, now=time.time() + 3600) == []
    finally:
        store.close()


def test_failed_triage_keeps_the_nudge(home, monkeypatch):
    nudge(home, time.time() - 1)
    monkeypatch.setattr(cli, "run_triage", lambda *a, **k: (["triage fake: classifier down"], 0))
    assert tick(home) == 1
    store = Store(home)
    try:
        assert fast_sources(config_of(home), store, now=time.time() + 3600) == ["fake"]
    finally:
        store.close()


def test_backlog_left_by_a_source_is_read_without_a_new_event(home):
    store = Store(home)
    try:
        assert fast_sources(config_of(home), store) == []
        store.set_meta("source_backlog:fake", True)
        assert fast_sources(config_of(home), store) == ["fake"]
        store.set_meta("source_backlog:fake", False)
        assert fast_sources(config_of(home), store) == []
    finally:
        store.close()


def test_backlog_of_a_disabled_source_is_ignored(home):
    store = Store(home)
    try:
        store.set_meta("source_backlog:fake", True)
        config = config_of(home)
        config["sources"]["fake"]["enabled"] = False
        assert fast_sources(config, store) == []
    finally:
        store.close()


# ── Tick pump ───────────────────────────────────────────────────────────────


def test_a_crashing_tick_does_not_stop_later_ticks():
    clock, runs = [100.0], []

    def runner():
        runs.append(clock[0])
        if len(runs) == 1:
            raise TimeoutError("tick hung")
        return 0, ""

    pump = listening.TickPump(runner, lambda: clock[0], logger=lambda message: None)
    pump.schedule(100)
    pump.pump()  # must not raise into the pumping thread
    clock[0] += 5
    pump.pump()
    assert runs == [100.0]  # retried later, not in a tight loop
    clock[0] += 120
    pump.pump()
    assert runs == [100.0, 225.0]


# ── Cloudflare gateway client ───────────────────────────────────────────────


class Sink:
    """Records emits; `fail` makes the next emits raise like a relay that refuses events."""

    def __init__(self):
        self.emitted, self.beats, self.fail = [], [], False

    def emit(self, source, hint=None, wake_after=None):
        if self.fail:
            raise RuntimeError("Relay did not accept the event")
        self.emitted.append(source)

    def beat(self, sources, connected):
        self.beats.append((tuple(sources), connected))

    def pump(self):
        pass


def hooks_client(tmp_path, sink, now=1000.0):
    from hermes_auto.hooks import HooksClient
    return HooksClient(tmp_path, {}, clock=lambda: now, sink=sink)


def gateway(message):
    return json.dumps(message)


def test_gateway_seq_waits_until_the_sink_accepted_the_event(tmp_path):
    sink = Sink()
    client = hooks_client(tmp_path, sink)
    sink.fail = True
    with pytest.raises(RuntimeError):
        client.handle(gateway({"source": "gmail", "seq": 1}))
    assert client.since() == 0  # the reconnect replays it
    sink.fail = False
    assert client.handle(gateway({"source": "gmail", "seq": 1})) == "gmail"
    assert client.since() == 1 and sink.emitted == ["gmail"]


def test_gateway_reset_catches_up_every_gateway_source(tmp_path):
    sink = Sink()
    client = hooks_client(tmp_path, sink)
    client.handle(gateway({"type": "hello", "seq": 40, "epoch": "a"}))
    listening.update_state(tmp_path, "hooks", lambda state: state.update(seq=40))
    # The Durable Object lost its storage: numbering restarts under a new epoch.
    client.handle(gateway({"type": "hello", "seq": 2, "epoch": "b"}))
    assert sorted(sink.emitted) == ["gmail", "todoist"]
    assert client.since() == 2
    client.handle(gateway({"source": "todoist", "seq": 3}))
    assert sink.emitted[-1] == "todoist" and client.since() == 3


def test_gateway_without_epoch_counts_a_lower_head_as_a_reset(tmp_path):
    sink = Sink()
    client = hooks_client(tmp_path, sink)
    listening.update_state(tmp_path, "hooks", lambda state: state.update(seq=40))
    client.handle(gateway({"type": "hello", "seq": 5}))  # a worker from before epochs
    assert sorted(sink.emitted) == ["gmail", "todoist"] and client.since() == 5


def test_first_epoch_seen_is_adopted_without_a_catchup(tmp_path):
    sink = Sink()
    client = hooks_client(tmp_path, sink)
    listening.update_state(tmp_path, "hooks", lambda state: state.update(seq=40))
    client.handle(gateway({"type": "hello", "seq": 41, "epoch": "a"}))  # worker upgraded in place
    assert sink.emitted == [] and client.since() == 40


def test_gateway_retention_gap_catches_up(tmp_path):
    sink = Sink()
    client = hooks_client(tmp_path, sink)
    listening.update_state(tmp_path, "hooks", lambda state: state.update(seq=10))
    # Offline past the gateway's retention: replay starts after events that are gone.
    client.handle(gateway({"source": "gmail", "seq": 700}))
    assert sorted(sink.emitted) == ["gmail", "gmail", "todoist"] and client.since() == 700


def test_gateway_beats_report_liveness_for_its_sources(tmp_path):
    sink = Sink()
    client = hooks_client(tmp_path, sink)
    client.beat(True)
    assert sink.beats == [(("gmail", "todoist"), True)]


def test_pumping_loop_survives_a_failing_step():
    import threading
    stop, calls = threading.Event(), []

    def step():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        if len(calls) >= 3:
            stop.set()

    listening.keep_pumping(stop, step, seconds=0, logger=lambda message: None)
    assert len(calls) == 3


# ── Relay queue and its Mac client ──────────────────────────────────────────


RELAYED = ["slack", "github", "gmail", "todoist"]


def relay_client(tmp_path, responses, sink, clock, **settings):
    from hermes_auto.relay import Client
    return Client(tmp_path, {"sources": RELAYED, "fallback_seconds": 1800, **settings},
                  fetch=lambda since: responses.pop(0), sink=sink, clock=lambda: clock[0])


def test_queue_reports_epoch_head_and_first_kept(tmp_path):
    from hermes_auto import relay
    queue = relay.Queue(tmp_path / "q.sqlite3")
    queue.publish("slack", {}, now=0)
    queue.publish("github", {}, now=relay.KEEP_SECONDS + 10)  # the first one expires
    page = queue.page(0)
    assert page["head"] == 2 and page["last"] == 2 and page["first"] == 2
    assert [e["source"] for e in page["events"]] == ["github"]
    assert page["epoch"] == relay.Queue(tmp_path / "q.sqlite3").page(0)["epoch"]  # survives restarts
    assert page["epoch"] != relay.Queue(tmp_path / "other.sqlite3").page(0)["epoch"]


def test_queue_head_survives_expiry_of_every_event(tmp_path):
    from hermes_auto import relay
    queue = relay.Queue(tmp_path / "q.sqlite3")
    queue.publish("slack", {}, now=0)
    queue.publish("github", {}, now=0)
    queue.db.execute("DELETE FROM events")
    page = queue.page(2)
    assert page["head"] == 2 and page["events"] == []  # not mistaken for a reset queue
    assert queue.publish("gmail", {}) == 3


def test_relay_reset_catches_up_every_relayed_source(tmp_path):
    sink, clock = Sink(), [1000.0]
    listening.update_state(tmp_path, "relay", lambda state: state.update(seq=50, epoch="old"))
    client = relay_client(tmp_path, [{"events": [], "head": 3, "last": 3, "first": 1, "epoch": "new"}],
                          sink, clock)
    client.step()
    assert sorted(sink.emitted) == sorted(RELAYED) and client.seq() == 3


def test_old_relay_with_a_lower_head_counts_as_reset(tmp_path):
    sink, clock = Sink(), [1000.0]
    listening.update_state(tmp_path, "relay", lambda state: state.update(seq=50))
    client = relay_client(tmp_path, [{"events": [], "last": 3}], sink, clock)
    client.step()
    assert sorted(sink.emitted) == sorted(RELAYED) and client.seq() == 3


def test_relay_retention_gap_catches_up_before_the_kept_events(tmp_path):
    sink, clock = Sink(), [1000.0]
    listening.update_state(tmp_path, "relay", lambda state: state.update(seq=5, epoch="e"))
    client = relay_client(tmp_path, [{"events": [{"seq": 90, "source": "slack"}], "head": 90, "last": 90,
                                      "first": 90, "epoch": "e"}], sink, clock)
    client.step()
    assert sorted(sink.emitted[:4]) == sorted(RELAYED) and sink.emitted[4:] == ["slack"]
    assert client.seq() == 90


def test_relay_seq_waits_until_the_sink_accepted(tmp_path):
    sink, clock = Sink(), [1000.0]
    page = {"events": [{"seq": 1, "source": "slack"}, {"seq": 2, "source": "github"}], "head": 2,
            "first": 1, "epoch": "e"}
    client = relay_client(tmp_path, [page, dict(page)], sink, clock)
    sink.fail = True
    with pytest.raises(RuntimeError):
        client.step()
    assert client.seq() == 0
    sink.fail = False
    client.step()
    assert sink.emitted == ["slack", "github"] and client.seq() == 2


def healthy(now, *names):
    return {name: {"heartbeat": now - 30, "connected": True} for name in names}


def test_dead_listener_behind_a_healthy_relay_falls_back_once_per_period(tmp_path):
    sink, clock = Sink(), [10_000.0]
    page = lambda: {"events": [], "head": 0, "first": 1, "epoch": "e", "now": clock[0],  # noqa: E731
                    "sources": {**healthy(clock[0], "gmail", "todoist", "github"),
                                "slack": {"heartbeat": clock[0] - 4000, "connected": True}}}
    responses = [page() for _ in range(3)]
    client = relay_client(tmp_path, responses, sink, clock)
    client.step()
    assert sink.emitted == ["slack"]
    clock[0] += 60
    responses[0]["now"] = clock[0]
    client.step()
    assert sink.emitted == ["slack"]  # bounded: not every long poll
    # The client restarts (crash loop, reboot): the fallback time is durable.
    client = relay_client(tmp_path, responses, sink, clock)
    client.step()
    assert sink.emitted == ["slack"]


def test_listener_that_never_reported_counts_as_dead(tmp_path):
    sink, clock = Sink(), [10_000.0]
    page = {"events": [], "head": 0, "first": 1, "epoch": "e", "now": clock[0],
            "sources": healthy(clock[0], "slack", "github", "gmail")}
    relay_client(tmp_path, [page], sink, clock).step()
    assert sink.emitted == ["todoist"]


def test_listener_disconnected_past_the_stale_limit_counts_as_dead(tmp_path):
    sink, clock = Sink(), [10_000.0]
    sources = healthy(clock[0], *RELAYED)
    sources["github"].update(connected=False, alive_at=clock[0] - 900)  # beating, but offline for 15 min
    page = {"events": [], "head": 0, "first": 1, "epoch": "e", "now": clock[0], "sources": sources}
    relay_client(tmp_path, [page], sink, clock).step()
    assert sink.emitted == ["github"]


def test_brief_disconnect_is_not_a_dead_listener(tmp_path):
    sink, clock = Sink(), [10_000.0]
    sources = healthy(clock[0], *RELAYED)
    sources["github"].update(connected=False, alive_at=clock[0] - 90)  # one failed GitHub request
    page = {"events": [], "head": 0, "first": 1, "epoch": "e", "now": clock[0], "sources": sources}
    relay_client(tmp_path, [page], sink, clock).step()
    assert sink.emitted == []


def test_healthy_idle_relay_causes_no_source_checks(tmp_path):
    sink, clock = Sink(), [10_000.0]
    responses = [{"events": [], "head": 0, "first": 1, "epoch": "e", "now": clock[0] + i * 60,
                  "sources": healthy(clock[0] + i * 60, *RELAYED)} for i in range(60)]
    client = relay_client(tmp_path, responses, sink, clock)
    for _ in range(60):
        client.step()
        clock[0] += 60
    assert sink.emitted == []


def test_liveness_uses_the_relay_clock_not_the_macs(tmp_path):
    sink, clock = Sink(), [10_000.0]
    # The Mac clock runs an hour ahead of the server; heartbeats are fresh by the server's clock.
    page = {"events": [], "head": 0, "first": 1, "epoch": "e", "now": clock[0] - 3600,
            "sources": healthy(clock[0] - 3600, *RELAYED)}
    relay_client(tmp_path, [page], sink, clock).step()
    assert sink.emitted == []


def test_old_relay_without_liveness_triggers_no_fallback(tmp_path):
    sink, clock = Sink(), [10_000.0]
    relay_client(tmp_path, [{"events": [], "last": 0}], sink, clock).step()
    assert sink.emitted == []


def test_relay_records_heartbeats_and_serves_them(tmp_path):
    from http.server import ThreadingHTTPServer
    import threading
    import urllib.request
    from hermes_auto import relay
    queue = relay.Queue(tmp_path / "q.sqlite3")
    server = ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(queue, "s3cret"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    auth = {"Authorization": "Bearer s3cret"}
    try:
        sink = listening.RelaySink(base, "s3cret")
        sink.beat(["gmail", "todoist"], True)
        with urllib.request.urlopen(urllib.request.Request(base + "/events?since=0", headers=auth)) as response:
            page = json.load(response)
        assert page["sources"]["gmail"]["connected"] is True and set(page["sources"]) == {"gmail", "todoist"}
        assert abs(page["now"] - page["sources"]["gmail"]["heartbeat"]) < 5
        sink.beat(["gmail"], False)
        with urllib.request.urlopen(urllib.request.Request(base + "/events?since=0", headers=auth)) as response:
            gmail = json.load(response)["sources"]["gmail"]
        assert gmail["connected"] is False and gmail["alive_at"] <= gmail["heartbeat"]  # last time it was up
        assert page["epoch"] and page["head"] == 0
    finally:
        server.shutdown()


def test_relay_sink_beat_tolerates_an_old_relay(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Old(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Old)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        listening.RelaySink(f"http://127.0.0.1:{server.server_address[1]}", "t").beat(["slack"], True)
    finally:
        server.shutdown()


# ── Listeners on the relay machine ──────────────────────────────────────────


def test_github_change_is_not_marked_seen_until_the_sink_accepted(tmp_path):
    from test_github import NOW, Response, Session, notification
    from hermes_auto_github.listener import GitHubListener
    changed = Response(200, [notification("3", NOW + 40)], modified="Sun, 25 Oct 2026 08:00:00 GMT")
    session = Session([Response(200, [notification("1", NOW - 60)]), changed, changed])
    sink = Sink()
    listener = GitHubListener(tmp_path, {}, session=session, clock=lambda: NOW + 50, sink=sink)
    listener.check()  # baseline after start: one catch-up nudge
    sink.fail = True
    with pytest.raises(RuntimeError):
        listener.check()
    sink.fail = False
    listener.check()
    # Not a conditional 304 on the change the relay never took.
    assert session.headers_sent[2] == {"If-Modified-Since": "Sun, 25 Oct 2026 07:00:00 GMT"}
    assert sink.emitted == ["github", "github"]


def test_github_listener_reports_liveness_to_the_sink(tmp_path):
    from test_github import NOW, Response, Session
    from hermes_auto_github.listener import GitHubListener
    sink = Sink()
    listener = GitHubListener(tmp_path, {}, session=Session([Response(304)]), clock=lambda: NOW, sink=sink)
    listener.check()
    assert sink.beats[-1] == (("github",), True)
    listener.beat(False)
    assert sink.beats[-1] == (("github",), False)


def test_slack_listener_reports_liveness_to_the_sink(tmp_path):
    from test_slack import FakeSlack
    from hermes_auto_slack.listener import Listener
    sink = Sink()
    listener = Listener(tmp_path, {}, http=FakeSlack(), clock=lambda: 1000.0, sink=sink)
    listener.handle({"type": "hello"})
    assert sink.beats[-1] == (("slack",), True)
    listener.beat()  # periodic beat keeps the last known connection state
    assert sink.beats[-1] == (("slack",), True)
    listener.beat(connected=False)
    assert sink.beats[-1] == (("slack",), False)


def test_a_later_wake_does_not_swallow_an_earlier_tick():
    # Todoist asks for a check in 16 minutes; a chat's quiet period ends in 30 seconds: both run, in order.
    clock, runs = [0.0], []

    def runner():
        runs.append(clock[0])
        return 0, ""

    pump = listening.TickPump(runner, lambda: clock[0], logger=lambda message: None)
    pump.schedule(960)
    pump.schedule(30)
    assert pump.due_at == 30
    clock[0] = 30
    pump.pump()
    clock[0] = 100
    pump.pump()
    assert runs == [30]
    clock[0] = 960
    pump.pump()
    pump.pump()
    assert runs == [30, 960] and pump.due_at is None
