"""Items shown in a Hermes session or brief are marked read at their source, and only then."""
import datetime as dt
import json

import pytest

from hermes_auto import triage
from hermes_auto.config import defaults
from hermes_auto.store import Store

from test_triage import Fake, mail


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {"gmail": {"enabled": True}},
            "triage": {"enabled": True, "brief_times": ["07:45", "16:30"]}}


class Recording:
    calls, fail = [], False

    def mark_read(self, config, items):
        if Recording.fail:
            raise RuntimeError("bridge down")
        Recording.calls.append([item["id"] for item in items])


def marks(store):
    return dict(store.db.execute("SELECT item_id, status FROM read_marks").fetchall())


def decide(store, config):
    hermes = Fake(replies=[[{"i": 0, "decision": "session", "reason": "pytanie", "summary": "Jan pyta"},
                            {"i": 1, "decision": "brief", "reason": "faktura", "summary": "Faktura"},
                            {"i": 2, "decision": "skip", "reason": "promo", "summary": "Promo"}]])
    triage.triage_mail(store, config, hermes, "gmail", "me@example.test",
                       [mail("M1", "T1"), mail("M2", "T2"), mail("M3", "T3")])


def test_session_item_is_marked_only_when_its_session_is_ready(store, config):
    Recording.calls, Recording.fail = [], False
    decide(store, config)
    assert marks(store) == {"M1": "pending"}  # brief waits for the brief; skip stays unread
    assert triage.flush_read_marks(store, config, {"gmail": Recording}) == []
    assert Recording.calls == [] and not triage.read_marks_waiting(store)  # session not ready yet
    job = store.rows()[0]
    store.update(job["id"], status="ready")
    assert triage.read_marks_waiting(store)
    assert triage.flush_read_marks(store, config, {"gmail": Recording}) == []
    assert Recording.calls == [["M1"]] and marks(store) == {"M1": "done"}


def test_failed_session_keeps_the_item_unread(store, config):
    Recording.calls, Recording.fail = [], False
    decide(store, config)
    store.update(store.rows()[0]["id"], status="needs_attention")
    triage.flush_read_marks(store, config, {"gmail": Recording})
    assert Recording.calls == [] and marks(store) == {"M1": "pending"}


def test_brief_items_are_marked_when_the_brief_is_ready(store, config):
    Recording.calls, Recording.fail = [], False
    decide(store, config)
    title = triage.maybe_brief(store, config, now=dt.datetime(2026, 10, 25, 7, 50))
    assert title
    assert marks(store) == {"M1": "pending", "M2": "pending"}
    brief = [job for job in store.rows() if job["source"] == "brief"][0]
    store.update(brief["id"], status="ready")
    triage.flush_read_marks(store, config, {"gmail": Recording})
    assert Recording.calls == [["M2"]] and marks(store)["M2"] == "done"


def test_retry_then_give_up_and_unsupported_sources(store, config):
    Recording.calls, Recording.fail = [], True
    decide(store, config)
    store.update(store.rows()[0]["id"], status="ready")
    for _ in range(triage.MAX_READ_ATTEMPTS):
        assert triage.flush_read_marks(store, config, {"gmail": Recording})
    assert marks(store) == {"M1": "failed"}
    store.db.execute("UPDATE read_marks SET status='pending', source='slack'")
    store.db.commit()
    triage.flush_read_marks(store, config, {})
    assert marks(store) == {"M1": "unsupported"}


def test_followup_reply_is_marked_with_its_new_session(store, config):
    Recording.calls, Recording.fail = [], False
    item = mail("M9", "T9", sent=[{"ts": 1_792_600_000, "to": ["dad@example.test"]}], subject="Re: Filmy")
    triage.triage_mail(store, config, Fake(found="S-dad", sessions={"S-dad": {}}), "gmail", "me@example.test",
                       [item])
    assert marks(store) == {"M9": "pending"}
    triage.process_followups(store, config, Fake(sessions={"S-dad": {}}))
    job = [job for job in store.rows() if job["external_id"] == "M9"][0]
    store.update(job["id"], status="ready")
    triage.flush_read_marks(store, config, {"gmail": Recording})
    assert Recording.calls == [["M9"]]


def test_mark_read_can_be_turned_off(store, config):
    config["triage"]["mark_read"] = False
    decide(store, config)
    assert marks(store) == {}


def test_github_marks_each_thread_once(monkeypatch):
    from hermes_auto_github import Source
    import hermes_auto_github as gh
    patched = []

    class Http:
        def __init__(self, token):
            pass

        def mark_thread_read(self, thread):
            patched.append(thread)

    monkeypatch.setattr(gh, "Http", Http)
    monkeypatch.setattr(gh, "gh_token", lambda command: "t")
    Source().mark_read({"sources": {"github": {}}}, [{"id": "1@a", "thread_id": "1"}, {"id": "1@b", "thread_id": "1"},
                                                    {"id": "2@a", "thread_id": "2"}])
    assert patched == ["1", "2"]
    assert json.dumps(patched)
