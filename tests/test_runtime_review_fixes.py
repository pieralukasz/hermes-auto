"""Review fixes for the runtime core: reply routing, followup recovery, status/retry, empty-session retry."""
import json
import sqlite3

import pytest

from hermes_auto.bridge import find_session

T = 1_800_000_000.0


def hermes_db(tmp_path, rows):
    """Synthetic Hermes state.db with the message columns find_session reads."""
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_calls TEXT, timestamp REAL)")
    db.executemany("INSERT INTO messages(session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)", rows)
    db.commit()
    db.close()
    return path


def send_call(command):
    return json.dumps([{"id": "c1", "type": "function",
                        "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}])


SENT = [{"ts": T + 600, "to": ["jan@kontrahent.test"]}]
QUERY = {"thread_id": "18f0gmailthread", "subject": "Re: Faktura 10/2026", "sent": SENT}


def test_find_session_ignores_prose_mentions_of_subject_or_recipient(tmp_path):
    path = hermes_db(tmp_path, [
        ("BRIEF", "assistant", "- **Jan** · Faktura 10/2026: prosi o zapłatę", None, T),
        ("PRIVATE", "user", "notatka: jan@kontrahent.test ma dług, wątek 18f0gmailthread", None, T + 500),
    ])
    assert find_session(path, QUERY) is None


def test_find_session_ignores_tool_calls_that_only_read_the_thread(tmp_path):
    read = send_call('python google_api.py gmail search "to:jan@kontrahent.test subject:Faktura 10/2026"')
    path = hermes_db(tmp_path, [("READER", "assistant", "", read, T + 590)])
    assert find_session(path, QUERY) is None


def test_find_session_rejects_a_receipt_without_its_send_call(tmp_path):
    # Contract (review cycle 1): a receipt alone is no provenance; see tests/test_review_cycle1.py.
    receipt = json.dumps({"status": "sent", "id": "m1", "threadId": "18f0gmailthread"})
    path = hermes_db(tmp_path, [
        ("PRIVATE", "user", "jan@kontrahent.test Faktura 10/2026", None, T + 500),
        ("SENDER", "tool", receipt, None, T + 598),
    ])
    assert find_session(path, QUERY) is None


def test_find_session_rejects_a_send_command_without_its_receipt(tmp_path):
    call = send_call('python google_api.py gmail send --to jan@kontrahent.test --subject "Faktura 10/2026" '
                     '--body "W załączeniu"')
    path = hermes_db(tmp_path, [("SENDER", "assistant", "", call, T + 595)])
    assert find_session(path, QUERY) is None


def test_find_session_fails_closed_without_role_column(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, content TEXT, "
               "tool_calls TEXT)")
    db.execute("INSERT INTO messages VALUES (1, 'X', ?, ?, NULL)", (T + 590, '{"status": "sent", '
                                                                      '"threadId": "18f0gmailthread"}'))
    db.commit()
    db.close()
    assert find_session(path, QUERY) is None


# ── Followups: recovery, status, explicit retry ──────────────────────────────

import argparse  # noqa: E402

from hermes_auto import cli, triage  # noqa: E402
from hermes_auto.config import defaults  # noqa: E402
from hermes_auto.store import Store  # noqa: E402

MAIL = {"id": "M1", "account": "me@example.test", "from": "Jan <jan@kontrahent.test>", "to": "me@example.test",
        "date": "Fri, 23 Oct 2026 10:00:00 +0200", "subject": "Re: Faktura 10/2026", "body": "Dzień dobry"}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {}, "triage": {"enabled": False}}


def add_followup(store, status, session="S1", error=""):
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload,status,error) "
                     "VALUES (?,?,?,?,?,?,?)", (f"gmail:me:{MAIL['id']}:{status}", "gmail", session,
                                                MAIL["subject"], json.dumps(MAIL), status, error))
    store.db.commit()
    return store.db.execute("SELECT MAX(id) FROM followups").fetchone()[0]


def followup(store, row_id):
    return dict(store.db.execute("SELECT * FROM followups WHERE id=?", (row_id,)).fetchone())


