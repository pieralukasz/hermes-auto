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


READ_ONLY_DISABLED = ["browser", "code_execution", "computer_use", "connections", "cronjob", "debugging", "delegation",
                      "file", "image_gen", "kanban", "memory", "project", "catalog", "onboarding", "skills", "terminal",
                      "tts", "video", "video_gen", "vision", "web", "search", "x_search", "bot_room", "todo", "clarify"]


def provider_yaml(port):
    return ("model:\n  default: test-model\n  provider: work-test\n"
            "providers:\n  work-test:\n    name: Work (test)\n"
            f"    api: http://127.0.0.1:{port}\n    transport: anthropic_messages\n"
            "    key_env: HERMES_AUTO_TEST_WORK_KEY\n    default_model: test-model\n    models: [test-model]\n")


class World:
    """A temporary multiplexed Hermes root (Dolores' default profile with approvals off, like the user's) with a
    fake model, one gateway (idle-exit) and, with `bots`, a read-only `inbox` bot (the guard plugin, reads:
    Todoist and GitHub) and a working bot `work` (approvals manual) that Todoist tasks go to."""

    def __init__(self, root, turns, *, bots=True, inbox_plugin=True, work_approvals="manual"):
        self.root, self.seen = root, []
        self.server = fake_anthropic(turns, self.seen)
        self.home = root / "hermes"
        self.home.mkdir(parents=True)
        # Dependency state is per Hermes root: borrow the installed one, as every Hermes process reads it.
        (self.home / "installs").symlink_to(Path.home() / ".hermes" / "installs")
        port = self.server.server_port
        (self.home / "config.yaml").write_text(provider_yaml(port) + "approvals:\n  mode: 'off'\n"
                                               "gateway:\n  multiplex_profiles: true\n")
        (self.home / ".env").write_text("HERMES_AUTO_TEST_WORK_KEY=synthetic-work-key\n")
        self.config = {**defaults(), "hermes_home": str(self.home),
                       "hermes_python": os.environ["HERMES_AUTO_TEST_PYTHON"],
                       "hermes_source": os.environ["HERMES_AUTO_TEST_SOURCE"],
                       "routes": [{"match": {"source": "example"}, "provider": "work-test", "model": "test-model"}]}
        if bots:
            inbox = self.profile("inbox", "platform_toolsets:\n  cli: [hermes-auto, session_search]\n"
                                 f"agent:\n  disabled_toolsets: {json.dumps(READ_ONLY_DISABLED)}\n"
                                 + ("plugins:\n  enabled: [hermes-auto-guard]\n" if inbox_plugin else "")
                                 + "approvals:\n  mode: manual\n"
                                 "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n"
                                 "auxiliary:\n  background_review:\n    enabled: false\n")
            guard.install(inbox, ["todoist", "github"])
            self.profile("work", f"approvals:\n  mode: '{work_approvals}'\n")
            self.config["bots"] = {"default": "default", "inbox": "inbox",
                                   "read_only": {"inbox": {"reads": ["github", "todoist"]}},
                                   "rules": [{"match": {"source": "todoist"}, "profile": "work"}]}
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "td").write_text("#!/bin/sh\necho \"synthetic td: $*\"\n")
        (bin_dir / "td").chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"
        self.store = Store(root, initialize=True)
        self.hermes = Hermes(self.config, root)

    def profile(self, name, extra):
        home = self.home / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(provider_yaml(self.server.server_port) + extra)
        (home / ".env").write_text("HERMES_AUTO_TEST_WORK_KEY=synthetic-work-key\n")
        return home

    def job(self, key, title="Synthetic: review", mode="draft", source="example", **payload):
        self.store.enqueue(key, source, key, title, mode, payload)
        self.store.db.commit()
        job = next(job for job in self.store.rows() if job["event_key"] == key)
        from hermes_auto.bots import choose
        return {**job, "profile": choose(job, self.config)}

    def db(self, profile="default"):
        db = sqlite3.connect(self.home / "state.db" if profile == "default" else
                             self.home / "profiles" / profile / "state.db")
        db.row_factory = sqlite3.Row
        return db

    def tool_results(self, sid, profile="default"):
        db = self.db(profile)
        try:
            return {row["tool_call_id"]: json.loads(row["content"]) if row["content"].startswith("{") else row["content"]
                    for row in db.execute("SELECT tool_call_id, content FROM messages WHERE session_id=? AND role='tool'",
                                          (sid,))}
        finally:
            db.close()

    def count(self, profile):
        path = self.home / "state.db" if profile == "default" else self.home / "profiles" / profile / "state.db"
        if not path.exists():
            return 0
        db = self.db(profile)
        try:
            return db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
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
    brief = world.job("brief", title="Brief", mode="open", source="brief")
    routed = world.job("routed", title="Routed", mode="open", source="brief", owner="acme")
    routed_example = world.job("routed2", title="Routed example", mode="open", source="example")
    assert brief["profile"] == "default" and routed_example["profile"] == "inbox"
    for _ in range(2):  # idempotent
        assert world.hermes.create(brief, "Synthetic test context") == brief["session_id"]
    world.hermes.create(routed, "Synthetic test context")
    world.hermes.create(routed_example, "Synthetic test context")
    info = world.hermes.invoke("inspect", brief)
    assert (info["exists"], info["messages"], info["archived"]) == (True, 2, False)
    assert world.hermes.invoke("inspect", brief, needles=["Synthetic test context"])["delivered"] is True
    assert world.hermes.invoke("inspect", brief, needles=["not delivered"])["delivered"] is False
    assert world.hermes.archive_states([(brief["session_id"], "default"), ("missing", "default"),
                                        (routed_example["session_id"], "inbox")]) == {
        brief["session_id"]: False, "missing": None, routed_example["session_id"]: False}
    assert world.hermes.inspect(routed_example["session_id"], profile="inbox")["exists"] is True
    assert world.hermes.inspect(routed_example["session_id"])["exists"] is False  # not in Dolores' profile
    assert world.hermes.find_session(subject="Synthetic subject", sent=[{"ts": 1.0, "to": []}]) is None
    db = world.db("inbox")
    try:
        rows = {row["id"]: row for row in db.execute("SELECT id, title, model, json_extract(model_config, "
                                                      "'$.provider') AS provider FROM sessions")}
    finally:
        db.close()
    assert rows[routed_example["session_id"]]["provider"] == "work-test"
    assert rows[routed_example["session_id"]]["model"] == "test-model"  # the route's model, not only its account


