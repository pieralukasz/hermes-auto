"""hermes-auto-guard: the Hermes plugin of the read-only inbox bots (second layer after the profile's toolsets)."""
import json
import os
from types import SimpleNamespace

import pytest

from hermes_auto import guard
from hermes_auto import guard_plugin as plugin
from hermes_auto.guard_plugin import policy


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A bot profile home with the plugin installed for mail reads (Google and Todoist)."""
    home = tmp_path / "hermes" / "profiles" / "inbox"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    guard.install(home, ["google", "todoist"])
    return home


def hook(name, args=None, session_id="s1"):
    return plugin.pre_tool_call(tool_name=name, args=args or {}, session_id=session_id, task_id="", turn_id="t1")


# ── Every session of the bot is read-only ────────────────────────────────────

@pytest.mark.parametrize("session_id", ["s1", "a-session-the-user-opened-by-hand"])
def test_every_session_of_the_bot_allows_only_the_read_tools(home, session_id):
    for name in ("hermes_auto_read", "hermes_auto_skill_view", "hermes_auto_skills_list", "session_search",
                 "tool_search", "tool_describe"):
        assert hook(name, session_id=session_id) is None, name
    assert hook("tool_call", {"name": "hermes_auto_read", "arguments": {"command": "td today"}}) is None
    for name in ("terminal", "execute_code", "delegate_task", "write_file", "patch", "skill_manage", "memory",
                 "web_search", "web_extract", "browser_navigate", "cronjob_manage", "send_message", "clarify",
                 "skill_view", "process_manage", ""):
        assert hook(name, session_id=session_id)["action"] == "block", name
    assert hook("tool_call", {"calls": [{"name": "hermes_auto_read"}, {"name": "terminal"}]})["action"] == "block"
    assert hook("tool_call", {"calls": "not json"})["action"] == "block"
    assert hook("tool_call", {})["action"] == "block"


def test_session_search_stays_in_this_profile(home):
    assert hook("session_search", {"query": "invoice"}) is None
    assert hook("session_search", {"session_id": "20261028_x", "around_message_id": 3}) is None
    assert hook("session_search", {"query": "secret", "profile": "default"})["action"] == "block"
    assert hook("session_search", {"session_id": "default/20261028_x"})["action"] == "block"
    # Through the bridge the real call is checked again by this hook once Hermes unwraps it.
    assert hook("tool_call", {"name": "session_search", "arguments": {"profile": "default"}}) is None


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


# ── The read tool: the profile's installed read families ─────────────────────

def fake_run(monkeypatch):
    calls = []
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: calls.append((argv, kw.get("env"))) or
                        SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    return calls


def test_the_read_tool_runs_only_the_profiles_read_families(home, monkeypatch):
    calls = fake_run(monkeypatch)
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id="s1")) == {"output": "ok", "exit_code": 0}
    assert json.loads(plugin.read_tool({"command": "td task delete id:1"}))["skipped"] is True
    assert json.loads(plugin.read_tool({"command": "gh pr view 1 --repo a/b"}))["skipped"] is True  # not installed
    assert [argv for argv, _ in calls] == [["td", "today"]]
    # The Google script finds the user's token in the root home, whatever home the gateway turn runs under.
    assert calls[0][1]["HERMES_HOME"] == os.path.expanduser("~/.hermes")


def test_a_work_bot_scope_reads_github_but_no_private_mail(home, monkeypatch):
    calls = fake_run(monkeypatch)
    guard.install(home, ["github", "linear", "slack"])
    assert json.loads(plugin.read_tool({"command": "gh pr view 1 --repo a/b"}))["exit_code"] == 0
    google = "python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail search x"
    assert json.loads(plugin.read_tool({"command": google}))["skipped"] is True
    assert json.loads(plugin.read_tool({"command": "td today"}))["skipped"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("scope", [None, "{not json", json.dumps({"version": 1, "reads": ["shell"]}),
                                   json.dumps({"version": 2, "reads": ["todoist"]})])
def test_without_a_valid_scope_every_read_is_refused(home, monkeypatch, scope):
    calls = fake_run(monkeypatch)
    path = home / "plugins" / policy.GUARD_NAME / policy.SCOPE_FILE
    if scope is None:
        path.unlink()
    else:
        path.write_text(scope)
    assert json.loads(plugin.read_tool({"command": "td today"}))["skipped"] is True
    assert calls == []


def test_the_scope_is_read_from_the_home_of_the_turn(tmp_path, monkeypatch):
    """One module may serve two bots in one gateway: each turn reads its own profile's scope."""
    calls = fake_run(monkeypatch)
    private, work = tmp_path / "profiles" / "inbox", tmp_path / "profiles" / "tapin-inbox"
    guard.install(private, ["todoist"])
    guard.install(work, ["github"])
    monkeypatch.setenv("HERMES_HOME", str(work))
    assert json.loads(plugin.read_tool({"command": "td today"}))["skipped"] is True
    monkeypatch.setenv("HERMES_HOME", str(private))
    assert json.loads(plugin.read_tool({"command": "td today"}))["exit_code"] == 0
    assert len(calls) == 1