def test_recover_moves_interrupted_followup_to_needs_attention_not_replay(store, config):
    stuck = add_followup(store, "running")
    waiting = add_followup(store, "pending")
    store.recover()
    assert followup(store, stuck)["status"] == "needs_attention"
    assert "inspect" in followup(store, stuck)["error"].lower()
    assert followup(store, waiting)["status"] == "pending"

    class Recorder:
        runs = []

        def inspect(self, session_id):
            return {"exists": True, "last_active": None}

        def unarchive(self, session_id):
            pass

        def run(self, job, prompt):
            self.runs.append(job["id"])
            return {"session_id": job["session_id"]}
        def session_excerpts(self, ids):
            return {}
    hermes = Recorder()
    triage.process_followups(store, config, hermes)
    assert hermes.runs == []  # replies open new sessions; nothing runs in the old one
    assert followup(store, waiting)["status"] == "redirected"  # the interrupted reply is not replayed
    assert followup(store, stuck)["status"] == "needs_attention"


def test_scheduled_run_recovers_followups_even_without_pending_jobs(store, config, tmp_path):
    stuck = add_followup(store, "running")
    cli.dispatch(argparse.Namespace(command="run"), tmp_path, config, store)
    assert followup(store, stuck)["status"] == "needs_attention"


def test_status_lists_followups_with_their_errors(store, config, tmp_path, capsys):
    broken = add_followup(store, "needs_attention", error="Hermes run failed (exit 1)")
    cli.dispatch(argparse.Namespace(command="status"), tmp_path, config, store)
    shown = json.loads(capsys.readouterr().out)["followups"]
    assert shown == [{"id": broken, "source": "gmail", "title": MAIL["subject"], "status": "needs_attention",
                      "session_id": "S1", "attempts": 0, "error": "Hermes run failed (exit 1)"}]
    assert "Dzień dobry" not in json.dumps(shown, ensure_ascii=False)  # no mail bodies in CLI output


def test_bridge_reports_whether_reply_text_reached_the_session(tmp_path):
    from hermes_auto.bridge import reply_delivered
    needles = [MAIL["date"], "jan@kontrahent.test"]
    # Whatever framing the prompt uses (fences, notes), the reply's date and sender reach the user message.
    path = hermes_db(tmp_path, [
        ("S1", "user", f"(Automat: ...)\n<<<MAIL-ab12\nOd: Jan <jan@kontrahent.test>\nData: {MAIL['date']}", None, T),
        ("S2", "assistant", f"jan@kontrahent.test {MAIL['date']}", None, T),  # quoting it back is not delivery
        ("S3", "user", "jan@kontrahent.test wrote earlier", None, T),  # one field alone is not enough
    ])
    assert reply_delivered(path, ["S1"], needles) is True
    assert reply_delivered(path, ["S2", "S3"], needles) is False
    assert reply_delivered(path, ["S1"], []) is False


class RetryHermes:
    def __init__(self, exists=True, delivered=False):
        self.state, self.calls = {"exists": exists, "delivered": delivered}, []

    def invoke(self, action, job=None, **extra):
        self.calls.append((action, job, extra))
        return {**self.state, "messages": 4, "archived": False, "last_active": None}


def test_retry_followup_requeues_in_the_same_session(store):
    row = add_followup(store, "needs_attention", error="interrupted")
    hermes = RetryHermes()
    cli.retry_followup(store, hermes, row)
    assert (followup(store, row)["status"], followup(store, row)["session_id"]) == ("retry_pending", "S1")
    action, job, extra = hermes.calls[0]
    assert action == "inspect" and job == {"session_id": "S1"}
    assert extra["needles"] == [MAIL["date"], MAIL["from"]]  # raw From, as the mail prompt prints it


def test_retry_followup_refuses_when_delivery_cannot_be_checked(store):
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload,status) "
                     "VALUES ('k','gmail','S1','t','{}','needs_attention')")
    store.db.commit()
    row = store.db.execute("SELECT MAX(id) FROM followups").fetchone()[0]
    hermes = RetryHermes()
    with pytest.raises(ValueError, match="cannot tell"):
        cli.retry_followup(store, hermes, row)
    assert followup(store, row)["status"] == "needs_attention" and hermes.calls == []


def test_retry_followup_refuses_when_reply_already_reached_session(store):
    row = add_followup(store, "needs_attention", error="interrupted")
    with pytest.raises(ValueError, match="already"):
        cli.retry_followup(store, RetryHermes(delivered=True), row)
    assert followup(store, row)["status"] == "needs_attention"
    assert "S1" in followup(store, row)["error"]


