"""Read-only command grammar and tool policy for unattended third-party turns."""
import json
import sys
import types
from types import SimpleNamespace

import pytest

from hermes_auto.bridge import deny_tool_execution, read_only_command

SLACK = "python3 ~/.hermes/skills/tap-in/tapin-slack/scripts/slack_mcp.py"
GOOGLE = "python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    return tmp_path


@pytest.mark.parametrize("command", [
    # Attached short write flags in gh.
    "gh api -XPOST repos/acme/web/issues",
    "gh api -fquery=x graphql",
    "gh api -Fbody=@/etc/passwd repos/acme/web/issues",
    "gh api --method=POST repos/acme/web/issues",
    "gh api --input=/etc/passwd repos/acme/web/issues",
    "gh api -HX-HTTP-Method-Override:DELETE repos/acme/web",
    "gh pr view 12 -w",
    "gh issue list --web",
    # Pager and program flags in git.
    "git grep -Ovim useCart",
    "git grep --open-files-in-pager=sh useCart",
    "git -p log",
    "git --paginate log",
    "git -c core.pager=sh log",
    "git -ccore.pager=sh log",
    "git --exec-path=/tmp log",
    "git log --output=/tmp/x",
    "git show --ext-diff HEAD",
    # Arbitrary program and script paths.
    "/tmp/evil/git log",
    "/tmp/evil/gh pr view 12",
    "/tmp/td today",
    "/tmp/python ~/.hermes/skills/tap-in/tapin-slack/scripts/slack_mcp.py call slack_read_thread '{}'",
    "python3 /tmp/x/skills/tap-in/tapin-slack/scripts/slack_mcp.py call slack_read_thread '{}'",
    "python3 '~/.hermes/skills/tap-in/tapin-slack/scripts/slack_mcp.py' call slack_read_thread '{}'",
    "python3 ~/.hermes/skills/x/../tap-in/tapin-slack/scripts/slack_mcp.py call slack_read_thread '{}'",
    "python /tmp/x/skills/productivity/google-workspace/scripts/google_api.py calendar list",
    # git fetch writes refs and objects and contacts arbitrary remotes.
    "git fetch origin",
    "git -C ~/Projects/web fetch origin",
    "git fetch --prune",
    "git ls-remote ext::sh",
    # Local files outside a repository.
    "git diff --no-index /etc/passwd /dev/null",
    "git grep --no-index secret",
    "git diff /etc/passwd /dev/null",
    "git -C ~/.ssh diff id_rsa id_rsa.pub",
    "git show HEAD -- /etc/passwd",
    "git log -- ../../.ssh",
    "git -C relative/dir log",
    "git blame --contents=/etc/passwd README.md",
    "git grep -f /etc/passwd",
])
def test_research_profile_refuses_unsafe_commands(command):
    assert read_only_command(command, "research") is False


@pytest.mark.parametrize("command", [
    "gh pr view 12 --repo acme/web --json state,title",
    "gh repo view acme/web",
    "gh repo view https://github.com/acme/web",
    "gh pr list -R acme/web --state open --limit 5",
    "gh issue view 7 --comments",
    "gh pr diff 12",
    "gh api repos/acme/web/pulls/12",
    "gh run view 99 --log-failed",
    f"{SLACK} call slack_read_thread '{{\"channel_id\":\"C1\",\"message_ts\":\"1.0\"}}'",
    "linear issue view WEB-12 --no-download",
    "linear api 'query { viewer { id } }'",
])
def test_research_profile_keeps_common_reads(command):
    assert read_only_command(command, "research") is True


@pytest.mark.parametrize("command", [
    "gh api graphql",
    "gh api https://evil.test/x",
    "gh repo view data.evil.test/acme/web",
    "gh pr view https://evil.test/acme/web/pull/1",
    "gh pr view 12 --repo evil.test/acme/web",
    "gh pr view 12 --jq .title",
    "gh pr list -L5",
    "gh run list -w deploy",
    "gh pr checks 12 --watch",
    "gh release download v1",
    "git branch evil",
    "git log -n",
    "git status",
    "linear issue view WEB-12 --app",
    "linear issue view WEB-12 -a",
    "linear api 'query { a }' --variable x=1",
    f"{SLACK} call slack_send_message '{{}}'",
    f"{SLACK} call slack_read_thread '{{}}' extra",
])
def test_research_profile_refuses_ambiguous_options(command):
    assert read_only_command(command, "research") is False


