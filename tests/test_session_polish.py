"""Clean first messages, per-call read policy, short names and session titles."""
import json
import os
import sys
import types
from types import SimpleNamespace

import pytest

from hermes_auto import bridge, triage
from hermes_auto.bridge import deny_tool_execution, read_only_command, show_clean_prompt
from hermes_auto.config import defaults
from hermes_auto.runner import display_for, process_jobs
from hermes_auto.store import Store


# ── Per-call policy at the batch boundary ────────────────────────────────────

@pytest.fixture
def agent_class(monkeypatch):
    results = []

    def append(messages, calls, payload):
        for call in calls:
            results.append((call.function.name, json.loads(payload(call))))

    module = types.ModuleType("agent.turn_tool_validation")
    module._append_tool_error_results = append
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.turn_tool_validation", module)

    class Agent:
        def __init__(self, **kwargs):
            self.executed = []

        def _execute_tool_calls(self, message, messages, *args, **kwargs):
            self.executed.append([call.function.name for call in message.tool_calls])
            return "executed"

    Agent.results = results
    return Agent


def batch(*calls):
    return SimpleNamespace(content="thinking", tool_calls=[SimpleNamespace(function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments))) for name, arguments in calls])


def test_reads_in_a_mixed_batch_still_run_and_only_the_refused_call_is_skipped(agent_class):
    deny_tool_execution(agent_class, profile="research")
    agent = agent_class()
    message = batch(("skill_view", {"name": "tapin-github"}), ("terminal", {"command": "git log -5"}),
                    ("terminal", {"command": "gh pr view 12 --repo acme/app"}))
    assert agent._execute_tool_calls(message, []) == "executed"
    assert agent.executed == [["skill_view", "terminal"]]  # the two reads, in order
    [(name, result)] = agent_class.results
    assert name == "terminal" and result["skipped"] is True and "error" not in result
    # Desktop shows an empty exit 1 of a terminal call as a grey notice, not a red failure.
    assert result["output"] == "" and result["exit_code"] == 1
    assert "gh api repos/OWNER/REPO/contents" in result["note"]


def test_a_batch_with_nothing_allowed_runs_nothing(agent_class):
    deny_tool_execution(agent_class, profile=True)
    agent = agent_class()
    assert agent._execute_tool_calls(batch(("web_search", {"query": "x"})), []) is None
    assert agent.executed == []
    [(name, result)] = agent_class.results
    assert name == "web_search" and "exit_code" not in result and "error" not in result


@pytest.fixture
def cache(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "cache" / "terminal-output").mkdir(parents=True)
    (home / "cache" / "terminal-output" / "out-1.log").write_text("long gh output")
    (tmp_path / "secret.txt").write_text("x")
    monkeypatch.setattr(bridge, "hermes_homes", lambda: {str(home)})
    return home / "cache" / "terminal-output"


@pytest.mark.parametrize("profile", [True, "research"])
def test_saved_command_output_can_be_read_back(agent_class, cache, profile):
    deny_tool_execution(agent_class, profile=profile)
    agent = agent_class()
    message = batch(("read_file", {"path": str(cache / "out-1.log")}),
                    ("search_files", {"pattern": "author", "path": str(cache / "out-1.log")}))
    assert agent._execute_tool_calls(message, []) == "executed"
    assert agent_class.results == []


@pytest.mark.parametrize("path", ["{tmp}/secret.txt", "{cache}/../../../secret.txt", "{cache}/link", "", None,
                                  "{cache}", "{cache}/missing,{tmp}", "{cache}/missing {tmp}", "{cache}/out-1.log,{tmp}"])
def test_reads_outside_the_output_cache_are_refused(agent_class, cache, tmp_path, path):
    os.symlink(tmp_path / "secret.txt", cache / "link")
    value = path.format(tmp=tmp_path, cache=cache) if path is not None else None
    deny_tool_execution(agent_class, profile="research")
    assert agent_class()._execute_tool_calls(batch(("read_file", {"path": value})), []) is None
    assert [name for name, _ in agent_class.results] == ["read_file"]


def test_private_profile_cannot_read_the_cache_either(agent_class, cache):
    deny_tool_execution(agent_class, profile="private")
    assert agent_class()._execute_tool_calls(batch(("read_file", {"path": str(cache / "out-1.log")})), []) is None


@pytest.mark.parametrize("command,allowed", [
    ("gh api repos/acme/app/pulls/4/files --paginate --jq '.[].filename'", True),
    ("gh api repos/acme/app/contents/src/a.ts?ref=abc123 --jq '.content | @base64d'", True),
    ("gh api repos/acme/app/issues/4/comments -q .[].body", True),
    ("gh api user --jq env", False),
    ("gh api user -q '$ENV'", False),
    ("gh api user --jq '.login, env.ANTHROPIC_API_KEY'", False),
    ("gh api user --jq '$__loc__'", False),
    ("gh api repos/acme/app/issues -f title=x --jq .", False),
    ("gh api repos/acme/app --jq . -X POST", False),
    ("gh api graphql --jq .", False),
])
def test_gh_api_jq_filters_are_reads(command, allowed):
    assert read_only_command(command, "research") is allowed


