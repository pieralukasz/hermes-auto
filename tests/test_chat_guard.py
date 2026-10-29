"""Guard 3.0.2: a chat's first turn may read that chat's history (iMessage via `imsg history`, Matrix via
`hermes-auto-matrix history`) and nothing of any other chat; sending stays blocked. The chat comes from the
session's registry entry (written by the bridge), so scope.json keeps its format and an older guard simply ignores
it. Synthetic sessions and chats only; no real Messages data, no network."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_auto import bridge, guard
from hermes_auto import guard_plugin as plugin
from hermes_auto.guard_plugin import policy

ROOM = "!AbCdEf0123456789_-AbCdEf0123456789_-abcdef"
OTHER_ROOM = "!ZyXwVu9876543210_-ZyXwVu9876543210_-zyxwvu"
IMESSAGE = {"source": "imessage", "id": "12"}
MATRIX = {"source": "matrix", "id": ROOM}


def chat_job(source, **payload):
    return {"id": 3, "source": source, "mode": "draft", "payload": json.dumps(payload), "session_id": "r3"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The default profile with mail reads installed and a state.db like Hermes' (synthetic rows)."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    guard.install(home, ["google", "todoist"])
    db = sqlite3.connect(home / "state.db")
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            api_content TEXT, display_kind TEXT, display_metadata TEXT, _compressed_summary INTEGER DEFAULT 0);
    """)
    db.commit()
    db.close()
    return home


def session(home, sid, parent=None, chat=None, register=True):
    db = sqlite3.connect(home / "state.db")
    if parent:
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id=?", (parent,))
    db.execute("INSERT INTO sessions VALUES (?, ?, NULL)", (sid, parent))
    db.execute("INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
               (sid, "card " + policy.AUTO_TURN_MARK))
    db.commit()
    db.close()
    if register:
        policy.register_session(home, sid, job_id=3, chat=chat)


def fake_run(monkeypatch):
    calls = []
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: calls.append(argv) or
                        SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    return calls


# ── Which chat a job's session belongs to ───────────────────────────────────

def test_a_chat_job_names_its_chat_and_nothing_else_does():
    assert policy.chat_of(chat_job("imessage", chat_id=12)) == IMESSAGE
    assert policy.chat_of(chat_job("matrix", room_id=ROOM)) == MATRIX
    assert policy.chat_of(chat_job("slack", chat_id=12, room_id=ROOM)) is None
    for bad in ({"chat_id": "12; rm"}, {"chat_id": True}, {"chat_id": -1}, {}):
        assert policy.chat_of(chat_job("imessage", **bad)) is None
    for bad in ("!a b", "#alias:example.test", "!x;rm", "", "!" + "a" * 300):
        assert policy.chat_of(chat_job("matrix", room_id=bad)) is None
    assert policy.chat_of({"source": "imessage", "payload": "{not json"}) is None


def test_the_registry_entry_carries_the_chat_and_a_compression_continuation_keeps_it(home):
    session(home, "s1", chat=IMESSAGE)
    assert policy.session_chat(home, "s1") == IMESSAGE
    session(home, "s1-cont", parent="s1", register=False)
    assert policy.session_chat(home, "s1-cont") == IMESSAGE
    # An entry from before 3.0.2 (no chat), an unregistered session, a broken entry: no chat read.
    session(home, "old")
    session(home, "plain", register=False)
    assert policy.session_chat(home, "old") is None and policy.session_chat(home, "plain") is None
    session(home, "broken", chat=MATRIX)
    (home / "hermes-auto-guard" / "sessions" / "broken").write_text("{not json")
    assert policy.session_chat(home, "broken") is None
    with pytest.raises(ValueError):
        policy.register_session(home, "bad", chat={"source": "imessage", "id": "1; rm"})


def test_the_bridge_registers_a_chat_session_with_its_chat(home):
    bridge.register({"hermes_home": str(home)}, "sid-im", chat_job("imessage", chat_id=12))
    bridge.register({"hermes_home": str(home)}, "sid-mail", chat_job("gmail", chat_id=12))
    entry = json.loads((home / "hermes-auto-guard" / "sessions" / "sid-im").read_text())
    assert entry == {"job": 3, "version": 1, "chat": IMESSAGE}
    assert "chat" not in json.loads((home / "hermes-auto-guard" / "sessions" / "sid-mail").read_text())


# ── The grammar: only this chat, only reads ─────────────────────────────────

@pytest.mark.parametrize("command", [
    "imsg history --chat-id 12 --limit 30 --json",
    "imsg history --chat-id 12",
    "/opt/homebrew/bin/imsg history --chat-id=12 --limit=50 -j --attachments",
    "imsg history --chat-id 12 --start 2026-10-01T00:00:00Z --end 2026-10-29T00:00:00Z --json",
])
def test_imessage_history_of_the_sessions_chat_is_a_read(command):
    assert policy.read_only_command(command, frozenset(), chat=IMESSAGE)


