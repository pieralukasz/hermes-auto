"""hermes-auto-guard: the Hermes plugin that checks automatic sessions in the gateway (second layer)."""
import json
import os
import sqlite3
import stat
import time
from types import SimpleNamespace

import pytest

from hermes_auto import guard
from hermes_auto import guard_plugin as plugin
from hermes_auto.guard_plugin import policy


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugin, "_dangerous", lambda command: command.startswith("rm -rf"))
    plugin._parents.clear()
    return home


def hook(name, args=None, session_id="s1"):
    return plugin.pre_tool_call(tool_name=name, args=args or {}, session_id=session_id, task_id="")


# ── Sessions hermes-auto did not register are never touched ─────────────────

@pytest.mark.parametrize("name", ["terminal", "execute_code", "write_file", "send_message"])
def test_the_users_own_sessions_are_untouched(home, name):
    assert hook(name, {"command": "rm -rf /"}) is None
    policy.write_registration(home, "other", "private")
    assert hook(name, {"command": "rm -rf /"}) is None


def test_an_unreadable_registry_never_blocks_the_users_sessions(home, monkeypatch):
    monkeypatch.setattr(policy, "read_registration", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    assert hook("terminal", {"command": "ls"}) is None


# ── Read profiles ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("profile", ["mail", "research"])
def test_read_profiles_allow_only_their_read_tools(home, profile):
    policy.write_registration(home, "s1", profile)
    for name in ("hermes_auto_read", "hermes_auto_skill_view", "hermes_auto_skills_list", "session_search",
                 "tool_search", "tool_describe"):
        assert hook(name) is None, name
    assert hook("tool_call", {"name": "hermes_auto_read", "arguments": {"command": "td today"}}) is None
    for name in ("terminal", "execute_code", "delegate_task", "write_file", "patch", "skill_manage", "memory",
                 "web_search", "web_extract", "browser_navigate", "cronjob_manage", "send_message", "clarify"):
        assert hook(name)["action"] == "block", name
    assert hook("tool_call", {"calls": [{"name": "hermes_auto_read"}, {"name": "terminal"}]})["action"] == "block"


def test_private_sessions_run_nothing_not_even_the_catalog(home):
    policy.write_registration(home, "s1", "private")
    for name in ("hermes_auto_read", "session_search", "tool_search", "tool_call"):
        assert hook(name, {"name": "hermes_auto_read"})["action"] == "block"


def test_a_damaged_registration_is_the_strictest_profile(home):
    path = policy.registration_path(home, "s1")
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as stream:
        stream.write("{not json")
    assert hook("hermes_auto_read")["action"] == "block"
    assert policy.read_registration(home, "s1")["profile"] == "private"


def test_registrations_are_private_files_with_safe_names(home):
    policy.write_registration(home, "local-abc_1.2", "mail", job=7)
    path = policy.registration_path(home, "local-abc_1.2")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(open(path).read()) == {"version": 1, "profile": "mail", "until": None, "job": 7}
    for bad in ("../x", "a/b", "", ".hidden", None):
        assert policy.registration_path(home, bad) is None
        with pytest.raises(ValueError):
            policy.write_registration(home, bad, "mail")
    with pytest.raises(ValueError):
        policy.write_registration(home, "s2", "admin")


# ── Full-tools runs: only what would need an approval, only during the automatic turn ──

def test_a_full_run_refuses_approval_gated_calls_while_it_runs(home):
    policy.write_registration(home, "s1", "full", until=time.time() + 600)
    assert hook("terminal", {"command": "rm -rf ~/x"})["action"] == "block"
    assert hook("execute_code", {"code": "import os"})["action"] == "block"
    assert hook("terminal", {"command": "ls ~"}) is None
    assert hook("write_file", {"path": "x"}) is None and hook("delegate_task") is None
    assert hook("terminal", {})["action"] == "block"  # no command to check: refused


def test_after_the_automatic_turn_the_user_keeps_normal_tools(home):
    policy.write_registration(home, "s1", "full", until=0)
    assert hook("terminal", {"command": "rm -rf ~/x"}) is None
    policy.write_registration(home, "s1", "full", until=time.time() - 1)
    assert hook("execute_code") is None


def test_a_delegated_child_or_compression_continuation_keeps_the_restrictions(home):
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
    db.executemany("INSERT INTO sessions VALUES (?,?)", [("s1", None), ("child", "s1"), ("grandchild", "child"),
                                                         ("unrelated", None)])
    db.commit()
    db.close()
    policy.write_registration(home, "s1", "full", until=time.time() + 600)
    assert hook("terminal", {"command": "rm -rf ~/x"}, session_id="grandchild")["action"] == "block"
    assert hook("terminal", {"command": "rm -rf ~/x"}, session_id="unrelated") is None
    policy.write_registration(home, "r1", "research")
    sqlite3.connect(home / "state.db").execute("INSERT INTO sessions VALUES ('r1-next', 'r1')").connection.commit()
    assert hook("terminal", {}, session_id="r1-next")["action"] == "block"


# ── The read tool and the skill tools ────────────────────────────────────────

def test_the_read_tool_runs_only_in_registered_read_sessions(home, monkeypatch):
    calls = []
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(
        returncode=0, stdout="ok", stderr=""))
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id="nobody"))["skipped"] is True
    policy.write_registration(home, "s1", "full", until=time.time() + 60)
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id="s1"))["skipped"] is True
    policy.write_registration(home, "s1", "mail")
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id="s1")) == {"output": "ok", "exit_code": 0}
    assert json.loads(plugin.read_tool({"command": "td task delete id:1"}, session_id="s1"))["skipped"] is True
    assert calls == [["td", "today"]]