# ── Clean first message ──────────────────────────────────────────────────────

def test_session_shows_the_card_while_the_model_gets_the_prompt():
    calls = []

    class Agent:
        def run_conversation(self, user_message, system_message=None, persist_user_message=None, **kwargs):
            calls.append((user_message, persist_user_message, kwargs.get("title_user_message")))
            return {}

    show_clean_prompt(Agent, "FULL PROMPT", "card")
    Agent().run_conversation("FULL PROMPT")
    Agent().run_conversation("a later turn")
    assert calls == [("FULL PROMPT", "card", "card"), ("a later turn", None, None)]


def test_no_display_leaves_hermes_untouched():
    class Agent:
        def run_conversation(self, user_message, **kwargs):
            return kwargs

    original = Agent.run_conversation
    show_clean_prompt(Agent, "P", None)
    show_clean_prompt(Agent, "P", "P")
    assert Agent.run_conversation is original


def test_unique_title_adds_a_counter_on_a_clash():
    class Db:
        MAX_TITLE_LENGTH = 20
        taken = {"GitHub · Aviad", "GitHub · Aviad (2)"}

        def set_session_title(self, sid, title):
            if title in self.taken:
                raise ValueError(f"Title '{title}' is already in use by session x")
            self.taken.add(title)

    assert bridge.unique_title(Db(), "s", "GitHub · Aviad") == "GitHub · Aviad (3)"
    assert len(bridge.unique_title(Db(), "s", "x" * 40)) == 20


def test_unique_title_keeps_other_errors():
    class Db:
        MAX_TITLE_LENGTH = 100

        def set_session_title(self, sid, title):
            raise ValueError("Title too long")

    with pytest.raises(ValueError, match="too long"):
        bridge.unique_title(Db(), "s", "t")


GITHUB = {"kind": "github", "id": "9@t", "thread_id": "9", "from": "aviadr1", "why": "review_requested",
          "place": "acme/web-app", "subject": "GitHub: feat(widgets): sports stat cards · acme/web-app",
          "url": "https://github.com/acme/web-app/pull/12", "body": "Poufny komentarz z kodem"}


def test_card_says_who_what_where_and_why_without_the_body():
    card = triage.session_card(GITHUB, "czeka na Twoje review", {"language": "Polish"})
    assert card.splitlines() == [
        "GitHub · aviadr1 prosi o review",
        "feat(widgets): sports stat cards",
        "acme/web-app · https://github.com/acme/web-app/pull/12",
        "Dlaczego sesja: czeka na Twoje review",
    ]
    assert "Poufny" not in card


def test_card_for_mail_and_reply_to_an_earlier_session():
    mail = {"id": "m1", "from": "Ewa Nowak <ewa@biuro.test>", "subject": "Faktura za październik",
            "url": "javascript:alert(1)", "followup_of": {"id": "S1", "title": "Gmail · Ewa: faktura"}}
    card = triage.session_card(mail, "", {"language": "Polish"})
    assert card.splitlines() == ["Mail · Ewa Nowak", "Faktura za październik",
                                 "Ciąg dalszy rozmowy: Gmail · Ewa: faktura"]


def test_display_for_todoist_mail_and_internal_jobs():
    config = {"language": "Polish"}
    todo = {"source": "todoist", "title": "x", "payload": json.dumps({"content": "Zrób VAT", "description": "z KSeF"})}
    assert display_for(todo, config) == "Zrób VAT\n\nz KSeF"
    mail = {"source": "github", "payload": json.dumps({**GITHUB, "triage": {"reason": "review"}})}
    assert display_for(mail, config).startswith("GitHub · aviadr1 prosi o review")
    assert display_for(mail, config, True) is None  # a retry is a short continuation
    assert display_for({"source": "brief", "payload": "{}"}, config) is None


def test_runner_sends_the_card_with_the_run(tmp_path):
    store = Store(tmp_path, initialize=True)
    config = {**defaults(), "language": "Polish", "sources": {"github": {"enabled": True}}}
    store.enqueue("github:me:9@t", "github", "9@t", "Aviad: review", "draft",
                  {**GITHUB, "triage": {"decision": "session", "reason": "review"}, "deny_tools": "research"})
    store.db.commit()

    class Hermes:
        started = []

        def create(self, job, prompt):
            return {}

        def start(self, job, prompt, display=None):
            self.started.append((prompt, display))
            return {"pid": 1}

        def collect(self, handle):
            return None

    process_jobs(store, config, Hermes())
    [(prompt, display)] = Hermes.started
    assert "Poufny komentarz" in prompt and "Poufny" not in display
    assert display.startswith("GitHub · aviadr1")
    store.close()


# ── Short names (Haiku) ──────────────────────────────────────────────────────

