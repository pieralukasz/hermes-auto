"""Opt-in contract check with installed Hermes (>= v0.22.5: every turn runs in a gateway), no network, no real
model: a local fake Anthropic server answers. Each test gets its own Hermes root OUTSIDE ~/.hermes (a home under
it would be served by the user's own gateway), started and stopped here; nothing touches the user's sessions.

Run: HERMES_AUTO_TEST_PYTHON=<first element of `hermes --print-runtime-command`> \
     HERMES_AUTO_TEST_SOURCE=~/.hermes/hermes-agent .venv/bin/pytest -q tests/test_real_hermes.py
"""
import http.server
import json
import os
import shutil
import signal
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

from hermes_auto import guard
from hermes_auto.config import defaults
from hermes_auto.guard_plugin import policy
from hermes_auto.hermes import Hermes
from hermes_auto.store import Store

CONTRACT = pytest.mark.skipif(not os.environ.get("HERMES_AUTO_TEST_PYTHON"), reason="local Hermes runtime not configured")


@pytest.mark.skipif(not os.environ.get("HERMES_AUTO_TEST_PYTHON"), reason="local Hermes runtime not configured")
def test_classify_route_reads_provider_key_from_hermes_env(tmp_path):
    """A routed provider's `key_env` lives in the profile's .env; classify must send it, not a placeholder."""
    seen = []

    class FakeAnthropic(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            seen.append(self.headers.get("x-api-key") or self.headers.get("authorization") or "")
            message = {"id": "msg_test", "type": "message", "role": "assistant", "model": "test-model",
                       "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": {"input_tokens": 1, "output_tokens": 1}}
            text = "{\"ok\": true}"
            if request.get("stream"):
                events = [("message_start", {"type": "message_start", "message": message}),
                          ("content_block_start", {"type": "content_block_start", "index": 0,
                                                   "content_block": {"type": "text", "text": ""}}),
                          ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": text}}),
                          ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                          ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 1},
                                             "delta": {"stop_reason": "end_turn", "stop_sequence": None}}),
                          ("message_stop", {"type": "message_stop"})]
                body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()
                kind = "text/event-stream"
            else:
                body = json.dumps({**message, "content": [{"type": "text", "text": text}],
                                   "stop_reason": "end_turn"}).encode()
                kind = "application/json"
            self.send_response(200)
            self.send_header("content-type", kind)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "providers:\n  work-test:\n    name: Work (test)\n"
        f"    api: http://127.0.0.1:{server.server_port}\n    transport: anthropic_messages\n"
        "    key_env: HERMES_AUTO_TEST_WORK_KEY\n    default_model: test-model\n    models: [test-model]\n")
    (hermes_home / ".env").write_text("HERMES_AUTO_TEST_WORK_KEY=synthetic-work-key\n")
    config = {**defaults(), "hermes_home": str(hermes_home),
              "hermes_python": os.environ["HERMES_AUTO_TEST_PYTHON"],
              "hermes_source": os.environ["HERMES_AUTO_TEST_SOURCE"]}
    try:
        result = Hermes(config, tmp_path).classify("Reply with JSON.", "Synthetic item",
                                                   route={"provider": "work-test", "model": "test-model"})
    finally:
        server.shutdown()
    assert result == {"ok": True}
    assert seen and all("synthetic-work-key" in header for header in seen)


