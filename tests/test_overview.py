"""Read-only overview for the menu bar app, and opening a message the sorter passed over."""
import argparse
import json
import time

import pytest

from hermes_auto import cli, triage
from hermes_auto.config import defaults
from hermes_auto.store import Store

GMAIL = "gmail:me@example.test"


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    return {**defaults(), "sources": {"gmail": {"enabled": True}, "slack": {"enabled": False}}}


def add_item(store, decision, item_id="m1", kind="mail", account=GMAIL, **payload):
    item = {"id": item_id, "thread_id": "t-" + item_id, "subject": "Faktura za wrzesień", "from": "Jan <jan@firma.test>",
            "body": "Poufna treść maila", "url": "https://mail.example.test/m1", **payload}
    store.db.execute("INSERT INTO triage_items(account,item_id,kind,decision,reason,summary,payload) "
                     "VALUES (?,?,?,?,?,?,?)", (account, item_id, kind, decision, "newsletter", "Jan wysyła fakturę.",
                                                json.dumps(item, ensure_ascii=False)))
    store.db.commit()
    return item


def run(store, tmp_path, config, capsys, command, **args):
    code = cli.dispatch(argparse.Namespace(command=command, **args), tmp_path, config, store)
    return code, capsys.readouterr().out


def test_events_show_decision_and_reason_without_the_body(store, tmp_path, config, capsys):
    add_item(store, "skip")
    _, out = run(store, tmp_path, config, capsys, "events", limit=50)
    [event] = json.loads(out)
    assert event["decision"] == "skip" and event["reason"] == "newsletter" and event["summary"] == "Jan wysyła fakturę."
    assert event["title"] == "Faktura za wrzesień" and event["source"] == "gmail" and event["can_open"] is True
    assert event["session_id"] is None
    assert "Poufna" not in out  # message bodies stay out of CLI output


def test_open_item_queues_a_session_and_later_mail_follows_it(store, tmp_path, config, capsys):
    item = add_item(store, "brief")
    code, out = run(store, tmp_path, config, capsys, "open-item", account=GMAIL, item_id="m1")
    assert code == 0
    job = store.job(json.loads(out)["job_id"])
    payload = json.loads(job["payload"])
    assert job["status"] == "pending" and job["mode"] == "draft" and job["source"] == "gmail"
    assert payload["triage"]["decision"] == "session" and payload["deny_tools"] is True
    assert payload["triage"]["reason"].startswith(triage.MANUAL_OPEN)
    # The next message of this thread continues in the new session instead of opening another.
    assert store.get_meta(triage.thread_key("gmail", "me@example.test", item["thread_id"])) == job["session_id"]
    row = store.db.execute("SELECT decision, briefed FROM triage_items WHERE item_id='m1'").fetchone()
    assert tuple(row) == ("session", 1)  # not listed in the next brief as well
    _, out = run(store, tmp_path, config, capsys, "events", limit=50)
    [event] = json.loads(out)
    assert event["job_id"] == job["id"] and event["session_id"] == job["session_id"] and event["can_open"] is False


def test_open_item_keeps_the_research_profile_of_slack(store):
    add_item(store, "skip", item_id="s1", kind="slack", account="slack:T1")
    store.db.execute("UPDATE triage_items SET payload=json_set(payload,'$.kind','slack') WHERE item_id='s1'")
    job_id = triage.open_item(store, "slack:T1", "s1")
    assert json.loads(store.job(job_id)["payload"])["deny_tools"] == "research"


@pytest.mark.parametrize("decision,kind", [("session", "mail"), ("followup", "mail"), ("planned", "task")])
def test_open_item_refuses_what_already_has_its_flow(store, decision, kind):
    add_item(store, decision, kind=kind)
    with pytest.raises(ValueError):
        triage.open_item(store, GMAIL, "m1")
    assert store.rows() == []


def test_open_item_twice_points_at_new_session(store):
    add_item(store, "skip")
    first = triage.open_item(store, GMAIL, "m1")
    store.db.execute("UPDATE triage_items SET decision='skip' WHERE item_id='m1'")  # e.g. a stale app view
    with pytest.raises(ValueError, match=f"new-session {first}"):
        triage.open_item(store, GMAIL, "m1")
    assert len(store.rows()) == 1


def test_status_health_reports_listener_heartbeat_and_retry(store, tmp_path, config, capsys):
    now = time.time()
    (tmp_path / "gmail-listener.json").write_text(json.dumps({"connected": True, "heartbeat": now, "secret": "x"}))
    store.set_meta("last_poll:gmail", now - 60)
    store.set_meta("source_retry:gmail", {"attempts": 2, "next_at": now + 120})
    _, out = run(store, tmp_path, config, capsys, "status")
    health = json.loads(out)["health"]
    assert set(health["sources"]) == {"gmail"}  # disabled sources are not shown
    gmail = health["sources"]["gmail"]
    assert gmail["last_poll"] == pytest.approx(now - 60) and gmail["retry_at"] == pytest.approx(now + 120)
    assert gmail["listener"]["connected"] is True and "secret" not in gmail["listener"]
    assert health["relay"] is None