class Namer:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def classify(self, system, user, max_tokens=4000, route=None):
        self.calls.append((system, json.loads(user), route))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def naming_config(**triage_options):
    return {**defaults(), "triage": {"title_model": "claude-haiku-5-5", **triage_options},
            "routes": [{"match": {"source": "github", "owner": "work"}, "provider": "work-account"}]}


def test_names_come_from_the_title_model_on_each_items_route():
    work = {**GITHUB, "owner": "work"}
    mine = {"id": "m1", "from": "Ewa <ewa@biuro.test>", "subject": "Faktura", "body": "Proszę o fakturę"}
    namer = Namer([[{"i": 0, "name": "Aviad: review sports stat cards"}],
                   [{"i": 0, "name": "„Ewa: brakująca faktura.”"}]])
    triage.name_items(namer, naming_config(), [work, mine])
    assert work["name"] == "Aviad: review sports stat cards" and mine["name"] == "Ewa: brakująca faktura"
    routes = [call[2] for call in namer.calls]
    assert {"provider": "work-account", "model": "claude-haiku-5-5"} in routes
    assert {"model": "claude-haiku-5-5"} in routes
    assert all(len(json.dumps(call[1])) < 4000 for call in namer.calls)


def test_naming_failure_or_off_keeps_subjects():
    item = {**GITHUB}
    triage.name_items(Namer([RuntimeError("rate limited")]), naming_config(), [item])
    assert "name" not in item and triage.item_title(item).startswith("GitHub: feat")
    namer = Namer([])
    triage.name_items(namer, {**defaults(), "triage": {}}, [item])  # off by default
    assert namer.calls == []


def test_long_names_are_cut():
    assert len(triage.clean_name("x" * 200)) == 60


def test_sorted_session_is_titled_with_its_name(tmp_path):
    store = Store(tmp_path, initialize=True)
    config = {**naming_config(), "language": "Polish", "sources": {"gmail": {"enabled": True}}}
    config["triage"]["enabled"] = True
    mail = {"id": "m1", "thread_id": "t1", "from": "Ewa <ewa@biuro.test>", "subject": "RE: RE: Faktura",
            "body": "Proszę o fakturę do piątku", "sent": []}
    hermes = Namer([[{"i": 0, "decision": "session", "reason": "prosi o fakturę", "summary": "Ewa prosi."}],
                    [{"i": 0, "name": "Ewa: faktura do piątku"}]])
    hermes.find_session = lambda **query: None
    triage.triage_mail(store, config, hermes, "gmail", "me@example.test", [mail])
    [job] = store.rows()
    assert job["title"] == "Ewa: faktura do piątku"
    payload = json.loads(store.db.execute("SELECT payload FROM triage_items").fetchone()[0])
    assert payload["name"] == "Ewa: faktura do piątku"
    store.close()



@pytest.mark.parametrize("command,allowed", [
    ('linear issue query --all-teams --search "navigation" --updated-after 2026-09-01 --limit 15', True),
    ("linear issue query --search shop --search-comments --state started -j", True),
    ("linear issue query --search shop --workspace other", False),
])
def test_linear_issue_query_filters_are_reads(command, allowed):
    assert read_only_command(command, "research") is allowed


def test_slack_card_does_not_repeat_the_sender_and_cleans_old_titles():
    item = {"kind": "slack", "from": "Bart", "why": "dm", "place": "DM", "subject": "Slack: Bart · DM",
            "url": "https://example.slack.com/archives/D1/p1",
            "followup_of": {"id": "S1", "title": "↩ Slack: Bart · DM · slack · #184 · slack · #191"}}
    assert triage.session_card(item, "", {"language": "Polish"}).splitlines() == [
        "Slack · Bart napisał do Ciebie", "DM · https://example.slack.com/archives/D1/p1",
        "Ciąg dalszy rozmowy: Slack: Bart · DM"]



def test_cache_files_from_before_this_run_are_refused(cache):
    old = cache / "out-old.log"
    old.write_text("another session's output")
    os.utime(old, (1, 1))
    assert bridge.cached_output_read("read_file", {"path": str(old)}) is False
    assert bridge.cached_output_read("read_file", {"path": str(cache / "out-1.log")}, since=0) is True
    # A file search over the cache would list other sessions' files.
    assert bridge.cached_output_read("search_files", {"path": str(cache / "out-1.log"), "target": "files"}) is False


@pytest.mark.parametrize("index", [1e400, "0", True, None, -1, 5])
def test_bad_name_indexes_are_ignored(index):
    item = {**GITHUB}
    triage.name_items(Namer([[{"i": index, "name": "x"}, "junk"]]), naming_config(), [item])
    assert "name" not in item


def test_events_title_never_shows_message_text():
    from hermes_auto.overview import _title
    assert _title({"snippet": "Poufny tekst", "body": "Poufny"}) == "(bez tematu)"
    assert _title({"content": "Zrób VAT"}, "task") == "Zrób VAT"
    assert _title({"name": "Ewa: faktura", "subject": "RE: x"}) == "Ewa: faktura"