def fake_anthropic(turns, seen):
    """A local Anthropic Messages server answering each request with the next scripted turn (streamed)."""
    class Server(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            seen.append(request)
            # Only agent turns (with tools) take the script; model probes and side calls get plain text.
            scripted = turns and request.get("tools")
            blocks, stop = turns.pop(0) if scripted else ([{"type": "text", "text": "Gotowe."}], "end_turn")
            message = {"id": "msg_test", "type": "message", "role": "assistant", "model": "test-model",
                       "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": {"input_tokens": 1, "output_tokens": 1}}
            events = [("message_start", {"type": "message_start", "message": message})]
            for index, block in enumerate(blocks):
                if block["type"] == "text":
                    events += [("content_block_start", {"type": "content_block_start", "index": index,
                                                        "content_block": {"type": "text", "text": ""}}),
                               ("content_block_delta", {"type": "content_block_delta", "index": index,
                                                        "delta": {"type": "text_delta", "text": block["text"]}})]
                else:
                    events += [("content_block_start", {"type": "content_block_start", "index": index,
                                                        "content_block": {"type": "tool_use", "id": block["id"],
                                                                          "name": block["name"], "input": {}}}),
                               ("content_block_delta", {"type": "content_block_delta", "index": index,
                                                        "delta": {"type": "input_json_delta",
                                                                  "partial_json": json.dumps(block["input"])}})]
                events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
            events += [("message_delta", {"type": "message_delta", "usage": {"output_tokens": 1},
                                          "delta": {"stop_reason": stop, "stop_sequence": None}}),
                       ("message_stop", {"type": "message_stop"})]
            body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class World:
    """A temporary Hermes root with a fake model, optionally the guard plugin, and one gateway (idle-exit)."""

    def __init__(self, root, turns, *, guard_enabled=True, approvals_off=False):
        self.root, self.seen = root, []
        self.server = fake_anthropic(turns, self.seen)
        self.home = root / "hermes"
        self.home.mkdir(parents=True)
        # Dependency state is per Hermes root: borrow the installed one, as every Hermes process reads it.
        (self.home / "installs").symlink_to(Path.home() / ".hermes" / "installs")
        (self.home / "config.yaml").write_text(
            "model:\n  default: test-model\n  provider: work-test\n"
            "providers:\n  work-test:\n    name: Work (test)\n"
            f"    api: http://127.0.0.1:{self.server.server_port}\n    transport: anthropic_messages\n"
            "    key_env: HERMES_AUTO_TEST_WORK_KEY\n    default_model: test-model\n    models: [test-model]\n"
            + ("plugins:\n  enabled: [hermes-auto-guard]\n" if guard_enabled else "")
            + ("approvals:\n  mode: 'off'\n" if approvals_off else ""))
        (self.home / ".env").write_text("HERMES_AUTO_TEST_WORK_KEY=synthetic-work-key\n")
        guard.install(self.home)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "td").write_text("#!/bin/sh\necho \"synthetic td: $*\"\n")
        (bin_dir / "td").chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"
        self.config = {**defaults(), "hermes_home": str(self.home),
                       "hermes_python": os.environ["HERMES_AUTO_TEST_PYTHON"],
                       "hermes_source": os.environ["HERMES_AUTO_TEST_SOURCE"],
                       "routes": [{"match": {"source": "example"}, "provider": "work-test", "model": "test-model"}]}
        self.store = Store(root, initialize=True)
        self.hermes = Hermes(self.config, root)

    def job(self, key, title="Synthetic: review", mode="draft", source="example", **payload):
        self.store.enqueue(key, source, key, title, mode, payload)
        self.store.db.commit()
        return next(job for job in self.store.rows() if job["event_key"] == key)

    def db(self):
        db = sqlite3.connect(self.home / "state.db")
        db.row_factory = sqlite3.Row
        return db

    def tool_results(self, sid):
        db = self.db()
        try:
            return {row["tool_call_id"]: json.loads(row["content"]) if row["content"].startswith("{") else row["content"]
                    for row in db.execute("SELECT tool_call_id, content FROM messages WHERE session_id=? AND role='tool'",
                                          (sid,))}
        finally:
            db.close()

    def close(self):
        self.store.close()
        self.server.shutdown()
        pid_file = self.home / "gateway.pid"
        try:
            record = json.loads(pid_file.read_text())
        except (OSError, ValueError):
            record = {}
        if record.get("hermes_home") == str(self.home) and record.get("pid"):
            try:
                os.kill(record["pid"], signal.SIGTERM)
                for _ in range(50):
                    os.kill(record["pid"], 0)
                    time.sleep(0.2)
            except ProcessLookupError:
                pass


@pytest.fixture
def make_world(monkeypatch):
    base = Path(os.environ.get("HERMES_AUTO_TEST_ROOT") or "~/.cache/hermes-auto-contract").expanduser()
    worlds = []
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")  # never sync or re-mint the checkout's launchers

    def make(turns=(), **options):
        world = World(base / uuid.uuid4().hex[:12], list(turns), **options)
        monkeypatch.setenv("PATH", world.path)
        worlds.append(world)
        return world
    yield make
    for world in worlds:
        world.close()
        shutil.rmtree(world.root, ignore_errors=True)


def bridged(call_id, name, arguments):
    """A call through Hermes' Tool Search bridge (plugin tools and session_search are deferred behind it)."""
    return {"type": "tool_use", "id": call_id, "name": "tool_call", "input": {"name": name, "arguments": arguments}}


