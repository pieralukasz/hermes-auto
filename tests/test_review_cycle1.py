"""Fix cycle 1 after the independent review: send provenance, sorter retries, followup snapshot,
delivery guard, LinkedIn identity. Synthetic data only."""
import argparse
import datetime as dt
import json
import sqlite3

import pytest

from hermes_auto import bridge, cli, triage
from hermes_auto.bridge import find_session
from hermes_auto.config import defaults
from hermes_auto.store import Store

T = 1_800_000_000.0
THREAD = "18f0gmailthread"
SCRIPT = "/Users/x/.hermes/skills/productivity/google-workspace/scripts/google_api.py"
SENT = [{"ts": T + 600, "to": ["jan@kontrahent.test"]}]
QUERY = {"thread_id": THREAD, "subject": "Re: Faktura 10/2026", "sent": SENT}


# ── S1: send provenance needs a parsed send call and its correlated receipt ──

def hermes_db(tmp_path, rows):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_call_id TEXT, tool_calls TEXT, timestamp REAL)")
    db.executemany("INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, timestamp) "
                   "VALUES (?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    return path


def call(command, call_id="c1", name="terminal"):
    return json.dumps([{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps({"command": command})}}])


def result(output, exit_code=0):
    return json.dumps({"output": output, "exit_code": exit_code, "error": None})


RECEIPT = json.dumps({"status": "sent", "id": "m1", "threadId": THREAD}, indent=2)
SEND = f'python {SCRIPT} gmail send --to jan@kontrahent.test --subject "Faktura 10/2026" --body "W załączeniu"'
REPLY = f"python3 {SCRIPT} gmail reply m0 --body 'Dziękuję'"


def sender(session="SENDER", command=SEND, output=RECEIPT, call_id="c1", exit_code=0, result_session=None):
    return [(session, "assistant", "", None, call(command, call_id), T + 590),
            (result_session or session, "tool", result(output, exit_code), call_id, None, T + 591)]


@pytest.mark.parametrize("command", [SEND, REPLY])
def test_send_call_with_correlated_receipt_routes(tmp_path, command):
    assert find_session(hermes_db(tmp_path, sender(command=command)), QUERY) == "SENDER"


def test_standalone_sent_tool_row_is_not_provenance(tmp_path):
    receipt = json.dumps({"status": "sent", "id": "m1", "threadId": THREAD})
    path = hermes_db(tmp_path, [("X", "tool", receipt, None, None, T + 598),
                                ("Y", "tool", result(RECEIPT), "orphan", None, T + 598)])
    assert find_session(path, QUERY) is None


def test_read_result_with_sent_label_is_not_provenance(tmp_path):
    read = f"python {SCRIPT} gmail get m1"
    output = json.dumps({"id": "m1", "threadId": THREAD, "labels": ["SENT", "INBOX"]})
    assert find_session(hermes_db(tmp_path, sender(command=read, output=output)), QUERY) is None


def test_read_command_mentioning_in_reply_to_is_not_provenance(tmp_path):
    read = f"python {SCRIPT} gmail search 'in-reply-to send reply {THREAD}'"
    assert find_session(hermes_db(tmp_path, sender(command=read)), QUERY) is None


def test_draft_or_unknown_tool_is_not_provenance(tmp_path):
    rows = [("D", "assistant", "", None, call(f"python {SCRIPT} gmail draft --to jan@kontrahent.test", "d1"), T),
            ("D", "tool", result(RECEIPT), "d1", None, T + 1),
            ("C", "assistant", "", None, call(SEND, "x1", name="send_message"), T),
            ("C", "tool", result(RECEIPT), "x1", None, T + 1)]
    assert find_session(hermes_db(tmp_path, rows), QUERY) is None


def test_send_call_without_receipt_is_not_provenance(tmp_path):
    rows = sender()[:1]
    assert find_session(hermes_db(tmp_path, rows), QUERY) is None


