"""Clean first messages, per-call read policy, short names and session titles."""
import json
from types import SimpleNamespace

import pytest

from hermes_auto import bridge, triage
from hermes_auto.guard_plugin import policy
from hermes_auto.guard_plugin.policy import read_only_command
from hermes_auto.config import defaults
from hermes_auto.runner import display_for, process_jobs
from hermes_auto.store import Store


# ── Per-call policy in the gateway (guard plugin) ────────────────────────────

RESEARCH = {"version": 1, "profile": "research", "until": None}


def test_reads_in_a_mixed_batch_still_run_and_only_the_refused_call_is_skipped(monkeypatch):
    """Hermes asks the guard per call: reads in a batch run, the refused one alone gets a neutral result."""
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: SimpleNamespace(
        returncode=0, stdout="PR 12", stderr=""))
    assert policy.decide("hermes_auto_skill_view", {"name": "tapin-github"}, RESEARCH) is None
    assert json.loads(policy.run_read("gh pr view 12 --repo acme/app", "research")) == {"output": "PR 12",
                                                                                       "exit_code": 0}
    result = json.loads(policy.run_read("git log -5", "research"))
    assert result["skipped"] is True and "error" not in result
    # Desktop shows an empty exit 1 as a grey notice, not a red failure.
    assert result["output"] == "" and result["exit_code"] == 1


def test_a_read_session_has_no_web_or_write_tools():
    for name in ("web_search", "terminal", "write_file", "read_file", "skill_manage", "send_message"):
        assert policy.decide(name, {}, RESEARCH) is not None
        assert policy.decide(name, {}, {**RESEARCH, "profile": "mail"}) is not None


def test_read_output_is_bounded_and_run_without_a_shell(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, shell=kwargs.get("shell", False))
        return SimpleNamespace(returncode=0, stdout="x" * (policy.OUTPUT_LIMIT + 10), stderr="warn")
    monkeypatch.setattr(policy.subprocess, "run", run)
    result = json.loads(policy.run_read("td task list --filter 'search: a | b' --json", "mail"))
    assert seen == {"argv": ["td", "task", "list", "--filter", "search: a | b", "--json"], "shell": False}
    assert len(result["output"]) < policy.OUTPUT_LIMIT + 200 and "truncated" in result["output"]


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

def test_unique_title_adds_a_counter_on_a_clash():
    taken = {"GitHub · Aviad", "GitHub · Aviad (2)"}

    def set_title(title):
        if title in taken:
            raise ValueError(f"Title '{title}' is already in use by session x")
        taken.add(title)

    assert bridge.unique_title(set_title, "GitHub · Aviad", limit=20) == "GitHub · Aviad (3)"
    assert len(bridge.unique_title(set_title, "x" * 40, limit=20)) == 20


def test_unique_title_keeps_other_errors():
    def set_title(title):
        raise ValueError("Title too long")

    with pytest.raises(ValueError, match="too long"):
        bridge.unique_title(set_title, "t")


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
