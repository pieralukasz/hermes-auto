"""Opt-in contract check with installed Hermes (>= v0.22.5: every turn runs in a gateway), no network, no real
model: a local fake Anthropic server answers. Every automatic session lives in the default profile; a third-party
session's first turn reads only (the guard's first_turn mode). Each test gets its own Hermes root OUTSIDE ~/.hermes (a home under
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

from hermes_auto import guard as guard_module
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
    """A local Anthropic Messages server answering each request with the next scripted turn (streamed). `turns`
    is a list (agent requests, those with tools, take the next entry) or a function request -> turn or None."""
    lock = threading.Lock()

    class Server(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            with lock:
                seen.append(request)
                # Only agent turns (with tools) take the script; model probes and side calls get plain text.
                if callable(turns):
                    turn = turns(request) if request.get("tools") else None
                else:
                    turn = turns.pop(0) if turns and request.get("tools") else None
            blocks, stop = turn or ([{"type": "text", "text": "Gotowe."}], "end_turn")
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




def provider_yaml(port):
    return ("model:\n  default: test-model\n  provider: work-test\n"
            "providers:\n  work-test:\n    name: Work (test)\n"
            f"    api: http://127.0.0.1:{port}\n    transport: anthropic_messages\n"
            "    key_env: HERMES_AUTO_TEST_WORK_KEY\n    default_model: test-model\n    models: [test-model]\n"
            "  work-two:\n    name: Second account (test)\n"
            f"    api: http://127.0.0.1:{port}\n    transport: anthropic_messages\n"
            "    key_env: HERMES_AUTO_TEST_WORK_KEY\n    default_model: test-model\n    models: [test-model]\n")


DOLORES_TOOLSETS = ["terminal", "file", "memory", "skills", "delegation", "session_search", "hermes-auto", "todo"]
# The first turn's reads: Todoist and GitHub on the profile's own account; the second account (`work-two`, chosen by
# the route for source "example") reads GitHub only and may not search earlier conversations.
FIRST_TURN = {"reads": ["github", "todoist"], "providers": {"work-two": {"reads": ["github"], "session_search": False}}}


class World:
    """A temporary multiplexed Hermes root with a fake model and one gateway (idle-exit), set up like the user's
    default profile (Dolores): approvals manual, the hermes-auto toolset on, the guard plugin enabled with the
    first_turn scope of `FIRST_TURN`. `guard=False` leaves the plugin disabled."""

    def __init__(self, root, turns, *, guard=True, approvals="manual"):
        self.root, self.seen = root, []
        self.server = fake_anthropic(turns, self.seen)
        self.home = root / "hermes"
        self.home.mkdir(parents=True)
        # Dependency state is per Hermes root: borrow the installed one, as every Hermes process reads it.
        (self.home / "installs").symlink_to(Path.home() / ".hermes" / "installs")
        port = self.server.server_port
        plugins = ["hermes-auto-guard"] if guard else []
        dolores = (f"approvals:\n  mode: '{approvals}'\n"
                   f"platform_toolsets:\n  cli: {json.dumps(DOLORES_TOOLSETS)}\n"
                   f"plugins:\n  enabled: {json.dumps(plugins)}\n"
                   "auxiliary:\n  background_review:\n    enabled: false\n")
        (self.home / "config.yaml").write_text(provider_yaml(port) + dolores + "gateway:\n  multiplex_profiles: true\n")
        (self.home / ".env").write_text("HERMES_AUTO_TEST_WORK_KEY=synthetic-work-key\n")
        guard_module.install(self.home, FIRST_TURN["reads"], FIRST_TURN["providers"])
        self.config = {**defaults(), "hermes_home": str(self.home),
                       "hermes_python": os.environ["HERMES_AUTO_TEST_PYTHON"],
                       "hermes_source": os.environ["HERMES_AUTO_TEST_SOURCE"],
                       "routes": [{"match": {"source": "example"}, "provider": "work-two", "model": "test-model"}],
                       "first_turn": FIRST_TURN}
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "td").write_text("#!/bin/sh\necho \"synthetic td: $*\"\n")
        (bin_dir / "td").chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"
        self.store = Store(root, initialize=True)
        self.hermes = Hermes(self.config, root)

    def job(self, key, title="Synthetic: review", mode="draft", source="example", profile="default", **payload):
        """A queued job; every new one is in the default profile (`profile` pins an older job's retired bot)."""
        self.store.enqueue(key, source, key, title, mode, payload)
        self.store.db.commit()
        job = next(job for job in self.store.rows() if job["event_key"] == key)
        return {**job, "profile": profile}

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

    def count(self):
        if not (self.home / "state.db").exists():
            return 0
        db = self.db()
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
        world = World(base / uuid.uuid4().hex[:12], turns if callable(turns) else list(turns), **options)
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


def call(request, call_id, name, arguments):
    """A tool call as the model would make it: direct when the tool is in the request, else through Tool Search."""
    names = {tool.get("name") for tool in request.get("tools") or []}
    if name in names:
        return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
    return bridged(call_id, name, arguments)


MARK = "<!-- hermes-auto:auto-turn -->"
GOOGLE = "python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail search x"


@CONTRACT
def test_open_sessions_are_imported_by_the_gateway_under_their_reserved_id(make_world):
    world = make_world()
    brief = world.job("brief", title="Plans", mode="open", source="questions")
    routed = world.job("routed", title="Routed", mode="open", source="questions", owner="acme")
    routed_example = world.job("routed2", title="Routed example", mode="open", source="example")
    for _ in range(2):  # idempotent
        assert world.hermes.create(brief, "Synthetic test context") == brief["session_id"]
    world.hermes.create(routed, "Synthetic test context")
    world.hermes.create(routed_example, "Synthetic test context")
    info = world.hermes.invoke("inspect", brief)
    assert (info["exists"], info["messages"], info["archived"]) == (True, 2, False)
    assert world.hermes.invoke("inspect", brief, needles=["Synthetic test context"])["delivered"] is True
    assert world.hermes.invoke("inspect", brief, needles=["not delivered"])["delivered"] is False
    # A retired bot's profile that is gone (archived) answers "missing" without a bridge.
    assert world.hermes.archive_states([(brief["session_id"], "default"), ("missing", "default"),
                                        (routed_example["session_id"], "default"), ("old", "inbox")]) == {
        brief["session_id"]: False, "missing": None, routed_example["session_id"]: False, "old": None}
    assert world.hermes.inspect(routed_example["session_id"])["exists"] is True
    assert world.hermes.inspect(routed_example["session_id"], profile="inbox")["exists"] is False
    registry = world.home / "hermes-auto-guard" / "sessions"
    assert sorted(path.name for path in registry.iterdir()) == [routed_example["session_id"]]
    assert world.hermes.find_session(subject="Synthetic subject", sent=[{"ts": 1.0, "to": []}]) is None
    db = world.db()
    try:
        rows = {row["id"]: row for row in db.execute("SELECT id, title, model, json_extract(model_config, "
                                                      "'$.provider') AS provider FROM sessions")}
        imported = db.execute("SELECT content FROM messages WHERE session_id=? AND role='user'",
                              (routed_example["session_id"],)).fetchone()[0]
    finally:
        db.close()
    assert imported.endswith(MARK)
    assert rows[routed_example["session_id"]]["provider"] == "work-two"
    assert rows[routed_example["session_id"]]["model"] == "test-model"  # the route's model, not only its account


@CONTRACT
def test_a_work_account_first_turn_reads_through_the_guard_and_shows_the_card(make_world):
    """Real multiplexed gateway, fake model: a third-party session on the work account lives in the default
    profile with its normal tools; the transcript shows the card while the full prompt is the api_content; its
    first turn reads only within the work account's scope, everything else gets a refusal."""
    world = make_world([([bridged("toolu_list", "hermes_auto_skills_list", {}),
                          bridged("toolu_skill", "hermes_auto_skill_view", {"name": "no-such-skill"}),
                          bridged("toolu_other", "session_search", {"query": "x", "profile": "default"}),
                          bridged("toolu_own", "session_search", {"query": "x"}),
                          bridged("toolu_td", "hermes_auto_read", {"command": "td today --json"}),
                          bridged("toolu_google", "hermes_auto_read", {"command": GOOGLE}),
                          bridged("toolu_git", "hermes_auto_read", {"command": "git log -3"}),
                          {"type": "tool_use", "id": "toolu_term", "name": "terminal", "input": {"command": "echo x"}},
                          bridged("toolu_term2", "terminal", {"command": "echo x"})], "tool_use")])
    doctor = world.hermes.doctor()
    assert doctor["ready"] is True and doctor["mode"] == "first_turn", doctor
    job = world.job("one", deny_tools="research", triage={"reason": "test"})
    twin = world.job("two", deny_tools="research", triage={"reason": "test"})
    sid = world.hermes.create(job, "unused")
    world.hermes.create(twin, "unused")
    assert sid != job["session_id"]  # the gateway names the session; the reserved id was its creation key
    assert world.hermes.create(job, "unused") == sid  # same creation key, same session
    assert (world.home / "hermes-auto-guard" / "sessions" / sid).is_file()
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
    assert user["content"] == "Synthetic card\n\n" + MARK and prompt in (user["api_content"] or "")
    # A gui-source session gets the Desktop platform prompt (Markdown renders), not the terminal one.
    agent_turns = [request for request in world.seen if prompt in json.dumps(request.get("messages"))]
    assert agent_turns and all("inside the Hermes desktop app" in json.dumps(request.get("system"))
                               for request in agent_turns)
    sent = {tool["name"] for request in world.seen for tool in request.get("tools") or []}
    assert "terminal" in sent  # the profile's normal tools: the user continues with them
    tools = world.tool_results(sid)
    # The work account reads GitHub only: no Todoist, no Google, no session_search.
    assert tools["toolu_td"].get("skipped") is True
    assert tools["toolu_list"]["success"] is True
    assert tools["toolu_skill"]["skipped"] is True and "no-such-skill" in tools["toolu_skill"]["note"]
    assert "not searchable" in str(tools["toolu_other"]) and "not searchable" in str(tools["toolu_own"])
    assert tools["toolu_google"]["skipped"] is True
    assert tools["toolu_git"]["skipped"] is True and "error" not in tools["toolu_git"]
    assert "Nie uruchomiono" in str(tools["toolu_term"]) and "Nie uruchomiono" in str(tools["toolu_term2"])


@CONTRACT
def test_a_private_matter_reads_with_the_private_scope(make_world):
    world = make_world([([bridged("toolu_td", "hermes_auto_read", {"command": "td today --json"}),
                          bridged("toolu_other", "session_search", {"query": "x", "profile": "default"}),
                          bridged("toolu_own", "session_search", {"query": "x"})], "tool_use")])
    job = world.job("mail", source="mailish", deny_tools=True, triage={"reason": "test"})
    sid = world.hermes.create(job, "unused")
    assert world.hermes.run({**job, "session_id": sid}, "Synthetic private matter")["exit_code"] == 0
    tools = world.tool_results(sid)
    assert tools["toolu_td"] == {"output": "synthetic td: today --json\n", "exit_code": 0}
    assert "only this profile" in str(tools["toolu_other"]) and "skipped" not in str(tools["toolu_own"])


AUTO_PROMPT = "AUTO-PROMPT: synthetic third-party matter"
LEAK = "SYNTHETIC-LEAK-CANARY.test"


@CONTRACT
@pytest.mark.parametrize("matter", [
    dict(source="mailish", deny_tools=True),
    # A private Slack channel is a matter like any other (Łukasz's decision): the same first turn.
    dict(source="slack", deny_tools="private", private=True, redacted=True),
])
def test_the_first_turn_only_reads_and_never_delegates(make_world, matter):
    """Real gateway, fake model: the automatic first turn tries terminal, memory, tool_search and delegate_task (all
    refused by the guard) and one read (runs)."""
    def route(request):
        text = json.dumps(request.get("messages"), ensure_ascii=False)
        if AUTO_PROMPT not in text or '"tool_result"' in text:
            return None
        return ([call(request, "toolu_term", "terminal", {"command": "echo should-not-run"}),
                 call(request, "toolu_mem", "memory", {"action": "add", "target": "memory", "content": "x"}),
                 call(request, "toolu_search", "tool_search", {"queries": [LEAK]}),
                 call(request, "toolu_sub", "delegate_task", {"goal": "przygotuj sprawę", "context": "dane"}),
                 call(request, "toolu_td", "hermes_auto_read", {"command": "td today --json"})], "tool_use")

    world = make_world(route)
    job = world.job("matter", triage={"reason": "test"}, **matter)
    sid = world.hermes.create(job, "unused")
    assert (world.home / "hermes-auto-guard" / "sessions" / sid).is_file()
    result = world.hermes.run({**job, "session_id": sid}, AUTO_PROMPT, display="Synthetic card")
    assert result["type"] == "result" and result["exit_code"] == 0
    tools = world.tool_results(sid)
    for refused in ("toolu_term", "toolu_mem", "toolu_search", "toolu_sub"):
        assert "Nie uruchomiono" in str(tools[refused]), (refused, tools[refused])
    assert "should-not-run" not in json.dumps(tools)
    assert tools["toolu_td"] == {"output": "synthetic td: today --json\n", "exit_code": 0}
    db = world.db()
    try:
        children = db.execute("SELECT COUNT(*) FROM sessions WHERE parent_session_id=?", (sid,)).fetchone()[0]
    finally:
        db.close()
    assert children == 0  # no subagent


@CONTRACT
def test_without_the_guard_or_with_unsafe_approvals_nothing_is_created(make_world):
    world = make_world(guard=False, approvals="off")
    research = world.job("one", deny_tools="research", triage={"reason": "test"})
    opened = world.job("opened", mode="open", source="mailish", deny_tools=True)  # someone's mail opened, no run
    task = world.job("task", source="todoist", mode="agent")
    doctor = world.hermes.doctor()
    assert doctor["ready"] is False and doctor["ok"] is False
    for job, why in ((research, "hermes-auto-guard"), (opened, "hermes-auto-guard"), (task, "approvals")):
        with pytest.raises(RuntimeError, match=why):
            world.hermes.create(job, "unused")
    # An older job pinned to a retired bot's profile is never created there.
    with pytest.raises(RuntimeError, match="retired profile"):
        world.hermes.create({**task, "profile": "tapin"}, "unused")
    (world.home / "config.yaml").write_text("approvals: [unclosed\n")
    with pytest.raises(RuntimeError, match="cannot be read"):
        world.hermes.create(world.job("task2", source="todoist", mode="agent"), "unused")
    assert world.count() == 0  # nothing created


@CONTRACT
def test_a_task_runs_with_full_tools_where_hermes_denies_approval_gated_calls(make_world):
    """approvals.mode manual + a finite automatic turn: Hermes itself denies what would need an approval."""
    turns = [([{"type": "tool_use", "id": "toolu_rm", "name": "terminal",
                "input": {"command": "rm -rf ~/hermes-auto-contract-does-not-exist"}},
               {"type": "tool_use", "id": "toolu_echo", "name": "terminal", "input": {"command": "echo synthetic-ok"}}],
              "tool_use")]
    world = make_world(turns)
    task = world.job("task", source="todoist", mode="agent")
    sid = world.hermes.create(task, "unused")
    assert not (world.home / "hermes-auto-guard" / "sessions").exists()  # the user's own task: not registered
    assert world.hermes.run({**task, "session_id": sid}, "Synthetic task")["exit_code"] == 0
    tools = world.tool_results(sid)
    assert "synthetic-ok" in str(tools["toolu_echo"])
    assert "synthetic-ok" not in str(tools["toolu_rm"]) and any(
        word in str(tools["toolu_rm"]).lower() for word in ("block", "denied", "approval")), tools["toolu_rm"]


@CONTRACT
def test_a_session_without_the_jobs_launch_is_refused_with_a_new_session_hint(make_world):
    world = make_world()
    old = world.job("old", mode="open")
    world.hermes.create(old, "Synthetic old session")  # a session created without hermes-auto's launch
    job = world.job("one", deny_tools="research", triage={"reason": "test"})
    with pytest.raises(RuntimeError, match="new-session"):
        world.hermes.run({**job, "session_id": old["session_id"]}, "Synthetic prompt")
    assert world.hermes.message_count(old) == 2  # nothing was sent