def test_retry_followup_never_recreates_a_deleted_session(store):
    row = add_followup(store, "needs_attention")
    with pytest.raises(ValueError, match="deleted"):
        cli.retry_followup(store, RetryHermes(exists=False), row)
    assert followup(store, row)["status"] == "deleted"
    assert store.rows() == []  # no replacement conversation either


def test_retry_followup_only_for_needs_attention(store):
    row = add_followup(store, "ready")
    with pytest.raises(ValueError, match="needs_attention"):
        cli.retry_followup(store, RetryHermes(), row)
    with pytest.raises(ValueError, match="No followup"):
        cli.retry_followup(store, RetryHermes(), 999)


def test_cli_parser_has_retry_followup_command():
    args = cli.parser().parse_args(["retry-followup", "7"])
    assert (args.command, args.followup_id) == ("retry-followup", 7)


# ── Retry of a job whose session stayed empty ────────────────────────────────

from hermes_auto.runner import process_jobs  # noqa: E402


class CrashOnceHermes:
    """Session row exists after the first create crashed; it holds `messages` messages."""

    def __init__(self, messages=0):
        self.messages, self.creates, self.prompts, self.crashed = messages, [], [], False

    def create(self, job, prompt):
        self.creates.append(prompt)
        if not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt

    def exists(self, job):
        return True

    def message_count(self, job):
        return self.messages

    def run(self, job, prompt):
        self.prompts.append(prompt)
        return {"session_id": job["session_id"]}

    # Agent runs start in the background (main 9a7b1c3); this run finishes at once.
    def start(self, job, prompt, display=None):
        return {"result": self.run(job, prompt)}

    def collect(self, handle):
        return handle["result"]


def crashed_job(store, config, hermes):
    store.enqueue("todoist:T:once", "todoist", "T", "Zrób raport VAT", "agent",
                  {"id": "T", "content": "Zrób raport VAT", "description": "z KSeF"})
    store.db.commit()
    with pytest.raises(KeyboardInterrupt):
        process_jobs(store, {**config, "sources": {"todoist": {"enabled": True}}}, hermes)
    store.recover()
    job = store.rows()[0]
    assert job["status"] == "needs_attention"
    store.update(job["id"], status="retry_pending", error="")  # `hermes-auto retry`
    return job


def test_retry_of_empty_session_sends_the_original_task_and_restores_the_session(store, config):
    hermes = CrashOnceHermes(messages=0)
    job = crashed_job(store, config, hermes)
    process_jobs(store, {**config, "sources": {"todoist": {"enabled": True}}}, hermes)
    assert "Zrób raport VAT" in hermes.prompts[0] and "z KSeF" in hermes.prompts[0]
    assert len(hermes.creates) == 2  # title/visibility finished on the same session id
    assert store.job(job["id"])["status"] == "ready"
    assert store.job(job["id"])["session_id"] == job["session_id"]


def test_retry_of_started_session_keeps_the_continuation_prompt(store, config):
    hermes = CrashOnceHermes(messages=3)
    crashed_job(store, config, hermes)
    process_jobs(store, {**config, "sources": {"todoist": {"enabled": True}}}, hermes)
    assert "Zrób raport VAT" not in hermes.prompts[0]
    assert "continuation" in hermes.prompts[0] and len(hermes.creates) == 1


def test_retry_followup_matches_dates_as_prompts_print_them(store):
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload,status) VALUES "
                     "('k2','gmail','S1','t',?,'needs_attention')",
                     (json.dumps({**MAIL, "date": " Fri,  23 Oct 2026\t10:00:00 +0200 "}),))
    store.db.commit()
    hermes = RetryHermes()
    cli.retry_followup(store, hermes, store.db.execute("SELECT MAX(id) FROM followups").fetchone()[0])
    assert hermes.calls[0][2]["needles"][0] == "Fri, 23 Oct 2026 10:00:00 +0200"  # one_line() form


def test_crash_during_empty_session_retry_needs_attention_again(store, config):
    hermes = CrashOnceHermes(messages=0)
    job = crashed_job(store, config, hermes)
    hermes.crashed = False  # the retry's create is interrupted too
    with pytest.raises(KeyboardInterrupt):
        process_jobs(store, {**config, "sources": {"todoist": {"enabled": True}}}, hermes)
    store.recover()
    assert store.job(job["id"])["status"] == "needs_attention"  # not silently retried on the next check
