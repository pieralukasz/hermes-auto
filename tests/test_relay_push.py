"""Relay queue, sinks and push-mode scheduling (no network, no real Todoist/Gmail)."""
import datetime as dt
import json
import threading
import time

import pytest

from hermes_auto import listening
from hermes_auto.cli import brief_due, main
from hermes_auto.config import defaults, private_json
from hermes_auto.relay import Client, Queue, make_handler
from hermes_auto.store import Store
from hermes_auto_gmail import Source as Gmail
from hermes_auto_todoist import Source as Todoist
from hermes_auto_todoist import due_timestamp


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


def test_queue_orders_waits_and_replays(tmp_path):
    queue = Queue(tmp_path / "q.sqlite3")
    assert queue.since(0) == []
    first = queue.publish("slack", {"hint": {"channel": "D1"}})
    queue.publish("github", {})
    events = queue.since(0)
    assert [e["source"] for e in events] == ["slack", "github"] and events[0]["hint"] == {"channel": "D1"}
    assert [e["source"] for e in queue.since(first)] == ["github"]
    later = []
    waiter = threading.Thread(target=lambda: later.extend(queue.since(queue.last(), wait=5)))
    waiter.start()
    time.sleep(0.1)
    queue.publish("gmail", {})
    waiter.join(3)
    assert [e["source"] for e in later] == ["gmail"]


def test_relay_http_requires_the_token(tmp_path):
    from http.server import ThreadingHTTPServer
    import urllib.error
    import urllib.request
    queue = Queue(tmp_path / "q.sqlite3")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(queue, "s3cret"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(urllib.request.Request(base + "/publish", data=b'{"source":"x"}', method="POST"))
        assert denied.value.code == 403
        request = urllib.request.Request(base + "/publish", data=json.dumps({"source": "github"}).encode(),
                                         method="POST", headers={"Authorization": "Bearer s3cret"})
        assert json.load(urllib.request.urlopen(request))["seq"] == 1
        request = urllib.request.Request(base + "/events?since=0", headers={"Authorization": "Bearer s3cret"})
        assert [e["source"] for e in json.load(urllib.request.urlopen(request))["events"]] == ["github"]
    finally:
        server.shutdown()


class Recorder:
    def __init__(self):
        self.emitted = []

    def emit(self, source, hint=None, wake_after=None):
        self.emitted.append((source, hint, wake_after))

    def pump(self):
        pass


def test_client_hands_events_once_and_falls_back_when_relay_is_down(tmp_path):
    sink, clock = Recorder(), [1000.0]
    batches = [[{"seq": 1, "source": "todoist"}, {"seq": 2, "source": "slack", "hint": {"channel": "D1"}}],
               [{"seq": 2, "source": "slack"}]]
    client = Client(tmp_path, {"again_after": {"todoist": 960}, "sources": ["slack", "github"],
                               "fallback_seconds": 1800}, fetch=lambda since: batches.pop(0), sink=sink,
                    clock=lambda: clock[0])
    assert client.step() == 2 and client.step() == 1
    assert sink.emitted == [("todoist", None, 960), ("slack", {"channel": "D1"}, None)]
    assert client.seq() == 2
    assert not client.failed()
    clock[0] += 1800
    assert client.failed()
    assert sink.emitted[-2:] == [("slack", None, None), ("github", None, None)]
    clock[0] += 60
    assert not client.failed()  # once per fallback period


def test_local_sink_merges_hints_and_wakes(tmp_path):
    clock, ran = [100.0], []
    sink = listening.LocalSink(tmp_path, {"slack": 5}, runner=lambda: ran.append(1) or (0, ""),
                               clock=lambda: clock[0])
    sink.emit("slack", hint={"channel": "C1", "thread_ts": "1.0", "why": "thread", "ts": "2.0"})
    sink.emit("slack", hint={"channel": "C1", "thread_ts": "1.0", "why": "mention", "ts": "3.0"})
    state = listening.read_state(tmp_path, "slack")
    assert state["hints"]["C1:1.0"]["why"] == "thread" and state["events_since_start"] == 2
    sink.emit("todoist", wake_after=960)
    assert listening.nudged(tmp_path, "todoist", 0, 106, 5)
    assert not listening.nudged(tmp_path, "todoist", 106, 500, 5)
    assert listening.nudged(tmp_path, "todoist", 106, 1061, 5)  # the settle wake-up


def test_todoist_due_on_webhook_new_day_and_timed_task(store, tmp_path):
    source = Todoist()
    now = dt.datetime(2026, 10, 25, 12, 0).timestamp()
    assert not source.due({}, store, now - 60, now)
    assert source.due({}, store, dt.datetime(2026, 10, 24, 23, 50).timestamp(), now)  # a new day
    store.set_meta("todoist:next_due_at", now - 30)
    assert source.due({}, store, now - 60, now)
    assert not source.due({}, store, now - 10, now)
    listening.write_state(tmp_path, "todoist", {"nudge_at": now - 20})
    assert source.due({}, store, now - 60, now)


def test_due_timestamp():
    assert due_timestamp({"due": {"date": "2026-10-25"}}) is None
    assert due_timestamp({"due": {"date": "2026-10-25T07:30:00Z"}}) == dt.datetime(
        2026, 10, 25, 7, 30, tzinfo=dt.timezone.utc).timestamp()


def test_gmail_due_renews_a_lapsing_watch(store):
    settings = {"push_topic": "projects/p/topics/t"}
    assert Gmail().due(settings, store, 0, 1000)  # never renewed
    store.set_meta("gmail_watch:me@example.test", {"renewed": 1000})
    assert not Gmail().due(settings, store, 900, 2000)
    assert Gmail().due(settings, store, 900, 1000 + 86400)
    assert not Gmail().due({}, store, 0, 10 ** 9)  # no push configured: nothing to renew


def test_brief_due(store):
    config = {**defaults(), "triage": {"enabled": True, "brief_times": ["07:45", "16:30"]}}
    assert brief_due(config, store, dt.datetime(2026, 10, 25, 8, 0))
    store.set_meta("brief_slot", "2026-10-25 07:45")
    assert not brief_due(config, store, dt.datetime(2026, 10, 25, 8, 0))
    assert brief_due(config, store, dt.datetime(2026, 10, 25, 16, 31))


def test_push_mode_tick_reads_nothing_without_a_nudge(tmp_path, monkeypatch):
    config = {**defaults(), "desktop_only": False, "poll_sources": False, "sources": {}}
    private_json(tmp_path / "config.json", config)
    Store(tmp_path, initialize=True).close()
    store = Store(tmp_path)
    store.set_meta("last_poll", time.time())
    store.close()
    called = []
    monkeypatch.setattr("hermes_auto.cli.poll", lambda c, s, only=None: called.append(only) or [])
    assert main(["--home", str(tmp_path), "tick"]) == 0
    assert called == []  # not due, no source due, no brief: nothing ran
