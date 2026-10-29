"""Codex review of the Dolores delegator: each confirmed finding as a test of the fixed behaviour.

1. (Changed by Łukasz) A private Slack channel's matter is an ordinary delegation: DELEGATE, registered, first_turn.
2. tool_search can send its queries to remote connectors (Hermes, `connections_in_scope`): refused in first_turn and
   read_only; tool_describe only for the mode's own (non-connector) tools.
3. A registry or state.db that cannot be read refuses (fail-closed), also for a compression continuation.
4. An incomplete compression lineage (over the hop limit, a cycle) refuses instead of passing as an ordinary session.
5. Dolores' readiness needs the advanced-delegation plugin: enabled, its files on disk, loaded by the gateway.
"""
# ruff: noqa: F811 - the fixtures `dolores` and `json_settings` are imported from test_dolores_delegator
import ast
import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_auto import bridge, guard
from hermes_auto import guard_plugin as plugin
from hermes_auto.guard_plugin import policy
from hermes_auto.runner import prompt_for
from test_dolores_delegator import (FakeClient, SID, config, delegation, dolores, dolores_config,  # noqa: F401
                                    hook, job, json_settings, say, write_dolores)

PRIVATE_SID = "20261029_093000_private"
SECRET = "SYNTHETIC-PRIVATE-CONTENT.test"


def add_session(home, sid, parent=None, compressed_parent=True):
    db = sqlite3.connect(home / "state.db")
    if parent and compressed_parent:
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id=?", (parent,))
    db.execute("INSERT INTO sessions VALUES (?, ?, NULL, 'gui')", (sid, parent))
    db.commit()
    db.close()


def is_block(result, needle=None):
    return bool(result) and result["action"] == "block" and (needle is None or needle in result["message"])


# ── 1. A private matter is delegated like any other (Łukasz's decision) ────

def test_a_private_matter_in_dolores_is_an_ordinary_delegation():
    profile, params = bridge.launch_plan({"deny_tools": "private", "full_tools": False, "job": {"mode": "draft"},
                                          "route": {}}, dolores_config())
    assert profile == policy.DELEGATE and "toolsets" not in params
    assert bridge.third_party(profile, {"source": "slack"}, {})
    # Without bots (no subagent) the old launch stays: no tools at all.
    assert bridge.launch_plan({"deny_tools": "private", "job": {"mode": "draft"}}, {**config(), "bot": None}) == (
        policy.PRIVATE, {"max_turns": config()["max_turns"], "run_budget": config()["run_budget_seconds"],
                         "toolsets": []})


class GatewayClient:
    """A gateway as the bridge sees it: the creation contract and the session."""

    def __init__(self):
        self.calls = []

    async def rpc(self, method, **fields):
        self.calls.append((method, fields))
        if method == "runtime.describe":
            return {"session_create": {"sources": ["gui"], "parameters": [
                "toolsets", "max_turns", "run_budget", "provider", "model", "cwd", "ignore_rules"]}}
        if method == "session.create":
            return {"stored_session_id": PRIVATE_SID}
        return {}


def test_a_private_matter_is_created_as_a_registered_first_turn_session(dolores, monkeypatch):
    cfg = dolores_config(dolores)
    client = GatewayClient()
    monkeypatch.setattr(bridge, "run_gateway", lambda work: asyncio.run(work(client)))

    async def ready(client, config):
        return [], {"ok": True}, {"ok": True}
    monkeypatch.setattr(bridge, "delegator_report", ready)

    class DB:
        def get_session(self, sid):
            return {"title": "Slack · Bob"}
    request = {"deny_tools": "private", "full_tools": False, "job": {"mode": "draft", "source": "slack"},
               "route": {}, "title": "Slack · Bob"}
    sid = bridge.create_agent_session(request, cfg, {"id": 9, "session_id": "reserved-9", "source": "slack"}, DB())
    assert sid == PRIVATE_SID and "toolsets" not in dict(client.calls)["session.create"]
    assert policy.is_registered(dolores, sid)
    add_session(dolores, sid)
    say(dolores, "Slack · Bob\n" + policy.AUTO_TURN_MARK, sid=sid)
    # The first_turn lock: the hand-off to inbox runs, nothing else.
    assert hook("delegate_task", delegation("inbox"), session_id=sid) is None
    assert is_block(hook("write_file", session_id=sid), "Łukasza")
    assert is_block(hook("delegate_task", delegation("tapin"), session_id=sid), "Łukasza")