def test_trusted_paths_follow_hermes_home(home, monkeypatch):
    runtime = home / "runtime"
    monkeypatch.setenv("HERMES_HOME", str(runtime))
    script = runtime / "skills/tap-in/tapin-slack/scripts/slack_mcp.py"
    assert read_only_command(f"python3 {script} call slack_read_thread '{{}}'", "research") is True
    absolute = home / ".hermes/skills/productivity/google-workspace/scripts/google_api.py"
    assert read_only_command(f"python {absolute} calendar list", True) is True
    venv = home / ".hermes/hermes-agent/venv/bin/python"
    assert read_only_command(f"{venv} {absolute} gmail search 'from:a'", True) is True


@pytest.mark.parametrize("command", [
    "td today",
    "/opt/homebrew/bin/td task view id:6hq2 --full --json",
    f"{GOOGLE} calendar list --start 2026-10-26T00:00:00Z",
    "git log --oneline",
    "gh pr view 12",
    f"{SLACK} call slack_read_thread '{{}}'",
])
def test_private_profile_allows_no_command(command):
    assert read_only_command(command, "private") is False


# ── Tool policy at the batch boundary, with a fake agent ─────────────────────

@pytest.fixture
def agent_class(monkeypatch):
    errors = []

    def append(messages, calls, payload):
        for call in calls:
            result = json.loads(payload(call))
            # A refused call is a neutral "not run" result: no `error` key (Desktop would paint it red).
            assert result["skipped"] is True and "error" not in result
            errors.append((call.function.name, result["note"]))

    module = types.ModuleType("agent.turn_tool_validation")
    module._append_tool_error_results = append
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.turn_tool_validation", module)

    class Agent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def _execute_tool_calls(self, message, messages, *args, **kwargs):
            return "executed"

    Agent.errors = errors
    return Agent


def batch(*calls):
    return SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments))) for name, arguments in calls])


WRAPPERS = [("tool_call", {"calls": [{"name": "terminal", "arguments": {"command": "td today"}}]}),
            ("execute_code", {"code": "print(1)"}),
            ("delegate_task", {"goal": "send the reply"}),
            ("terminal", {"command": "td today", "background": True})]


@pytest.mark.parametrize("profile", [True, "research", "slack", "private", "unknown"])
@pytest.mark.parametrize("wrapper", WRAPPERS, ids=[name for name, _ in WRAPPERS])
def test_restricted_profiles_refuse_tool_wrappers(agent_class, profile, wrapper):
    deny_tool_execution(agent_class, profile=profile)
    messages = []
    assert agent_class()._execute_tool_calls(batch(wrapper), messages) is None
    assert [name for name, _ in agent_class.errors] == [wrapper[0]]


@pytest.mark.parametrize("name, arguments", [
    ("skill_view", {"name": "x"}), ("skills_list", {}), ("session_search", {"query": "x"}),
    ("web_search", {"query": "x"}), ("web_extract", {"urls": ["https://a.test"]}),
    ("terminal", {"command": "td today"}),
])
def test_private_profile_executes_no_tool(agent_class, name, arguments):
    deny_tool_execution(agent_class, profile="private")
    assert agent_class()._execute_tool_calls(batch((name, arguments)), []) is None
    assert agent_class.errors and "no tools" in agent_class.errors[0][1]


@pytest.mark.parametrize("command", ["git log -p HEAD", "git show HEAD", "git grep phrase", "git branch"])
def test_unattended_research_never_executes_repository_config(command):
    assert read_only_command(command, "research") is False


@pytest.mark.parametrize("tool,args", [("web_search", {"query": "x"}),
                                      ("web_extract", {"urls": ["https://external.test"]})])
def test_research_cannot_send_private_context_to_arbitrary_web(agent_class, tool, args):
    deny_tool_execution(agent_class, profile="research")
    assert agent_class()._execute_tool_calls(batch((tool, args)), []) is None


def test_profiles_keep_their_reads(agent_class):
    deny_tool_execution(agent_class, profile="research")
    reads = batch(("skills_list", {}), ("terminal", {"command": "gh pr view 12"}))
    assert agent_class()._execute_tool_calls(reads, []) == "executed"


@pytest.mark.parametrize("profile", [True, "research"])
@pytest.mark.parametrize("command", [
    "td today --progress-jsonl /scratch/report",
    "td today --progress-jsonl=/scratch/report",
    "td task view id:1 --user other",
    "td task list -- --progress-jsonl=/scratch/report",
])
def test_todoist_global_options_cannot_write_or_switch_account(profile, command):
    assert read_only_command(command, profile) is False


def test_linear_view_requires_no_attachment_download():
    assert read_only_command("linear issue view WEB-12", "research") is False
    assert read_only_command("linear issue view WEB-12 --no-download", "research") is True


def test_mail_profile_keeps_its_reads(agent_class):
    deny_tool_execution(agent_class, profile=True)
    assert agent_class()._execute_tool_calls(batch(("terminal", {"command": "td today"})), []) == "executed"
    assert agent_class()._execute_tool_calls(batch(("web_search", {"query": "x"})), []) is None