@CONTRACT
def test_the_inbox_bot_reads_through_the_guard_and_shows_the_card(make_world):
    """Real multiplexed gateway, fake model: the session lives in the read-only bot's profile, the transcript
    shows the card while the full prompt is the api_content; its tools are the guard's reads only; reads in the
    profile's scope run, anything else gets a neutral result."""
    world = make_world([([bridged("toolu_list", "hermes_auto_skills_list", {}),
                          bridged("toolu_skill", "hermes_auto_skill_view", {"name": "no-such-skill"}),
                          bridged("toolu_other", "session_search", {"query": "x", "profile": "default"}),
                          bridged("toolu_td", "hermes_auto_read", {"command": "td today --json"}),
                          bridged("toolu_google", "hermes_auto_read", {
                              "command": "python3 ~/.hermes/skills/productivity/google-workspace/scripts/"
                                         "google_api.py gmail search x"}),
                          bridged("toolu_git", "hermes_auto_read", {"command": "git log -3"}),
                          {"type": "tool_use", "id": "toolu_term", "name": "terminal", "input": {"command": "echo x"}},
                          bridged("toolu_term2", "terminal", {"command": "echo x"})], "tool_use")])
    doctor = world.hermes.doctor()
    assert doctor["bots"]["inbox"]["ready"] is True, doctor
    assert doctor["bots"]["work"]["ready"] is True and doctor["bots"]["default"]["ready"] is False
    job = world.job("one", deny_tools="research", triage={"reason": "test"})
    twin = world.job("two", deny_tools="research", triage={"reason": "test"})
    assert job["profile"] == "inbox"
    sid = world.hermes.create(job, "unused")
    world.hermes.create(twin, "unused")
    assert sid != job["session_id"]  # the gateway names the session; the reserved id was its creation key
    assert world.hermes.create(job, "unused") == sid  # same creation key, same session
    assert world.count("default") == 0  # nothing in Dolores' profile
    prompt = "FULL SYNTHETIC PROMPT with the third-party text"
    result = world.hermes.run({**job, "session_id": sid}, prompt, display="Synthetic card")
    assert result["type"] == "result" and result["exit_code"] == 0
    db = world.db("inbox")
    try:
        titles = sorted(row[0] for row in db.execute("SELECT title FROM sessions"))
        user = db.execute("SELECT content, api_content FROM messages WHERE session_id=? AND role='user' "
                          "ORDER BY id LIMIT 1", (sid,)).fetchone()
    finally:
        db.close()
    assert titles == ["Synthetic: review", "Synthetic: review (2)"]
    assert user["content"] == "Synthetic card" and prompt in (user["api_content"] or "")
    # A gui-source session gets the Desktop platform prompt (Markdown renders), not the terminal one, and the
    # bot's own SOUL/role, not Dolores'.
    agent_turns = [request for request in world.seen if prompt in json.dumps(request.get("messages"))]
    assert agent_turns and all("inside the Hermes desktop app" in json.dumps(request.get("system"))
                               for request in agent_turns)
    sent = {tool["name"] for request in world.seen for tool in request.get("tools") or []}
    assert "terminal" not in sent and sent <= {"tool_search", "tool_describe", "tool_call", "hermes_auto_read",
                                                "hermes_auto_skill_view", "hermes_auto_skills_list", "session_search"}
    tools = world.tool_results(sid, "inbox")
    assert tools["toolu_td"] == {"output": "synthetic td: today --json\n", "exit_code": 0}
    assert tools["toolu_list"]["success"] is True
    assert tools["toolu_skill"]["skipped"] is True and "no-such-skill" in tools["toolu_skill"]["note"]
    assert "only its own" in str(tools["toolu_other"])
    assert tools["toolu_google"]["skipped"] is True  # Google is not in this bot's read scope
    assert tools["toolu_git"]["skipped"] is True and "error" not in tools["toolu_git"]
    assert "does not exist" in str(tools["toolu_term"]) and "Not run" in str(tools["toolu_term2"])


