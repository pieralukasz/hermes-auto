"""Proton sends traced to their Hermes session, and human replies in the user's threads as sessions.
Synthetic data only; no network, no real Bridge."""
import json
import sqlite3

import pytest

from hermes_auto import triage
from hermes_auto.bridge import find_session, proton_send_command
from hermes_auto.store import Store
from hermes_auto_proton import Proton, send

T = 1_800_000_000.0
ROOT = "<root.1@protonmail.test>"
MINE = "<mine.2@protonmail.test>"
SEND = "hermes-auto-proton-send --to jan@firma.test --subject 'Oferta 7/2026' --body-file /tmp/body.txt"
REPLY = (f"/Users/x/.local/bin/hermes-auto-proton-send --to jan@firma.test --subject 'Re: Oferta' "
         f"--body-file /tmp/b.txt --in-reply-to '{ROOT}'")


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


# ── Recognising the Proton sender ──

@pytest.mark.parametrize("command", [SEND, REPLY])
def test_plain_proton_send_is_recognised(command):
    assert proton_send_command(command)


@pytest.mark.parametrize("command", [
    SEND + "; echo hi",
    SEND.replace("--to", "$(echo) --to"),
    "hermes-auto-proton-send --help",
    "python3 send_replies.py --to jan@firma.test",
    "himalaya message send < mail.eml",
    "cd ~/.hermes/cache/scratch && python3 - <<'EOF'\nimport smtplib\nEOF",
    "echo hermes-auto-proton-send",
])
def test_other_commands_are_not_proton_sends(command):
    assert not proton_send_command(command)


# ── find_session with a Proton receipt ──

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