# ── 2. tool_search is not a local catalog read ──────────────────────────────

def test_the_first_turn_refuses_tool_search_and_remote_describes(dolores):
    assert is_block(hook("tool_search", {"queries": [SECRET]}))
    assert is_block(hook("tool_call", {"name": "tool_search", "arguments": {"queries": [SECRET]}}))
    for names in (["connectors__gmail_send_email"], ["delegate_task", "connectors__x_y"], ["terminal"], [], "",
                  None, {"a": 1}):
        assert is_block(hook("tool_describe", {"names": names})), names
    for names in (["delegate_task"], "delegate_task", ["session_search", "hermes_auto_skill_view"]):
        assert hook("tool_describe", {"names": names}) is None, names
    # The hand-off itself needs no catalog: direct, or through tool_call with the exact name.
    assert hook("delegate_task", delegation("inbox")) is None
    assert hook("tool_call", {"name": "delegate_task", "arguments": delegation("inbox")}) is None


def test_a_read_only_bot_refuses_tool_search_and_remote_describes(tmp_path, monkeypatch):
    home = tmp_path / "inbox"
    guard.install(home, ["todoist"])
    monkeypatch.setenv("HERMES_HOME", str(home))
    for sid in ("s1", "20261029_090500_child"):  # its own session and Dolores' subagent alike
        assert is_block(hook("tool_search", {"queries": [SECRET]}, session_id=sid))
        assert is_block(hook("tool_describe", {"names": ["connectors__slack_post"]}, session_id=sid))
        assert is_block(hook("tool_describe", {"names": ["terminal"]}, session_id=sid))
        assert hook("tool_describe", {"names": ["hermes_auto_read"]}, session_id=sid) is None
        assert hook("tool_call", {"name": "hermes_auto_read", "arguments": {"command": "td today"}},
                    session_id=sid) is None


def test_prompts_and_tool_descriptions_never_ask_for_tool_search():
    mail = {"triage": {"reason": "r"}, "deny_tools": True, "from": "Ann <ann@example.test>", "subject": "S",
            "body": "b"}
    items = [job("gmail", **mail),
             job("slack", kind="slack", why="mention", place="#dev", body="?", deny_tools="research",
                 triage={"reason": "r"}),
             job("github", kind="github", why="review_requested", place="example-org/app#1", body="PR",
                 deny_tools="research", triage={"reason": "r"}),
             job("slack", kind="slack", why="channel", place="#secret", redacted=True, deny_tools="private",
                 triage={"reason": "r"})]
    texts = [prompt_for(item, config()) for item in items]
    texts += [plugin.READ_SCHEMA["description"], plugin.SKILL_VIEW_SCHEMA["description"],
              plugin.SKILLS_LIST_SCHEMA["description"], policy.REFUSED_READ, policy.REFUSED_FIRST_TURN]
    assert all("tool_search" not in text for text in texts)
    goal = texts[0].split("CEL", 1)[1]
    assert "tool_call" in goal and "hermes_auto_read" in goal  # how the subagent reaches a deferred read tool


HERMES_SOURCE = Path(os.environ.get("HERMES_AUTO_TEST_SOURCE") or "/nonexistent").expanduser()


@pytest.mark.skipif(not (HERMES_SOURCE / "tools" / "connectors" / "search.py").is_file(),
                    reason="HERMES_AUTO_TEST_SOURCE (Hermes source) not set")