@pytest.mark.parametrize("kwargs", [
    {"exit_code": 1},
    {"output": json.dumps({"status": "sent", "id": "m1", "threadId": "otherthread99"})},
    {"output": "Error: quota\n" + RECEIPT + "\n" + RECEIPT},
    {"result_session": "OTHER"},
    {"command": SEND + "; echo hi"},
    {"command": SEND.replace("python ", "python $(echo) ")},
])
def test_failed_mismatched_or_forged_receipts_are_rejected(tmp_path, kwargs):
    assert find_session(hermes_db(tmp_path, sender(**kwargs)), QUERY) is None


def test_old_schema_without_tool_call_id_fails_closed(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_calls TEXT, timestamp REAL)")
    db.execute("INSERT INTO messages(session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
               ("S", "assistant", "", call(SEND), T + 590))
    db.commit()
    db.close()
    assert find_session(path, QUERY) is None


# ── Shared fixtures for the core ──────────────────────────────────────────────

NOW = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)).replace(second=10, microsecond=0)


@pytest.fixture
def store(tmp_path):
    result_ = Store(tmp_path, initialize=True)
    yield result_
    result_.close()


def cfg():
    return {**defaults(), "language": "Polish",
            "sources": {"gmail": {"enabled": True}, "todoist": {"enabled": True, "command": "td"}},
            "triage": {"enabled": True, "todoist": {"backlog": True, "settle_minutes": 0}}}


class Hermes:
    def __init__(self, replies=None, sessions=None):
        self.replies, self.sessions = list(replies or []), sessions or {}
        self.classified, self.runs, self.inspected = [], [], []

    def recent_sessions(self, days=21, limit=400):
        return []

    def classify(self, system, user, max_tokens=0):
        self.classified.append(json.loads(user))
        return self.replies.pop(0)

    def find_session(self, **query):
        return None

    def inspect(self, session_id, profile=None):
        self.inspected.append(session_id)
        info = self.sessions.get(session_id)
        return {"exists": info is not None, "last_active": (info or {}).get("last_active")}

    def unarchive(self, session_id):
        pass

    def run(self, job, prompt):
        self.runs.append((job, prompt))
        return {"session_id": job["session_id"]}


class Adapter:
    def __init__(self, tasks, fail=None):
        self.tasks = list(tasks)
        self.all = {task["id"]: dict(task) for task in tasks}
        self.fail = fail  # callable(task, decision) -> raise, or None
        self.applied = []
        self.read_error = None

    def inbox_tasks(self, settings):
        return list(self.tasks)

    def task(self, settings, task_id):
        if self.read_error:
            raise self.read_error
        return self.all.get(task_id)

    def outline(self, settings, store, hours):
        return {"projects": [{"id": "P", "path": "Dom", "sections": [], "samples": []}]}

    def apply(self, settings, task, decision, label):
        self.tasks = [t for t in self.tasks if t["id"] != task["id"]]
        if self.fail:
            self.fail(task, decision)
        self.applied.append((task["id"], decision))
        self.all[task["id"]]["projectId"] = decision["project_id"]
        return {"agent": False, "labels": [], "run": False}


def dec(task_id, **fields):
    return {"id": task_id, "project_id": "P", "priority": "p2", "agent": False, "start": "later", **fields}


def sort(store, adapter, hermes):
    return triage.triage_todoist(store, cfg(), hermes, adapter, now=NOW)


def item_state(store, task_id="A"):
    row = store.db.execute("SELECT decision, reason FROM triage_items WHERE item_id=?", (task_id,)).fetchone()
    return row["decision"], json.loads(row["reason"] or "{}")


def partial_failure(task, decision):
    raise RuntimeError("td failed")


# ── L1: a retry never overrides the user's own move ─────────────────────────

def test_retry_does_not_override_users_move_to_another_project(store):
    adapter = Adapter([{"id": "A", "content": "A", "labels": [], "projectId": "INBOX"}], fail=partial_failure)
    hermes = Hermes([[dec("A")]])
    sort(store, adapter, hermes)
    with pytest.raises(RuntimeError):
        sort(store, adapter, hermes)
    adapter.all["A"]["projectId"] = "USERS"  # the user filed it elsewhere after the failure
    adapter.fail = None
    sort(store, adapter, hermes)
    assert adapter.applied == []
    assert item_state(store)[0] == "user_changed"


