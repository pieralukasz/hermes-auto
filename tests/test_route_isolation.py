"""A classifier failure on one model route (e.g. the personal account rate limited) must not hold back the
items of another route from the same source; the failed items wait and are sorted once the route works."""
import json
import time

import pytest

from hermes_auto import cli, triage
from hermes_auto.config import defaults
from hermes_auto.store import Store

WORK = {"provider": "work-account"}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {"github": {"enabled": True}},
            "triage": {"enabled": True, "brief_times": ["07:45", "16:30"]},
            "routes": [{"match": {"source": "github", "owner": "acme-org"}, **WORK}]}


class Sorter:
    """Classifier stand-in: the default route fails while `down`, the work route always answers."""

    def __init__(self, down=True):
        self.down = down
        self.calls = []

    def classify(self, system, user, max_tokens=0, route=None):
        sent = json.loads(user)
        self.calls.append((route, sorted(entry["text"] for entry in sent)))
        if route is None and self.down:
            raise RuntimeError("AuxiliaryClientUnavailable: anthropic credentials cooling down after 429")
        return [{"i": entry["i"], "decision": "session", "reason": "", "summary": entry["text"]} for entry in sent]

    def find_session(self, **query):
        return None


def note(nid, owner):
    return {"kind": "github", "id": f"{nid}@t", "thread_id": nid, "account": "me", "owner": owner,
            "place": f"{owner}/repo", "from": "bot", "why": "author", "type": "PullRequest", "body": nid,
            "subject": f"PR {nid}", "read": False}


def sessions(store):
    return sorted(job["external_id"] for job in store.rows())


def decided(store):
    return dict(store.db.execute("SELECT item_id, decision FROM triage_items").fetchall())


def test_failing_route_keeps_its_items_pending_while_the_other_route_is_sorted(store, config):
    sorter = Sorter(down=True)
    with pytest.raises(RuntimeError, match="cooling down"):
        triage.triage_mail(store, config, sorter, "github", "me", [note("1", "me"), note("2", "acme-org")],
                           "cursor", {"since": 2})
    assert sessions(store) == ["2@t"]
    assert decided(store) == {"2@t": "session"}
    assert store.get_meta("cursor") == {"since": 2}
    marked = [row[0] for row in store.db.execute("SELECT item_id FROM read_marks")]
    assert "1@t" not in marked

    # Before the retry is due the failed item is not sent to the classifier again.
    sorter.calls.clear()
    triage.triage_mail(store, config, sorter, "github", "me", [], "cursor", {"since": 3})
    assert sorter.calls == [] and sessions(store) == ["2@t"]

    # The provider is back: the next read (nothing new, the decided item offered again) sorts only item 1.
    sorter.down = False
    triage.triage_mail(store, config, sorter, "github", "me", [note("2", "acme-org")], "cursor", {"since": 4},
                       now=time.time() + 3600)
    assert sorter.calls == [(None, ["1"])]
    assert sessions(store) == ["1@t", "2@t"]
    assert decided(store) == {"1@t": "session", "2@t": "session"}

    sorter.calls.clear()
    triage.triage_mail(store, config, sorter, "github", "me", [], "cursor", {"since": 5}, now=time.time() + 7200)
    assert sorter.calls == [] and len(store.rows()) == 2


def test_pending_item_joins_a_call_on_its_route_before_its_retry_is_due(store, config):
    sorter = Sorter(down=True)
    with pytest.raises(RuntimeError):
        triage.triage_mail(store, config, sorter, "github", "me", [note("1", "me"), note("2", "acme-org")])
    sorter.down = False
    sorter.calls.clear()
    triage.triage_mail(store, config, sorter, "github", "me", [note("3", "me")])
    assert sorter.calls == [(None, ["1", "3"])]
    assert sessions(store) == ["1@t", "2@t", "3@t"]


class Notifications:
    """Source stand-in: hands each queued batch once, like a cursor-based adapter."""
    batches = []

    def inbox(self, config, store, limit=40):
        return (Notifications.batches.pop(0) if Notifications.batches else []), "cursor", {"read": time.time()}


def test_route_failure_does_not_back_off_the_source_and_its_retry_makes_the_source_due(store, config,
                                                                                       monkeypatch):
    sorter = Sorter(down=True)
    Notifications.batches = [[note("1", "me"), note("2", "acme-org")]]
    errors, _ = cli.run_triage(config, store, lambda: sorter, {"github": Notifications}, only=["github"])
    assert len(errors) == 1 and "github" in errors[0] and "cooling down" in errors[0]
    assert sessions(store) == ["2@t"]
    cli.settle_checked(store, ["github"], errors, time.time())
    # The source is not throttled: the other route's next notification is read at the next nudge.
    assert store.get_meta("source_retry:github", None) is None

    later = time.time() + 3600
    assert "github" in cli.fast_sources(config, store, now=later)
    sorter.down = False
    monkeypatch.setattr(triage.time, "time", lambda: later)
    # The source has nothing new; the waiting item is sorted anyway.
    errors, _ = cli.run_triage(config, store, lambda: sorter, {"github": Notifications}, only=["github"])
    assert errors == [] and sessions(store) == ["1@t", "2@t"]
    sorter.calls.clear()
    errors, _ = cli.run_triage(config, store, lambda: sorter, {"github": Notifications}, only=["github"])
    assert errors == [] and sorter.calls == [] and len(store.rows()) == 2