@CONTRACT
def test_the_gateway_refuses_more_tools_than_the_bot_profile_allows(make_world):
    """The boundary is the profile: a launch asking the inbox bot for web search (bounded research of an
    unsorted item) is refused by the gateway itself, and a full-tools job never reaches a read-only bot."""
    world = make_world()
    research = world.job("web", mode="research")
    assert research["profile"] == "inbox"
    with pytest.raises(RuntimeError):
        world.hermes.create(research, "unused")
    assert "GatewayRPCError: invalid_params" in (world.root / "last-error.json").read_text()  # the gateway's
    draft = world.job("draft")  # the same launch without web (no tools at all) is accepted
    assert world.hermes.create(draft, "unused") and world.count("inbox") == 1
    with pytest.raises(RuntimeError, match="read-only bot"):
        world.hermes.create({**world.job("task", source="todoist", mode="agent"), "profile": "inbox"}, "unused")
    assert world.count("inbox") == 1


@CONTRACT
def test_without_a_ready_bot_third_party_and_unsafe_full_runs_are_refused(make_world):
    world = make_world(inbox_plugin=False, work_approvals="off")
    research = world.job("one", deny_tools="research", triage={"reason": "test"})
    mail = world.job("mail", deny_tools=True, triage={"reason": "test"})
    task = world.job("task", source="todoist", mode="agent")
    assert world.hermes.doctor()["bots"]["inbox"]["ready"] is False
    for job, why in ((research, "hermes-auto-guard"), (mail, "hermes-auto-guard"), (task, "approvals")):
        with pytest.raises(RuntimeError, match=why):
            world.hermes.create(job, "unused")
    # Dolores' profile (approvals off, no bots involved) gets neither third-party content nor automatic tasks.
    with pytest.raises(RuntimeError, match="read-only bot"):
        world.hermes.create({**mail, "profile": "default"}, "unused")
    with pytest.raises(RuntimeError, match="approvals"):
        world.hermes.create({**task, "profile": "default"}, "unused")
    assert (world.count("default"), world.count("inbox"), world.count("work")) == (0, 0, 0)  # nothing created


@CONTRACT
def test_a_task_runs_in_its_bot_where_hermes_denies_approval_gated_calls(make_world):
    """approvals.mode manual + a finite automatic turn: Hermes itself denies what would need an approval."""
    turns = [([{"type": "tool_use", "id": "toolu_rm", "name": "terminal",
                "input": {"command": "rm -rf ~/hermes-auto-contract-does-not-exist"}},
               {"type": "tool_use", "id": "toolu_echo", "name": "terminal", "input": {"command": "echo synthetic-ok"}}],
              "tool_use")]
    world = make_world(turns)
    task = world.job("task", source="todoist", mode="agent")
    assert task["profile"] == "work"
    sid = world.hermes.create(task, "unused")
    assert world.hermes.run({**task, "session_id": sid}, "Synthetic task")["exit_code"] == 0
    tools = world.tool_results(sid, "work")
    assert "synthetic-ok" in str(tools["toolu_echo"])
    assert "synthetic-ok" not in str(tools["toolu_rm"]) and any(
        word in str(tools["toolu_rm"]).lower() for word in ("block", "denied", "approval")), tools["toolu_rm"]
    sent = {tool["name"] for request in world.seen for tool in request.get("tools") or []}
    assert "terminal" in sent  # a full run keeps the bot's normal tools; the guard is not loaded there


@CONTRACT
def test_a_session_without_the_jobs_launch_is_refused_with_a_new_session_hint(make_world):
    world = make_world()
    old = world.job("old", mode="open")
    world.hermes.create(old, "Synthetic old session")  # a session created without hermes-auto's launch
    job = world.job("one", deny_tools="research", triage={"reason": "test"})
    with pytest.raises(RuntimeError, match="new-session"):
        world.hermes.run({**job, "session_id": old["session_id"]}, "Synthetic prompt")
    assert world.hermes.message_count(old) == 2  # nothing was sent