@CONTRACT
def test_open_sessions_are_imported_by_the_gateway_under_their_reserved_id(make_world):
    world = make_world()
    brief = world.job("brief", title="Brief", mode="open")
    routed = world.job("routed", title="Routed", mode="open", owner="acme")
    assert world.hermes.invoke("doctor")["ok"]
    for _ in range(2):  # idempotent
        assert world.hermes.create(brief, "Synthetic test context") == brief["session_id"]
    world.hermes.create(routed, "Synthetic test context")
    info = world.hermes.invoke("inspect", brief)
    assert (info["exists"], info["messages"], info["archived"]) == (True, 2, False)
    assert world.hermes.invoke("inspect", brief, needles=["Synthetic test context"])["delivered"] is True
    assert world.hermes.invoke("inspect", brief, needles=["not delivered"])["delivered"] is False
    assert world.hermes.archive_states([brief["session_id"], "missing"]) == {brief["session_id"]: False, "missing": None}
    assert world.hermes.find_session(subject="Synthetic subject", sent=[{"ts": 1.0, "to": []}]) is None
    db = world.db()
    try:
        rows = {row["id"]: row for row in db.execute("SELECT id, title, model, json_extract(model_config, "
                                                      "'$.provider') AS provider FROM sessions")}
    finally:
        db.close()
    assert rows[brief["session_id"]]["title"] == "Brief" and rows[routed["session_id"]]["provider"] == "work-test"
    assert rows[routed["session_id"]]["model"] == "test-model"  # the route's model, not only its account


@CONTRACT
def test_sorted_run_reads_through_the_guard_and_shows_the_card(make_world):
    """Real gateway, fake model: the transcript shows the card while the full prompt is the api_content; the
    session's tools are the guard's reads only; reads run, a refused command gets a neutral result."""
    world = make_world([([bridged("toolu_list", "hermes_auto_skills_list", {}),
                          bridged("toolu_skill", "hermes_auto_skill_view", {"name": "no-such-skill"}),
                          bridged("toolu_other", "session_search", {"query": "x", "profile": "work"}),
                          bridged("toolu_td", "hermes_auto_read", {"command": "td today --json"}),
                          bridged("toolu_git", "hermes_auto_read", {"command": "git log -3"}),
                          {"type": "tool_use", "id": "toolu_term", "name": "terminal", "input": {"command": "echo x"}},
                          bridged("toolu_term2", "terminal", {"command": "echo x"})], "tool_use")])
    job = world.job("one", deny_tools="research", triage={"reason": "test"})
    twin = world.job("two", deny_tools="research", triage={"reason": "test"})
    sid = world.hermes.create(job, "unused")
    world.hermes.create(twin, "unused")
    assert sid != job["session_id"]  # the gateway names the session; the reserved id was its creation key
    assert world.hermes.create(job, "unused") == sid  # same creation key, same session
    assert policy.read_registration(world.home, sid)["profile"] == "research"
    prompt = "FULL SYNTHETIC PROMPT with the third-party text"
    result = world.hermes.run({**job, "session_id": sid}, prompt, display="Synthetic card")
    assert result["type"] == "result" and result["exit_code"] == 0
    db = world.db()
    try:
        titles = sorted(row[0] for row in db.execute("SELECT title FROM sessions"))
        user = db.execute("SELECT content, api_content FROM messages WHERE session_id=? AND role='user' "
                          "ORDER BY id LIMIT 1", (sid,)).fetchone()
    finally:
        db.close()
    assert titles == ["Synthetic: review", "Synthetic: review (2)"]
    assert user["content"] == "Synthetic card" and prompt in (user["api_content"] or "")
    assert any(prompt in json.dumps(request) for request in world.seen)  # the model was sent the full prompt
    # A gui-source session gets the Desktop platform prompt (Markdown renders), not the terminal one.
    agent_turns = [request for request in world.seen if prompt in json.dumps(request.get("messages"))]
    assert agent_turns and all("inside the Hermes desktop app" in json.dumps(request.get("system"))
                               for request in agent_turns)
    sent = {tool["name"] for request in world.seen for tool in request.get("tools") or []}
    assert "terminal" not in sent and sent <= {"tool_search", "tool_describe", "tool_call", "hermes_auto_read",
                                                "hermes_auto_skill_view", "hermes_auto_skills_list", "session_search"}
    tools = world.tool_results(sid)
    assert tools["toolu_td"] == {"output": "synthetic td: today --json\n", "exit_code": 0}
    assert tools["toolu_list"]["success"] is True
    assert tools["toolu_skill"]["skipped"] is True and "no-such-skill" in tools["toolu_skill"]["note"]
    assert "only this profile" in str(tools["toolu_other"])
    assert tools["toolu_git"]["skipped"] is True and "error" not in tools["toolu_git"]
    assert "does not exist" in str(tools["toolu_term"]) and "Not run" in str(tools["toolu_term2"])