def test_retry_still_applies_when_task_is_in_original_or_target_project(store):
    adapter = Adapter([{"id": "A", "content": "A", "labels": [], "projectId": "INBOX"}], fail=partial_failure)
    hermes = Hermes([[dec("A")]])
    sort(store, adapter, hermes)
    with pytest.raises(RuntimeError):
        sort(store, adapter, hermes)
    adapter.all["A"]["projectId"] = "P"  # partly applied: already in the target
    adapter.fail = None
    sort(store, adapter, hermes)
    assert [task_id for task_id, _ in adapter.applied] == ["A"]
    assert item_state(store)[0] == "planned"


# ── L2: bounded retries, visible issues, read errors are not deletions ──────

def test_apply_retries_stop_at_needs_attention_and_status_shows_it(store, tmp_path, capsys):
    adapter = Adapter([{"id": "A", "content": "Zadanie A", "labels": []}], fail=partial_failure)
    hermes = Hermes([[dec("A")]])
    sort(store, adapter, hermes)
    for _ in range(triage.MAX_APPLY_ATTEMPTS):
        with pytest.raises(RuntimeError):
            sort(store, adapter, hermes)
    decision, state = item_state(store)
    assert decision == "needs_attention" and state["attempts"] == triage.MAX_APPLY_ATTEMPTS
    calls = len(adapter.applied)
    sort(store, adapter, hermes)  # no further attempts, no error
    assert len(adapter.applied) == calls
    cli.dispatch(argparse.Namespace(command="status"), tmp_path, cfg(), store)
    issues = json.loads(capsys.readouterr().out)["triage_issues"]
    assert [(i["item_id"], i["decision"], i["attempts"]) for i in issues] == [
        ("A", "needs_attention", triage.MAX_APPLY_ATTEMPTS)]
    assert "td failed" in issues[0]["error"]


def test_task_read_error_is_retried_not_marked_deleted(store):
    adapter = Adapter([{"id": "A", "content": "A", "labels": []}], fail=partial_failure)
    hermes = Hermes([[dec("A")]])
    sort(store, adapter, hermes)
    with pytest.raises(RuntimeError):
        sort(store, adapter, hermes)
    adapter.read_error = RuntimeError("td exited 1: network")
    with pytest.raises(RuntimeError, match="network"):
        sort(store, adapter, hermes)
    decision, state = item_state(store)
    assert decision == "decided" and state["attempts"] == 2


# ── L3: one dead session redirects once per check ───────────────────────────

def test_two_replies_to_a_dead_session_open_one_new_conversation_in_one_check(store):
    config = cfg()
    for number in (1, 2):
        item = {"id": f"r{number}", "thread_id": "T1", "account": "me@example.test", "from": "Jan <j@x.test>",
                "subject": "Pytanie", "date": "d", "body": "b"}
        store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload) VALUES (?,?,?,?,?)",
                         (f"k{number}", "gmail", "DEAD", "t", json.dumps(item)))
    store.set_meta(triage.thread_key("gmail", "me@example.test", "T1"), "DEAD")
    store.db.commit()
    hermes = Hermes()
    triage.process_followups(store, config, hermes)
    assert len(store.rows()) == 1
    assert hermes.inspected.count("DEAD") == 1
    second = store.db.execute("SELECT session_id, status FROM followups WHERE event_key='k2'").fetchone()
    assert tuple(second) == (store.rows()[0]["session_id"], "pending")


def test_recover_followups_helper_is_gone_store_recover_covers_it():
    assert not hasattr(triage, "recover_followups")


# ── L5: delivery guard ───────────────────────────────────────────────────────

class Lineage:
    def __init__(self, lineage=None, tip=None, fail=False):
        self.lineage, self.tip, self.fail = lineage, tip, fail

    def get_compression_lineage(self, sid):
        if self.fail:
            raise RuntimeError("db")
        return self.lineage

    def resolve_resume_session_id(self, sid):
        return self.tip or sid