def test_every_describe_the_guard_allows_stays_local_in_hermes():
    """Hermes' own remote_schemas_for (unchanged body), with manage_connections in scope: no name the guard lets
    tool_describe load reaches the connector service."""
    def load(path, names):
        tree = ast.parse(path.read_text())
        body: list[ast.stmt] = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        assert len(body) == len(names)
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        return compile(ast.fix_missing_locations(ast.Module(body=[future, *body], type_ignores=[])), str(path), "exec")
    namespace = {"functools": __import__("functools"), "UNREACHABLE": "unreachable",
                 "logger": __import__("logging").getLogger("synthetic"), "CONNECTOR_NAME_PREFIX": "connectors__",
                 "_fn": lambda td: td.get("function", td)}
    exec(load(HERMES_SOURCE / "tools/connectors/gateway/names.py", {"is_connector_name"}), namespace)
    exec(load(HERMES_SOURCE / "tools/connectors/search.py", {"remote_schemas_for", "connections_in_scope"}),
         namespace)
    remote = []

    def describe(names):
        remote.append(names)
        raise AssertionError("remote describe")
    in_scope = [{"type": "function", "function": {"name": "manage_connections"}}]
    allowed = [name for name in sorted(policy.FIRST_TURN_DESCRIBE | policy.ALLOWED_TOOLS)
               if policy.decide_first_turn("tool_describe", {"names": [name]}, "inbox") is None
               or policy.decide("tool_describe", {"names": [name]}) is None]
    assert allowed
    assert namespace["remote_schemas_for"](allowed, in_scope, describe) == ({}, None)
    assert remote == []
    # The same body does go remote for a connector name: the check above is not vacuous.
    namespace["remote_schemas_for"](["connectors__gmail_send"], in_scope, describe)
    assert remote == [["connectors__gmail_send"]]


# ── 3. Unreadable state refuses ─────────────────────────────────────────────

UNKNOWN = "nie da się ustalić"


def test_a_registry_that_cannot_be_listed_refuses(dolores, monkeypatch):
    def unavailable(path):
        raise PermissionError("synthetic fault")
    monkeypatch.setattr(policy.os, "listdir", unavailable)
    for sid in (SID, "20261029_100000_byhand"):
        assert is_block(hook("write_file", session_id=sid), UNKNOWN), sid
        assert is_block(hook("delegate_task", delegation("inbox"), session_id=sid)), sid