def sender(command, receipt, session="SENDER", exit_code=0):
    call = json.dumps([{"id": "c1", "type": "function",
                        "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}])
    output = json.dumps({"output": json.dumps(receipt), "exit_code": exit_code, "error": None})
    return [(session, "assistant", "", None, call, T + 590), (session, "tool", output, "c1", None, T + 591)]


def test_reply_to_a_proton_send_finds_the_session_by_message_id(tmp_path):
    receipt = {"status": "sent", "id": MINE, "threadId": ROOT}
    query = {"thread_id": "<other.root@x.test>", "sent": [{"ts": T + 600, "to": ["jan@firma.test"], "id": MINE}]}
    assert find_session(hermes_db(tmp_path, sender(REPLY, receipt)), query) == "SENDER"


def test_new_proton_thread_matches_by_thread_root(tmp_path):
    receipt = {"status": "sent", "id": ROOT, "threadId": ROOT}
    query = {"thread_id": ROOT, "sent": [{"ts": T + 600, "to": ["jan@firma.test"]}]}
    assert find_session(hermes_db(tmp_path, sender(SEND, receipt)), query) == "SENDER"


@pytest.mark.parametrize("command,receipt,exit_code", [
    (SEND, {"status": "sent", "id": "<someone.else@x.test>", "threadId": "<other@x.test>"}, 0),
    (SEND, {"status": "sent", "id": MINE, "threadId": ROOT}, 1),
    ("himalaya envelope list -f Sent", {"status": "sent", "id": MINE, "threadId": ROOT}, 0),
])
def test_unrelated_failed_or_read_results_do_not_route(tmp_path, command, receipt, exit_code):
    query = {"thread_id": ROOT, "sent": [{"ts": T + 600, "to": ["jan@firma.test"], "id": MINE}]}
    assert find_session(hermes_db(tmp_path, sender(command, receipt, exit_code=exit_code)), query) is None


# ── The sender itself ──

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.sent, self.refuse = host, port, [], set()
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls = context is not None

    def login(self, user, password):
        self.user, self.password = user, password

    def send_message(self, message):
        self.sent.append(message)
        return {address: (550, b"no") for address in self.refuse}


def config_home(tmp_path):
    cert = tmp_path / "bridge.pem"
    cert.write_text("")
    (tmp_path / "config.json").write_text(json.dumps({"sources": {"proton": {
        "username": "me@proton.test", "certificate": str(cert),
        "password_command": ["printf", "sekret-haslo"]}}}))
    return tmp_path


@pytest.fixture
def no_tls(monkeypatch):
    monkeypatch.setattr(send.ssl, "create_default_context", lambda cafile=None: type("Ctx", (), {})())


def test_send_prints_a_receipt_and_threads_the_reply(tmp_path, capsys, no_tls):
    home = config_home(tmp_path)
    body = tmp_path / "body.txt"
    body.write_text("Dzień dobry,\nw załączeniu oferta.")
    FakeSMTP.instances.clear()
    assert send.main(["--home", str(home), "--from", "Lucas <me@protonmail.test>", "--to", "jan@firma.test",
                      "--subject", "Re: Oferta", "--body-file", str(body), "--in-reply-to", ROOT], smtp=FakeSMTP) == 0
    out = capsys.readouterr().out
    receipt = json.loads(out)
    message = FakeSMTP.instances[0].sent[0]
    assert receipt == {"status": "sent", "id": message["Message-ID"], "threadId": ROOT}
    assert message["Message-ID"].endswith("@protonmail.test>")
    assert message["In-Reply-To"] == ROOT and message["References"] == ROOT
    assert (FakeSMTP.instances[0].host, FakeSMTP.instances[0].port) == ("127.0.0.1", 1025)
    assert FakeSMTP.instances[0].password == "sekret-haslo" and "sekret" not in out


def test_new_mail_is_its_own_thread_root_and_partial_refusal_still_has_a_receipt(tmp_path, capsys, no_tls):
    home = config_home(tmp_path)
    body = tmp_path / "body.txt"
    body.write_text("x")

    class Partial(FakeSMTP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.refuse = {"zly@firma.test"}

    send.main(["--home", str(home), "--to", "jan@firma.test", "--to", "zly@firma.test", "--subject", "Oferta",
               "--body-file", str(body)], smtp=Partial)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["threadId"] == receipt["id"] and receipt["refused"] == ["zly@firma.test"]


def test_send_refuses_a_remote_host(tmp_path):
    home = config_home(tmp_path)
    config = json.loads((home / "config.json").read_text())
    config["sources"]["proton"]["host"] = "mail.example.test"
    (home / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError):
        send.settings_from(home)


# ── Adapter: the user's sent message carries its Message-ID ──

def raw(message_id, sender, refs=""):
    headers = f"Message-ID: {message_id}\r\nFrom: {sender}\r\nTo: me@proton.test\r\nSubject: Oferta\r\n"
    headers += "Date: Thu, 29 Oct 2026 10:00:00 +0100\r\n"
    if refs:
        headers += f"References: {refs}\r\nIn-Reply-To: {refs.split()[-1]}\r\n"
    return (headers + "Content-Type: text/plain; charset=utf-8\r\n\r\nDziękuję, przekazałem dalej.\r\n").encode()


class FakeProton(Proton):
    def __init__(self, mailboxes):
        self.mailboxes, self.current = mailboxes, None

    def select(self, mailbox):
        self.current = mailbox

    def inbox_status(self):
        return 7, max(self.mailboxes["INBOX"]) + 1

    def search(self, *criteria):
        box = self.mailboxes[self.current]
        if criteria[0] == "HEADER":
            wanted = criteria[2].strip('"')
            return [str(uid).encode() for uid, data in box.items() if f"Message-ID: {wanted}".encode() in data]
        if "SEEN" in criteria:
            return []
        return [str(uid).encode() for uid in box]

    def fetch(self, uid, headers=False):
        return self.mailboxes[self.current][int(uid)]


def test_proton_candidate_lists_the_users_sent_message_id(store):
    proton = FakeProton({"INBOX": {1: raw("<old@x.test>", "a@x.test")}, "Sent": {9: raw(MINE, "me@proton.test")}})
    settings = {"username": "me@proton.test"}
    _, key, cursor = proton.inbox(settings, store)
    store.set_meta(key, cursor)
    proton.mailboxes["INBOX"][2] = raw("<reply.3@firma.test>", "jan@firma.test", refs=MINE)
    candidates, _, _ = proton.inbox(settings, store)
    assert [entry["id"] for entry in candidates[0]["sent"]] == [MINE]


# ── Classifier: a human reply in the user's thread is a session ──

class Recorder:
    def __init__(self):
        self.prompts = []

    def classify(self, system, user, max_tokens=4000, **route):
        self.prompts.append((system, json.loads(user)))
        return [{"i": i, "decision": "session", "reason": "", "summary": ""} for i in range(len(json.loads(user)))]


def test_classifier_sees_the_thread_flag_and_the_fixed_reply_rule():
    hermes = Recorder()
    item = {"id": "<reply.3@firma.test>", "from": "Jan <jan@firma.test>", "subject": "Re: Oferta",
            "body": "Dziękuję", "sent": [{"ts": T, "to": ["jan@firma.test"], "id": MINE}]}
    triage.classify_batch(hermes, {"triage": {}}, [item, {**item, "id": "<n@x.test>", "sent": []}], None)
    system, user = hermes.prompts[0]
    assert triage.MAIL_REPLY_RULE in system
    assert [entry["user_wrote_in_thread"] for entry in user] == [True, False]