@pytest.mark.parametrize("command", [
    "imsg history --chat-id 13 --limit 30 --json",          # another chat
    "imsg history --limit 30 --json",                        # no chat: the newest of every chat
    "imsg history --chat-id 12 --chat-id 13",
    "imsg history --chat-id 12 --convert-attachments",       # writes converted files
    "imsg history --chat-id 12 --db /tmp/other.db",
    "imsg history --chat-id 12 --participants +4800",
    "imsg history --chat-id 12 --limit 5000",
    "imsg history --chat-id 12 --limit x",
    "imsg history --chat-id 12 extra",
    "imsg chats --json", "imsg send --chat-id 12 --text hi", "imsg watch --chat-id 12",
    "imsg history --chat-id 12 --json; imsg send --chat-id 12 --text hi",
    "/tmp/imsg history --chat-id 12",
])
def test_anything_else_from_imsg_is_refused(command):
    assert not policy.read_only_command(command, frozenset(policy.FAMILIES), chat=IMESSAGE)


def test_without_its_chat_no_session_reads_a_chat():
    command = "imsg history --chat-id 12 --limit 30 --json"
    assert not policy.read_only_command(command, frozenset(policy.FAMILIES))
    assert not policy.read_only_command(command, frozenset(policy.FAMILIES), chat=MATRIX)
    matrix = f"hermes-auto-matrix history {ROOM} --limit 30"
    assert not policy.read_only_command(matrix, frozenset(policy.FAMILIES), chat=IMESSAGE)


@pytest.mark.parametrize("command", [f"hermes-auto-matrix history {ROOM} --limit 30",
                                     f"hermes-auto-matrix history {ROOM}",
                                     f"hermes-auto-matrix history --limit=100 {ROOM}"])
def test_matrix_history_of_the_sessions_room_is_a_read(command):
    assert policy.read_only_command(command, frozenset(), chat=MATRIX)


@pytest.mark.parametrize("command", [
    f"hermes-auto-matrix history {OTHER_ROOM} --limit 30",
    f"hermes-auto-matrix history {ROOM} {OTHER_ROOM}",
    "hermes-auto-matrix history --limit 30",
    f"hermes-auto-matrix history {ROOM} --limit 101",
    f"hermes-auto-matrix history {ROOM} --homeserver https://evil.test",
    f"hermes-auto-matrix history {ROOM} --token-file /tmp/t",
    f"hermes-auto-matrix send {ROOM} hi", "hermes-auto-matrix check", "hermes-auto-matrix rooms",
    f"/tmp/hermes-auto-matrix history {ROOM}",
])
def test_anything_else_from_the_matrix_helper_is_refused(command):
    assert not policy.read_only_command(command, frozenset(policy.FAMILIES), chat=MATRIX)


# ── The read tool in a locked chat session ───────────────────────────────────

def test_the_read_tool_reads_the_sessions_chat_even_without_read_families(home, monkeypatch):
    calls = fake_run(monkeypatch)
    guard.install(home, [])  # an account with no reads at all still sees its own chat
    session(home, "chat-1", chat=IMESSAGE)
    session(home, "mail-1")
    command = {"command": "imsg history --chat-id 12 --limit 30 --json"}
    assert json.loads(plugin.read_tool(command, session_id="chat-1")) == {"output": "ok", "exit_code": 0}
    assert json.loads(plugin.read_tool(command, session_id="mail-1"))["skipped"] is True
    assert json.loads(plugin.read_tool({"command": "imsg history --chat-id 13 --json"},
                                       session_id="chat-1"))["skipped"] is True
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id="chat-1"))["skipped"] is True
    assert calls == [["imsg", "history", "--chat-id", "12", "--limit", "30", "--json"]]
    # Sending stays blocked in the locked turn whatever the tool.
    blocked = plugin.pre_tool_call(tool_name="terminal", args={"command": "imsg send --chat-id 12 --text x"},
                                   session_id="chat-1", task_id="", turn_id="t1")
    assert blocked and blocked["action"] == "block"


def test_the_scope_file_keeps_its_format():
    data = json.loads(guard.scope_bytes(["google", "todoist"], {}))
    assert set(data) == {"version", "mode", "reads"} and data["version"] == 2
    assert "imessage" not in policy.FAMILIES and "matrix" not in policy.FAMILIES


def test_guard_version_is_3_0_2_in_code_and_manifest():
    manifest = (Path(plugin.__file__).parent / "plugin.yaml").read_text()
    assert policy.GUARD_VERSION == "3.0.2" and 'version: "3.0.2"' in manifest