def test_trusted_script_paths_are_expanded_without_a_shell():
    command = "python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail search 'from:a'"
    argv = policy.read_argv(command, "mail")
    assert argv[1] == os.path.expanduser("~/.hermes/skills/productivity/google-workspace/scripts/google_api.py")
    assert argv[2:] == ["gmail", "search", "from:a"]
    assert policy.read_argv(command.replace("gmail search", "gmail send"), "mail") is None


def test_the_slack_script_is_trusted_in_the_tapin_bots_skills():
    for path in ("~/.hermes/skills/tap-in/tapin-slack/scripts/slack_mcp.py",
                 "~/.hermes/profiles/tapin/skills/tap-in/tapin-slack/scripts/slack_mcp.py"):
        assert policy.read_argv(f"python3 {path} call slack_read_thread '{{}}'", ["slack"]) is not None, path
        assert policy.read_argv(f"python3 {path} call slack_send_message '{{}}'", ["slack"]) is None
        assert policy.read_argv(f"python3 {path} call slack_read_thread '{{}}'", ["github"]) is None
    assert policy.read_argv("python3 ~/.hermes/profiles/x/skills/tap-in/tapin-slack/scripts/slack_mcp.py call "
                            "slack_read_thread '{}'", ["slack"]) is None


def test_register_adds_the_toolset_and_the_hook():
    seen = {"tools": [], "hooks": []}
    ctx = SimpleNamespace(register_tool=lambda **kw: seen["tools"].append((kw["name"], kw["toolset"])),
                          register_hook=lambda name, callback: seen["hooks"].append(name))
    plugin.register(ctx)
    assert seen["tools"] == [(name, "hermes-auto") for name in policy.PLUGIN_TOOLS]
    assert seen["hooks"] == ["pre_tool_call"]


# ── Installation ─────────────────────────────────────────────────────────────

def test_install_copies_the_plugin_with_its_scope_and_reports_differences(tmp_path):
    hermes_home = tmp_path / "hermes"
    assert guard.differences(hermes_home) == list(guard.FILES)
    target = guard.install(hermes_home, ["todoist", "google"])
    assert sorted(path.name for path in target.iterdir()) == sorted(guard.FILES + (policy.SCOPE_FILE,))
    assert guard.differences(hermes_home, ["google", "todoist"]) == []
    assert guard.differences(hermes_home, ["google"]) == [policy.SCOPE_FILE]
    (target / "policy.py").write_text("changed")
    assert guard.differences(hermes_home) == ["policy.py"]
    guard.install(hermes_home, ["google"])
    assert guard.differences(hermes_home, ["google"]) == [] and not list(target.parent.glob(".hermes-auto-guard*"))
    with pytest.raises(ValueError):
        guard.install(hermes_home, ["shell"])


def test_plugin_manifest_version_matches_the_policy():
    from hermes_auto import bridge
    assert bridge.plugin_version() == policy.GUARD_VERSION