def test_delivery_scope_covers_the_whole_compression_lineage():
    ids = bridge.delivery_scope(Lineage(lineage=["ROOT", "MID", "TIP"], tip="TIP"), "MID")
    assert set(ids) == {"ROOT", "MID", "TIP"}


def test_delivery_scope_fails_closed_without_lineage_api():
    assert bridge.delivery_scope(Lineage(fail=True), "S") is None
    assert bridge.delivery_scope(object(), "S") is None


def test_retry_followup_needles_match_the_prompt_text(store):
    item = {"id": "M1", "from": '"Nowak, Jan" <jan@kontrahent.test>', "date": " Fri, 23 Oct 2026\t10:00 ",
            "subject": "s", "body": "b"}
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload,status) VALUES "
                     "('k','gmail','S1','t',?,'needs_attention')", (json.dumps(item),))
    store.db.commit()
    calls = []

    class H:
        def invoke(self, action, job=None, **extra):
            calls.append(extra)
            return {"exists": True, "delivered": False}
    cli.retry_followup(store, H(), store.db.execute("SELECT MAX(id) FROM followups").fetchone()[0])
    block = triage.mail_block(item)
    assert calls[0]["needles"] == ["Fri, 23 Oct 2026 10:00", '"Nowak, Jan" <jan@kontrahent.test>']
    assert all(needle in block for needle in calls[0]["needles"])


def test_retry_followup_refuses_when_delivery_is_unknown(store):
    item = {"id": "M1", "from": "Jan <jan@kontrahent.test>", "date": "Fri", "subject": "s", "body": "b"}
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload,status) VALUES "
                     "('k','gmail','S1','t',?,'needs_attention')", (json.dumps(item),))
    store.db.commit()

    class H:
        def invoke(self, action, job=None, **extra):
            return {"exists": True, "delivered": None}
    with pytest.raises(ValueError):
        cli.retry_followup(store, H(), store.db.execute("SELECT MAX(id) FROM followups").fetchone()[0])


# ── LinkedIn: only the provider's thread id groups mail ─────────────────────

LINKEDIN = '"Anna Nowak via LinkedIn" <messaging-digest-noreply@linkedin.com>'


def test_linkedin_body_link_and_name_never_pick_the_conversation():
    body = "https://www.linkedin.com/messaging/thread/2-ABCdef_123==/"
    item = {"id": "M", "thread_id": "T1", "from": LINKEDIN, "body": body}
    assert triage.conversation_id(item) == "T1"
    assert triage.conversation_id({**item, "body": ""}) == "T1"


def test_forged_linkedin_thread_link_does_not_route_into_a_session(store):
    body = "https://www.linkedin.com/messaging/thread/2-ABCdef_123==/"
    store.set_meta(triage.thread_key("gmail", "me@example.test", "linkedin:thread:2-ABCdef_123=="), "REAL")
    store.set_meta(triage.thread_key("gmail", "me@example.test", "linkedin:anna nowak"), "REAL")
    hermes = Hermes([[{"i": 0, "decision": "skip", "reason": "", "summary": ""}]])
    item = {"id": "m9", "thread_id": "T9", "account": "me@example.test", "from": LINKEDIN, "to": "me",
            "subject": "x", "date": "d", "body": body, "sent": []}
    triage.triage_mail(store, cfg(), hermes, "gmail", "me@example.test", [item])
    assert len(hermes.classified) == 1
    assert store.db.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 0


def test_two_sessions_with_correlated_send_receipts_fail_closed(tmp_path):
    rows = sender("A", call_id="a1") + sender("B", call_id="b1")
    path = hermes_db(tmp_path, rows)
    assert find_session(path, QUERY) is None
    assert find_session(path, {**QUERY, "exclude": ["A"]}) == "B"


def test_receipt_matching_the_sent_message_id_routes_without_thread_id(tmp_path):
    path = hermes_db(tmp_path, sender())
    query = {"thread_id": "", "sent": [{"ts": T + 600, "to": [], "id": "m1"}]}
    assert find_session(path, query) == "SENDER"
    assert find_session(path, {**query, "sent": [{"ts": T + 600, "to": [], "id": "m2"}]}) is None