@CONTRACT
def test_without_the_guard_third_party_and_unguarded_full_runs_are_refused(make_world):
    world = make_world(guard_enabled=False, approvals_off=True)
    research = world.job("one", deny_tools="research", triage={"reason": "test"})
    mail = world.job("mail", deny_tools=True, triage={"reason": "test"})
    task = world.job("task", source="todoist", mode="agent")
    for job in (research, mail, task):
        with pytest.raises(RuntimeError, match="hermes-auto-guard"):
            world.hermes.create(job, "unused")
    db = world.db()
    try:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0  # nothing was created
    finally:
        db.close()
    # Bounded preparation needs no guard: its frozen toolsets are empty.
    draft = world.job("draft")
    sid = world.hermes.create(draft, "unused")
    assert world.hermes.run({**draft, "session_id": sid}, "Synthetic prompt")["exit_code"] == 0
    assert not any(request.get("tools") for request in world.seen if "Synthetic prompt" in json.dumps(request))


@CONTRACT
def test_full_run_refuses_approval_gated_calls_only_during_its_turn(make_world):
    """approvals.mode off would auto-approve them; the guard refuses them while the automatic turn runs."""
    turns = [([{"type": "tool_use", "id": "toolu_rm", "name": "terminal",
                "input": {"command": "rm -rf ~/hermes-auto-contract-does-not-exist"}},
               {"type": "tool_use", "id": "toolu_echo", "name": "terminal", "input": {"command": "echo synthetic-ok"}},
               {"type": "tool_use", "id": "toolu_code", "name": "execute_code", "input": {"code": "print(1)"}},
               {"type": "tool_use", "id": "toolu_rules", "name": "write_file",
                "input": {"path": "{root}/AGENTS.md", "content": "synthetic"}},
               {"type": "tool_use", "id": "toolu_notes", "name": "write_file",
                "input": {"path": "{root}/notes.txt", "content": "synthetic"}}],
              "tool_use")]
    world = make_world(turns, approvals_off=True)
    for block in turns[0][0]:  # the world's temporary root is known only now
        if block["name"] == "write_file":
            block["input"]["path"] = block["input"]["path"].format(root=world.root)
    task = world.job("task", source="todoist", mode="agent")
    sid = world.hermes.create(task, "unused")
    assert world.hermes.run({**task, "session_id": sid}, "Synthetic task")["exit_code"] == 0
    tools = world.tool_results(sid)
    assert "cannot ask the user" in str(tools["toolu_rm"]) and "cannot ask the user" in str(tools["toolu_code"])
    assert "synthetic-ok" in str(tools["toolu_echo"])
    # A protected instruction file needs an approval Hermes would grant itself with approvals off: refused.
    assert "cannot ask the user" in str(tools["toolu_rules"]) and not (world.root / "AGENTS.md").exists()
    assert (world.root / "notes.txt").read_text() == "synthetic"
    assert policy.read_registration(world.home, sid)["until"] == 0  # the user's own later turns are unrestricted
    sent = {tool["name"] for request in world.seen for tool in request.get("tools") or []}
    assert "terminal" in sent  # a full run keeps the profile's normal tools


@CONTRACT
def test_a_session_without_the_tool_policy_is_refused_with_a_new_session_hint(make_world):
    world = make_world()
    old = world.job("old", mode="open")
    world.hermes.create(old, "Synthetic old session")  # a session created without hermes-auto's policy
    job = world.job("one", deny_tools="research", triage={"reason": "test"})
    with pytest.raises(RuntimeError, match="new-session"):
        world.hermes.run({**job, "session_id": old["session_id"]}, "Synthetic prompt")
    with pytest.raises(RuntimeError, match="new-session"):
        world.hermes.create({**job, "session_id": old["session_id"]}, "Synthetic prompt")
    assert world.hermes.message_count(old) == 2  # nothing was sent