def test_trusted_script_paths_are_expanded_without_a_shell(home):
    command = "python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail search 'from:a'"
    argv = policy.read_argv(command, "mail")
    assert argv[1] == os.path.expanduser("~/.hermes/skills/productivity/google-workspace/scripts/google_api.py")
    assert argv[2:] == ["gmail", "search", "from:a"]
    assert policy.read_argv(command.replace("gmail search", "gmail send"), "mail") is None


def test_skill_tools_refuse_outside_automatic_sessions(home):
    assert json.loads(plugin.skill_view_tool({"name": "x"}, session_id="nobody"))["skipped"] is True
    assert json.loads(plugin.skills_list_tool({}, session_id="nobody"))["skipped"] is True


def test_register_adds_the_toolset_and_the_hook():
    seen = {"tools": [], "hooks": []}
    ctx = SimpleNamespace(register_tool=lambda **kw: seen["tools"].append((kw["name"], kw["toolset"])),
                          register_hook=lambda name, callback: seen["hooks"].append(name))
    plugin.register(ctx)
    assert seen["tools"] == [(name, "hermes-auto") for name in policy.PLUGIN_TOOLS]
    assert seen["hooks"] == ["pre_tool_call"]


# ── Installation ─────────────────────────────────────────────────────────────

def test_install_copies_the_plugin_and_reports_differences(tmp_path):
    hermes_home = tmp_path / "hermes"
    assert guard.differences(hermes_home) == list(guard.FILES)
    target = guard.install(hermes_home)
    assert sorted(path.name for path in target.iterdir()) == sorted(guard.FILES)
    assert guard.differences(hermes_home) == []
    (target / "policy.py").write_text("changed")
    assert guard.differences(hermes_home) == ["policy.py"]
    guard.install(hermes_home)
    assert guard.differences(hermes_home) == [] and not list(target.parent.glob(".hermes-auto-guard*"))


def test_plugin_manifest_version_matches_the_policy():
    from hermes_auto import bridge
    assert bridge.plugin_version() == policy.GUARD_VERSION
