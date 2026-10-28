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


cwd = {}  # task id -> working directory the fake resolver uses


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugin, "_dangerous", lambda command: command.startswith("rm -rf"))
    monkeypatch.setattr(plugin, "_gated_write", lambda paths: any(path.endswith(".ssh/config") for path in paths))
    monkeypatch.setattr(plugin, "_resolver", lambda task_id: lambda path: str(cwd.get(task_id, home) / path)
                        if not str(path).startswith(("/", "~")) else str(path))
    plugin._parents.clear()
    plugin._latch.clear()
    return home


def hook(name, args=None, session_id="s1", turn_id="t1", task_id=""):
    return plugin.pre_tool_call(tool_name=name, args=args or {}, session_id=session_id, task_id=task_id,
                                turn_id=turn_id)


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


def test_a_full_run_refuses_gated_writes_desktop_control_and_changes_to_the_guard(home):
    policy.write_registration(home, "s1", "full", until=time.time() + 600)
    assert hook("write_file", {"path": "~/.ssh/config"})["action"] == "block"
    assert hook("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: notes.md\n*** Update File: "
                          "~/.ssh/config\n*** End Patch"})["action"] == "block"
    assert hook("computer_use", {"action": "click"})["action"] == "block"
    registry = policy.registration_path(home, "s1")
    for args in ({"path": registry}, {"path": str(home / "plugins" / "hermes-auto-guard" / "policy.py")}):
        assert "guard" in hook("write_file", args)["message"]
    assert "guard" in hook("terminal", {"command": f"echo x > {registry}"})["message"]
    assert hook("patch", {"path": "notes.md", "old_string": "a", "new_string": "b"}) is None


def test_relative_writes_and_terminal_workdirs_are_checked_where_they_land(home):
    policy.write_registration(home, "s1", "full", until=time.time() + 600)
    registry = os.path.dirname(policy.registration_path(home, "s1"))
    cwd["task"] = __import__("pathlib").Path(registry)
    assert "guard" in hook("write_file", {"path": "s1.json"}, task_id="task")["message"]
    assert "guard" in hook("terminal", {"command": "printf x > s1.json", "workdir": registry})["message"]
    cwd.clear()


def test_a_full_turn_stays_restricted_after_its_registration_is_rewritten(home):
    policy.write_registration(home, "s1", "full", until=time.time() + 600)
    assert hook("execute_code", turn_id="auto")["action"] == "block"
    policy.write_registration(home, "s1", "full", until=0)  # what a hostile run could do itself
    assert hook("execute_code", turn_id="auto")["action"] == "block"
    os.remove(policy.registration_path(home, "s1"))
    assert hook("execute_code", turn_id="auto")["action"] == "block"
    policy.write_registration(home, "s1", "full", until=0)
    assert hook("execute_code", turn_id="users-own-turn") is None  # the user's later turn follows the file


def test_gated_write_detector_failure_refuses_the_write(home, monkeypatch):
    policy.write_registration(home, "s1", "full", until=time.time() + 600)
    monkeypatch.setattr(plugin, "_gated_write", lambda paths: (_ for _ in ()).throw(ImportError("moved")))
    with pytest.raises(ImportError):  # Hermes turns a raising pre_tool_call into a refusal
        hook("write_file", {"path": "notes.md"})


@pytest.mark.parametrize("data", [{"profile": "full"}, {"version": 1, "profile": "full", "until": "broken"},
                                  {"version": 1, "profile": "full", "until": float("nan")},
                                  {"version": 1, "profile": "full", "until": True},
                                  {"version": 2, "profile": "full", "until": 0}, ["full"]])
def test_a_registration_not_in_the_written_shape_is_the_strictest_profile(home, data):
    path = policy.registration_path(home, "s1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as stream:
        stream.write(json.dumps(data))
    assert policy.read_registration(home, "s1")["profile"] == "private"
    assert hook("execute_code")["action"] == "block"


@pytest.mark.parametrize("profile", ["mail", "research"])
def test_session_search_stays_in_this_profile(home, profile):
    policy.write_registration(home, "s1", profile)
    assert hook("session_search", {"query": "invoice"}) is None
    assert hook("session_search", {"session_id": "20261028_x", "around_message_id": 3}) is None
    assert hook("session_search", {"query": "secret", "profile": "work"})["action"] == "block"
    assert hook("session_search", {"session_id": "work/20261028_x"})["action"] == "block"


def make_lineage(home, chain):
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
    db.executemany("INSERT INTO sessions VALUES (?,?)", list(zip(chain, [None] + chain[:-1])))
    db.commit()
    db.close()


def test_a_long_lineage_keeps_the_restrictions(home):
    chain = [f"c{index}" for index in range(12)]
    make_lineage(home, chain)
    policy.write_registration(home, "c0", "research")
    assert hook("terminal", {}, session_id="c11")["action"] == "block"


def test_an_unreadable_lineage_blocks_only_while_a_full_turn_may_be_ours(home):
    (home / "state.db").write_text("not a database")
    policy.write_registration(home, "r1", "research")
    assert hook("terminal", {"command": "ls"}, session_id="user-session") is None  # no full turn running
    policy.write_registration(home, "f1", "full", until=time.time() + 600)
    with pytest.raises(sqlite3.Error):  # a raising hook is a refusal in Hermes
        hook("terminal", {"command": "ls"}, session_id="maybe-a-child")


def test_skills_are_read_as_raw_text_inside_their_directory(tmp_path):
    skill = tmp_path / "skills" / "demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndeps: [ffmpeg]\n---\nRun !`date` now ${HERMES_SKILL_DIR}\n")
    (skill / "references" / "a.md").write_text("ref")
    (tmp_path / "secret.txt").write_text("secret")
    shown = json.loads(policy.read_skill_text(skill / "SKILL.md"))
    assert "!`date`" in shown["content"] and "${HERMES_SKILL_DIR}" in shown["content"]  # nothing rendered
    assert shown["linked_files"] == ["references/a.md"]
    assert json.loads(policy.read_skill_text(skill / "SKILL.md", "references/a.md"))["content"] == "ref"
    for bad in ("../secret.txt", str(tmp_path / "secret.txt"), "references/../../secret.txt", "missing.md"):
        assert json.loads(policy.read_skill_text(skill / "SKILL.md", bad))["skipped"] is True
    os.symlink(tmp_path / "secret.txt", skill / "references" / "link.md")
    assert json.loads(policy.read_skill_text(skill / "SKILL.md", "references/link.md"))["skipped"] is True


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
