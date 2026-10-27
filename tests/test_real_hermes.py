"""Opt-in contract check with installed Hermes, no network and no model calls."""
import http.server
import json
import os
import threading

import pytest

from hermes_auto.config import defaults
from hermes_auto.hermes import Hermes
from hermes_auto.store import Store


@pytest.mark.skipif(not os.environ.get("HERMES_AUTO_TEST_PYTHON"), reason="local Hermes runtime not configured")
def test_real_session_api_two_profiles(tmp_path):
    ids = []
    for name in ("profile-a", "profile-b", "profile-a"):
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        hermes_home = home / "hermes"
        hermes_home.mkdir(exist_ok=True)
        config = {**defaults(), "hermes_home": str(hermes_home),
                  "hermes_python": os.environ["HERMES_AUTO_TEST_PYTHON"],
                  "hermes_source": os.environ["HERMES_AUTO_TEST_SOURCE"],
                  "triage": {"hide_sessions": True}}
        store = Store(home, initialize=True)
        try:
            store.enqueue("example", "todoist", "x", "Integration test", "open", {})
            store.db.commit()
            job = store.rows()[0]
            hermes = Hermes(config, home)
            assert hermes.invoke("doctor")["ok"]
            hermes.create(job, "Synthetic test context")
            hermes.create(job, "Synthetic test context")
            info = hermes.invoke("inspect", job)
            assert (info["exists"], info["messages"], info["archived"]) == (True, 2, False)
            assert hermes.invoke("inspect", job, needles=["Synthetic test context"])["delivered"] is True
            assert hermes.invoke("inspect", job, needles=["not delivered"])["delivered"] is False
            assert hermes.archive_states([job["session_id"], "missing"]) == {
                job["session_id"]: False, "missing": None}
            assert hermes.find_session(subject="Synthetic subject", sent=[{"ts": 1.0, "to": []}]) is None
            ids.append(job["session_id"])
        finally:
            store.close()
    assert ids[0] == ids[2] and ids[0] != ids[1]


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


@pytest.mark.skipif(not os.environ.get("HERMES_AUTO_TEST_PYTHON"), reason="local Hermes runtime not configured")
def test_routed_session_row_records_its_provider(tmp_path):
    """Desktop resumes a session on `model_config.provider`: a routed job's row must carry the route's provider,
    or a reply in a work-account session runs on the profile's default account."""
    import sqlite3

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config = {**defaults(), "hermes_home": str(hermes_home),
              "hermes_python": os.environ["HERMES_AUTO_TEST_PYTHON"],
              "hermes_source": os.environ["HERMES_AUTO_TEST_SOURCE"],
              "routes": [{"match": {"source": "example", "owner": "acme"}, "provider": "work-test"}]}
    store = Store(tmp_path, initialize=True)
    try:
        store.enqueue("routed", "example", "r", "Routed", "open", {"owner": "acme"})
        store.enqueue("plain", "example", "p", "Plain", "open", {"owner": "other"})
        store.db.commit()
        routed, plain = store.rows()
        hermes = Hermes(config, tmp_path)
        hermes.create(routed, "Synthetic test context")
        hermes.create(plain, "Synthetic test context")
    finally:
        store.close()
    db = sqlite3.connect(hermes_home / "state.db")
    try:
        stored = dict(db.execute("SELECT id, json_extract(model_config, '$.provider') FROM sessions").fetchall())
    finally:
        db.close()
    assert stored[routed["session_id"]] == "work-test"
    assert stored[plain["session_id"]] is None



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


@pytest.mark.skipif(not os.environ.get("HERMES_AUTO_TEST_PYTHON"), reason="local Hermes runtime not configured")
def test_sorted_run_shows_the_card_and_skips_only_the_refused_call(tmp_path):
    """Real Hermes, local fake model: the transcript shows the card while the sent prompt is kept as api_content;
    in a batch of a read and a refused call, the read runs and the refused call gets a neutral result."""
    import sqlite3

    seen = []
    turns = [([{"type": "tool_use", "id": "toolu_read", "name": "skills_list", "input": {}},
               {"type": "tool_use", "id": "toolu_git", "name": "terminal", "input": {"command": "git log -3"}}],
              "tool_use")]
    server = fake_anthropic(turns, seen)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "providers:\n  work-test:\n    name: Work (test)\n"
        f"    api: http://127.0.0.1:{server.server_port}\n    transport: anthropic_messages\n"
        "    key_env: HERMES_AUTO_TEST_WORK_KEY\n    default_model: test-model\n    models: [test-model]\n")
    (hermes_home / ".env").write_text("HERMES_AUTO_TEST_WORK_KEY=synthetic-work-key\n")
    config = {**defaults(), "hermes_home": str(hermes_home),
              "hermes_python": os.environ["HERMES_AUTO_TEST_PYTHON"],
              "hermes_source": os.environ["HERMES_AUTO_TEST_SOURCE"],
              "routes": [{"match": {"source": "example"}, "provider": "work-test"}]}
    store = Store(tmp_path, initialize=True)
    try:
        for key in ("one", "two"):  # the same title twice: Hermes keeps titles unique
            store.enqueue(key, "example", key, "Synthetic: review", "draft",
                          {"deny_tools": "research", "triage": {"reason": "test"}})
        store.db.commit()
        job, twin = store.rows()
        hermes = Hermes(config, tmp_path)
        hermes.create(job, "unused")
        hermes.create(twin, "unused")
        prompt = "FULL SYNTHETIC PROMPT with the third-party text"
        hermes.run(job, prompt, display="Synthetic card")
    finally:
        store.close()
        server.shutdown()
    db = sqlite3.connect(hermes_home / "state.db")
    db.row_factory = sqlite3.Row
    try:
        titles = sorted(row[0] for row in db.execute("SELECT title FROM sessions"))
        user = db.execute("SELECT content, api_content FROM messages WHERE session_id=? AND role='user' "
                          "ORDER BY id LIMIT 1", (job["session_id"],)).fetchone()
        tools = {row["tool_call_id"]: json.loads(row["content"]) if row["content"].startswith("{") else row["content"]
                 for row in db.execute("SELECT tool_call_id, content FROM messages WHERE session_id=? AND role='tool'",
                                       (job["session_id"],))}
    finally:
        db.close()
    assert titles == ["Synthetic: review", "Synthetic: review (2)"]
    assert user["content"] == "Synthetic card" and prompt in (user["api_content"] or "")
    assert any(prompt in json.dumps(request) for request in seen)  # the model was sent the full prompt
    assert isinstance(tools["toolu_git"], dict) and tools["toolu_git"]["skipped"] is True
    assert "error" not in tools["toolu_git"]
    assert not (isinstance(tools["toolu_read"], dict) and tools["toolu_read"].get("skipped"))
