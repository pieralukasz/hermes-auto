"""hermes-hooks gateway client and Gmail push plumbing (no network)."""
import json

import pytest

from hermes_auto import listening
from hermes_auto.hooks import HooksClient
from hermes_auto.store import Store
from hermes_auto_gmail import Gmail, Source


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_gateway_events_nudge_their_source_once(tmp_path):
    clock, ran = Clock(1000.0), []
    client = HooksClient(tmp_path, {}, clock=clock, runner=lambda: ran.append(clock()) or (0, ""))
    assert client.handle(json.dumps({"type": "hello", "seq": 0})) is None
    assert listening.read_state(tmp_path, "hooks")["connected"]
    assert client.handle(json.dumps({"source": "gmail", "seq": 1, "historyId": "5"})) == "gmail"
    assert client.handle(json.dumps({"source": "gmail", "seq": 1})) is None  # replayed
    assert client.since() == 1
    state = listening.read_state(tmp_path, "gmail")
    assert state["nudge_at"] == 1000.0 and state["events_since_start"] == 1 and "historyId" not in state
    client.ticks.pump()
    assert ran == []
    clock.now += 10
    client.ticks.pump()
    assert ran == [1010.0]
    assert client.handle("pong") is None


def test_gmail_due_only_after_a_push(store, tmp_path):
    source = Source()
    assert not source.due({}, store, 0, 1000)
    listening.write_state(tmp_path, "gmail", {"nudge_at": 995})
    assert not source.due({}, store, 0, 1000)
    assert source.due({}, store, 0, 1006)
    assert not source.due({}, store, 1001, 1006)


class Response:
    def __init__(self, ok=True, expiration="1800604800000"):
        self.ok, self.status_code = ok, 200 if ok else 403
        self._json = {"historyId": "1", "expiration": expiration}

    def json(self):
        return self._json


class Http:
    def __init__(self, ok=True):
        self.posts, self.ok = [], ok

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return Response(self.ok)


def gmail_with(http):
    gmail = Gmail.__new__(Gmail)
    gmail.http = http
    return gmail


def test_watch_renews_daily_and_only_with_a_topic(store):
    http = Http()
    gmail = gmail_with(http)
    assert gmail.renew_watch({}, store, "me@example.test", now=0) is None and http.posts == []
    config = {"push_topic": "projects/p/topics/t"}
    state = gmail.renew_watch(config, store, "me@example.test", now=1000)
    assert http.posts[0][1] == {"topicName": "projects/p/topics/t", "labelIds": ["INBOX"],
                                "labelFilterBehavior": "include"}
    assert state["expiration"] == 1800604800
    gmail.renew_watch(config, store, "me@example.test", now=1000 + 3600)
    assert len(http.posts) == 1
    gmail.renew_watch(config, store, "me@example.test", now=1000 + 86400)
    assert len(http.posts) == 2


def test_watch_failure_is_loud(store):
    with pytest.raises(RuntimeError):
        gmail_with(Http(ok=False)).renew_watch({"push_topic": "projects/p/topics/t"}, store, "me@example.test")