def test_without_a_registry_ordinary_sessions_pass(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    guard.install(home, [], mode=policy.FIRST_TURN_MODE, subagent="inbox")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert hook("write_file", session_id="20261029_100000_byhand") is None  # nothing registered yet, no state.db


def test_an_unreadable_database_refuses_a_continuation_and_unknown_sessions(dolores):
    add_session(dolores, "tip-1", SID)
    assert is_block(hook("write_file", session_id="tip-1"), "Łukasza")
    (dolores / "state.db").write_text("synthetic corruption")
    assert is_block(hook("write_file", session_id="tip-1"), UNKNOWN)
    assert is_block(hook("write_file", session_id="20261029_100000_byhand"), UNKNOWN)
    # The registered session itself keeps its first-turn rules (it is known to be automatic).
    assert is_block(hook("write_file"), "Łukasza")
    assert hook("delegate_task", delegation("inbox")) is None


def test_a_failing_lineage_query_refuses(dolores):
    db = sqlite3.connect(dolores / "state.db")
    db.execute("ALTER TABLE sessions RENAME TO sessions_old")
    db.commit()
    db.close()
    assert is_block(hook("write_file", session_id="tip-1"), UNKNOWN)
    assert is_block(hook("write_file", session_id="20261029_100000_byhand"), UNKNOWN)


def test_a_missing_database_with_registered_sessions_refuses_unknown_ones(dolores):
    (dolores / "state.db").unlink()
    assert is_block(hook("write_file", session_id="20261029_100000_byhand"), UNKNOWN)
    assert is_block(hook("write_file"), "Łukasza")


# ── 4. Incomplete lineage refuses ───────────────────────────────────────────

def chain(home, root, length, prefix):
    parent = root
    for n in range(1, length + 1):
        add_session(home, f"{prefix}-{n}", parent)
        parent = f"{prefix}-{n}"
    return parent


def test_a_long_registered_lineage_stays_locked(dolores):
    # Codex review: a 64-hop cap once unlocked the 65th continuation. The walk now reads the whole lineage.
    chain(dolores, SID, 200, "child")
    assert is_block(hook("write_file", session_id="child-65"), "Łukasza")
    assert is_block(hook("write_file", session_id="child-200"), "Łukasza")
    assert hook("delegate_task", delegation("inbox"), session_id="child-200") is None
    say(dolores, "Napisz do niej", sid="child-30")  # a person wrote in the lineage: open from there on
    assert hook("write_file", session_id="child-200") is None


def test_a_long_ordinary_lineage_is_never_refused(dolores):
    # A long-lived manual chat compressed many times is an ordinary session, not an unknown one.
    add_session(dolores, "own-root")
    chain(dolores, "own-root", 200, "own")
    assert hook("write_file", session_id="own-65") is None
    assert hook("write_file", session_id="own-200") is None


def test_a_lineage_beyond_the_safety_bound_refuses(dolores, monkeypatch):
    monkeypatch.setattr(policy, "COMPRESSION_HOPS", 5)
    add_session(dolores, "own-root")
    chain(dolores, "own-root", 7, "own")
    assert hook("write_file", session_id="own-5") is None
    assert is_block(hook("write_file", session_id="own-7"), UNKNOWN)


def test_a_lineage_cycle_refuses(dolores):
    db = sqlite3.connect(dolores / "state.db")
    db.executemany("INSERT INTO sessions VALUES (?, ?, 'compression', 'gui')",
                   [("loop-a", "loop-b"), ("loop-b", "loop-a")])
    db.execute("INSERT INTO sessions VALUES ('loop-tip', 'loop-a', NULL, 'gui')")
    db.execute("INSERT INTO sessions VALUES ('self', 'self', 'compression', 'gui')")
    db.commit()
    db.close()
    for sid in ("loop-tip", "loop-a", "self"):
        assert is_block(hook("write_file", session_id=sid), UNKNOWN), sid


def test_a_missing_middle_of_a_registered_lineage_refuses(dolores):
    # Codex review round 2, finding 6: a dangling parent pointer used to end the walk as a complete lineage.
    chain(dolores, SID, 3, "cut")
    assert is_block(hook("write_file", session_id="cut-3"), "Łukasza")
    db = sqlite3.connect(dolores / "state.db")
    db.execute("DELETE FROM sessions WHERE id='cut-2'")
    db.commit()
    db.close()
    for tool in ("write_file", "memory", "skill_manage", "tool_search"):
        assert is_block(hook(tool, session_id="cut-3"), UNKNOWN), tool
    assert is_block(hook("delegate_task", delegation("work"), session_id="cut-3"), UNKNOWN)


def test_a_session_without_a_parent_or_not_yet_persisted_is_open(dolores):
    add_session(dolores, "solo")
    assert hook("write_file", session_id="solo") is None
    assert hook("write_file", session_id="not-in-the-db-yet") is None


# ── 5. Readiness needs the delegation backend ───────────────────────────────

def test_readiness_needs_the_advanced_delegation_plugin_on_disk_and_enabled(tmp_path, json_settings):
    home = tmp_path / "hermes"
    write_dolores(home)
    cfg = dolores_config(home)
    assert bridge.delegator_problems(cfg) == []
    settings = json.loads((home / "config.yaml").read_text())
    settings["plugins"]["enabled"] = ["hermes-auto-guard"]
    (home / "config.yaml").write_text(json.dumps(settings))
    assert any("advanced-delegation" in problem for problem in bridge.delegator_problems(cfg))
    for missing in ("__init__.py", "profile_routes.py"):
        write_dolores(home)
        (home / "plugins" / "advanced-delegation" / missing).unlink()
        assert any("advanced-delegation" in problem and missing in problem
                   for problem in bridge.delegator_problems(cfg)), missing
    # What was once left on the live system: only the bytecode cache.
    write_dolores(home)
    target = home / "plugins" / "advanced-delegation"
    for path in target.iterdir():
        path.unlink()
    (target / "__pycache__").mkdir()
    assert any("advanced-delegation" in problem for problem in bridge.delegator_problems(cfg))


@pytest.mark.parametrize("row, needle", [
    (None, "not loaded"),
    ({"enabled": False}, "not active"),
    ({"error": "ImportError: synthetic"}, "ImportError"),
    ({"commands": 0}, "not active"),
])
def test_readiness_needs_the_gateway_to_have_loaded_the_delegation_plugin(tmp_path, monkeypatch, json_settings,
                                                                          row, needle):
    home = tmp_path / "hermes"
    write_dolores(home)
    cfg = dolores_config(home)
    monkeypatch.setattr(bridge, "profile_problems", lambda sub: [])

    async def sub_status(sub):
        return {"ok": True, "loaded": True}
    monkeypatch.setattr(bridge, "subagent_guard_status", sub_status)
    assert asyncio.run(bridge.delegator_report(FakeClient(), cfg))[0] == []
    client = FakeClient()
    client.rows = [] if row is None else [{**client.rows[0], **row}]
    problems = asyncio.run(bridge.delegator_report(client, cfg))[0]
    assert any("advanced-delegation" in problem and needle in problem for problem in problems), problems
