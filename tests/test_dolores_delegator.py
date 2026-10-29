"""Sessions live in Dolores (`bots.default`); the read-only `inbox` profile prepares third-party matters as her
subagent. The guard's `first_turn` mode locks an automatic session to that hand-off until the user writes."""
import asyncio
import json
import sqlite3

import pytest

from hermes_auto import bots, bridge, guard
from hermes_auto import guard_plugin as plugin
from hermes_auto.config import defaults, read_config
from hermes_auto.guard_plugin import policy
from hermes_auto.hermes import Hermes
from hermes_auto.runner import display_for, process_jobs, prompt_for
from hermes_auto.store import Store

TAPIN_PROJECT, ECMR_PROJECT = "tapinProject0001", "ecmrProject00001"
BOTS = {
    "default": "default", "inbox": "inbox",
    "read_only": {"inbox": {"reads": ["google", "todoist", "github", "linear", "slack"]}},
    "rules": [{"match": {"source": "todoist", "projectId": TAPIN_PROJECT}, "profile": "tapin"},
              {"match": {"source": "todoist", "projectId": ECMR_PROJECT}, "profile": "ecmr"}],
}


def config(**extra):
    return {**defaults(), "hermes_home": "/h", "language": "Polish", "bots": BOTS, **extra}


def job(source, mode="draft", **payload):
    return {"id": 7, "source": source, "mode": mode, "title": "Synthetic", "payload": json.dumps(payload),
            "session_id": "reserved-7", "profile": ""}


# ── Bots: every job goes to Dolores ──────────────────────────────────────────

@pytest.mark.parametrize("item", [
    job("gmail", deny_tools=True), job("proton", deny_tools=True), job("gmail", "agent"),
    job("slack", deny_tools="research", dm=True), job("slack", deny_tools="research"),
    job("slack", deny_tools="private", private=True), job("linear", deny_tools="research"),
    job("github", deny_tools="research", owner="example-org"),
    job("todoist", "agent", projectId=TAPIN_PROJECT), job("todoist", "agent", projectId=ECMR_PROJECT),
    job("todoist", "agent", projectId="other"),
    job("brief", "open"), job("questions", "open"), job("proposals", "open"), job("unblocked", "open"),
    job("gmail", deny_tools=True, bot="inbox"),  # a "↩" reply to an old inbox conversation
    job("slack", deny_tools="research", bot="inbox", dm=True),
])
def test_with_bots_every_job_goes_to_dolores(item):
    assert bots.choose(item, config()) == "default"
    assert bots.choose(item, config(bots={**BOTS, "default": "dolores2"})) == "dolores2"


def test_without_bots_nothing_changes():
    plain = {**defaults(), "hermes_home": "/h"}
    assert bots.choose(job("gmail", deny_tools=True), plain) == "default"
    assert bots.subagent(plain) is None and not bots.delegates(plain)


def test_the_subagent_and_the_delegation_hints():
    assert bots.subagent(config()) == "inbox" and bots.delegates(config())
    assert bots.profiles(config()) == ["default", "inbox"]  # what doctor reports
    assert bots.working_profiles(config()) == ["tapin", "ecmr"]
    assert bots.delegation_hint(job("todoist", "agent", projectId=TAPIN_PROJECT), config()) == "tapin"
    assert bots.delegation_hint(job("todoist", "agent", projectId="other"), config()) is None
    # Third-party content is never pointed at a working profile.
    assert bots.delegation_hint(job("gmail", deny_tools=True, projectId=TAPIN_PROJECT), config()) is None
    assert bots.delegation_hint(job("gmail", "agent", projectId=TAPIN_PROJECT),
                                config(bots={**BOTS, "rules": [{"match": {"projectId": TAPIN_PROJECT},
                                                                 "profile": "tapin"}]})) is None


@pytest.mark.parametrize("broken", [
    {**BOTS, "inbox": "default"},
    {**BOTS, "default": "inbox"},
    {**BOTS, "inbox": ""},
    {**BOTS, "inbox": "tapin"},  # the subagent must be a read-only (guarded) profile
    {**BOTS, "read_only": {}},
    {**BOTS, "rules": [{"match": {"source": "todoist"}, "profile": "inbox"}]},  # work never goes to the reader
    {**BOTS, "rules": [{"match": {"source": "x"}, "profile": "../etc"}]},
])
def test_config_validation(tmp_path, broken):
    (tmp_path / "config.json").write_text(json.dumps({"bots": BOTS}))
    assert read_config(tmp_path)["bots"] == BOTS
    (tmp_path / "config.json").write_text(json.dumps({"bots": broken}))
    with pytest.raises(ValueError):
        read_config(tmp_path)


class FakeHermes:
    def __init__(self):
        self.created, self.started = [], []

    def create(self, job, prompt):
        self.created.append((job["id"], job["profile"]))
        return job["session_id"]

    def start(self, job, prompt, display=None):
        self.started.append((job["id"], prompt, display))
        return {"pid": 1, "dir": "x", "deadline": 0}

    def collect(self, handle):
        return None


def test_jobs_are_created_in_dolores_and_keep_that_profile(tmp_path):
    store = Store(tmp_path, initialize=True)
    cfg = config(sources={"gmail": {"enabled": True}, "todoist": {"enabled": True}})
    store.enqueue("a", "gmail", "1", "Mail", "draft", {"deny_tools": True, "triage": {"reason": "x"},
                                                       "from": "Ann <ann@example.test>", "subject": "Hi"})
    store.enqueue("b", "todoist", "2", "Task", "agent", {"id": "2", "content": "x", "projectId": TAPIN_PROJECT})
    store.db.commit()
    fake = FakeHermes()
    process_jobs(store, cfg, fake)
    assert sorted(fake.created) == [(1, "default"), (2, "default")]
    assert {row["id"]: row["profile"] for row in store.rows()} == {1: "default", 2: "default"}
    prompts = {job_id: prompt for job_id, prompt, _ in fake.started}
    assert 'profile: "inbox"' in prompts[1] and 'profile: "tapin"' in prompts[2]
    store.close()


# ── Tools and launch of a third-party job in Dolores ─────────────────────────

def test_third_party_jobs_in_dolores_never_get_full_tools(tmp_path):
    hermes = Hermes.__new__(Hermes)
    hermes.config = {**config(), "hermes_home": str(tmp_path)}
    dolores = {"profile": "default"}
    assert hermes.tools_of({**job("gmail", "agent"), **dolores}) == (False, True)  # agent label: still a reader
    assert hermes.tools_of({**job("gmail", deny_tools=True), **dolores}) == (False, True)
    assert hermes.tools_of({**job("slack", deny_tools="research"), **dolores}) == (False, "research")
    assert hermes.tools_of({**job("todoist", "agent"), **dolores}) == (True, False)
    assert hermes.tools_of({**job("brief", "open"), **dolores}) == (False, False)
    # An old job of the read-only bot keeps its read-only launch there.
    assert hermes.tools_of({**job("gmail", "agent"), "profile": "inbox"}) == (False, True)
    assert hermes.budget({**job("gmail", deny_tools=True), **dolores}) == hermes.config["agent_run_budget_seconds"]


def test_dolores_config_names_her_subagent():
    hermes = Hermes.__new__(Hermes)
    hermes.config = config()
    bot = hermes.profile_config("default")["bot"]
    assert bot["name"] == "default" and bot["read_only"] is False
    assert bot["subagent"] == {"name": "inbox", "home": "/h/profiles/inbox", "read_only": True,
                               "reads": ["github", "google", "linear", "slack", "todoist"], "providers": {}}
    assert "subagent" not in hermes.profile_config("inbox")["bot"]
    assert "subagent" not in hermes.profile_config("tapin")["bot"]


def dolores_config(home=None):
    return {**config(), "hermes_home": str(home or "/h"),
            "bot": {"name": "default", "read_only": False, "reads": [], "providers": {},
                    "subagent": {"name": "inbox", "home": str(home or "/h") + "/profiles/inbox", "read_only": True,
                                 "reads": ["google", "todoist"], "providers": {}}}}


@pytest.mark.parametrize("deny", [True, "research", "private"])
def test_a_third_party_launch_in_dolores_keeps_her_tools_under_the_first_turn_guard(deny):
    profile, params = bridge.launch_plan({"deny_tools": deny, "full_tools": False, "job": {"mode": "draft"},
                                          "route": {}}, dolores_config())
    assert profile == policy.DELEGATE
    assert "toolsets" not in params and "ignore_rules" not in params
    assert (params["max_turns"], params["run_budget"]) == (60, 900)
    assert bridge.third_party(profile, {"source": "gmail"}, {})
    # Without a subagent (no bots) the old read-only launch stays.
    profile, params = bridge.launch_plan({"deny_tools": deny, "full_tools": False, "job": {"mode": "draft"}},
                                         {**config(), "bot": None})
    assert profile in policy.DENY_PROFILES and params["toolsets"] in ([policy.TOOLSET, "session_search"], [])


def test_the_marker_goes_into_both_texts():
    prompt, display = bridge.marked("full prompt", "card")
    assert prompt.endswith(policy.AUTO_TURN_MARK) and display.endswith(policy.AUTO_TURN_MARK)
    prompt, display = bridge.marked("full prompt", None)
    assert prompt.endswith(policy.AUTO_TURN_MARK) and display is None


@pytest.fixture
def json_settings(monkeypatch):
    """Hermes' strict YAML reader is not importable here: the test writes config.yaml as JSON (valid YAML)."""
    monkeypatch.setattr(bridge, "raw_settings",
                        lambda cfg: json.loads((__import__("pathlib").Path(cfg["hermes_home"]) / "config.yaml")
                                               .read_text()))


def write_dolores(home, *, enabled=True, toolsets=("terminal", "delegation", "hermes-auto", "session_search"),
                  backend="profile_routes", routed=("inbox", "tapin"), approvals="manual", scope=True,
                  disabled=()):
    import shutil
    home.mkdir(parents=True, exist_ok=True)
    settings = {"platform_toolsets": {"cli": list(toolsets)}, "approvals": {"mode": approvals},
                "agent": {"disabled_toolsets": list(disabled)},
                "delegation": {"backend": backend, "backends": {"profile_routes": {"profiles": [
                    {"name": name, "description": "synthetic"} for name in routed]}}}}
    if enabled:
        settings["plugins"] = {"enabled": ["other-plugin", "hermes-auto-guard"]}
    (home / "config.yaml").write_text(json.dumps(settings))
    shutil.rmtree(home / "plugins", ignore_errors=True)
    if scope:
        guard.install(home, [], mode=policy.FIRST_TURN_MODE, subagent="inbox")


def test_delegator_readiness_is_read_from_her_config(tmp_path, json_settings):
    home = tmp_path / "hermes"
    write_dolores(home)
    cfg = dolores_config(home)
    assert bridge.delegator_problems(cfg) == []
    for change, needle in ((dict(enabled=False), "plugins.enabled"), (dict(scope=False), "install-guard default"),
                           (dict(toolsets=("terminal", "delegation")), "hermes-auto"),
                           (dict(toolsets=("terminal", "hermes-auto")), "delegation"),
                           (dict(disabled=("delegation",)), "disabled_toolsets"),
                           (dict(backend="default"), "profile_routes"), (dict(routed=("tapin",)), "inbox"),
                           (dict(approvals="off"), "approvals")):
        write_dolores(home, **change)
        assert any(needle in problem for problem in bridge.delegator_problems(cfg)), (change, needle)
    # A read-only scope where Dolores needs first_turn is not ready either.
    write_dolores(home)
    guard.install(home, ["google"])
    assert any("install-guard default" in problem for problem in bridge.delegator_problems(cfg))


class FakeClient:
    def __init__(self, version=None, tools=3, hooks=1):
        self.plugin = {"name": policy.GUARD_NAME, "enabled": True, "hooks": hooks, "tools": tools,
                       "version": version or bridge.plugin_version()}

    async def rpc(self, method, **_):
        assert method == "plugins.list"
        return {"plugins": [self.plugin]}


def test_a_third_party_session_needs_a_ready_dolores_and_a_ready_subagent(tmp_path, monkeypatch, json_settings):
    home = tmp_path / "hermes"
    write_dolores(home)
    cfg = dolores_config(home)
    sub_ready = {"ok": True, "loaded": True}
    monkeypatch.setattr(bridge, "profile_problems", lambda sub: [] if sub["hermes_home"].endswith("inbox") else ["x"])

    async def sub_status(sub):
        assert sub["bot"]["name"] == "inbox" and sub["hermes_home"] == cfg["bot"]["subagent"]["home"]
        return sub_ready
    monkeypatch.setattr(bridge, "subagent_guard_status", sub_status)
    run = lambda client, profile=policy.DELEGATE: asyncio.run(  # noqa: E731
        bridge.require_bot(client, cfg, profile, {"id": 3, "source": "gmail"}, {}))
    run(FakeClient())
    with pytest.raises(bridge.Refusal, match="2.0.0"):
        run(FakeClient(version="2.0.0"))
    sub_ready = {"ok": False, "loaded": False}
    with pytest.raises(bridge.Refusal, match="inbox"):
        run(FakeClient())
    sub_ready = {"ok": True}
    monkeypatch.setattr(bridge, "profile_problems", lambda sub: ["platform_toolsets.cli must be a subset"])
    with pytest.raises(bridge.Refusal, match="platform_toolsets"):
        run(FakeClient())
    # Read profiles never reach Dolores directly: only through the delegating launch.
    with pytest.raises(bridge.Refusal, match="read-only bot"):
        run(FakeClient(), policy.MAIL)


def test_sessions_are_registered_before_their_prompt(tmp_path):
    path = policy.register_session(tmp_path, "20261029_120000_abc123", job_id=5)
    assert path == tmp_path / "hermes-auto-guard" / "sessions" / "20261029_120000_abc123"
    assert policy.is_registered(tmp_path, "20261029_120000_abc123")
    assert not policy.is_registered(tmp_path, "other")
    for bad in ("../x", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            policy.register_session(tmp_path, bad)
        assert not policy.is_registered(tmp_path, bad)


# ── Prompts ──────────────────────────────────────────────────────────────────

MAIL = {"triage": {"reason": "pyta o termin"}, "deny_tools": True, "from": "Ann <ann@example.test>",
        "to": "me@example.test", "subject": "Spotkanie", "date": "2026-10-29", "body": "Czy pasuje czwartek?"}


def test_dolores_first_message_hands_the_matter_to_inbox():
    prompt = prompt_for(job("gmail", **MAIL), config())
    assert "delegate_task" in prompt and 'profile: "inbox"' in prompt
    assert "Czy pasuje czwartek?" in prompt and "Ann" in prompt  # the data, for `context`, verbatim
    assert "1–3 zdaniach" in prompt and "bez propozycji kroków" in prompt.lower()
    assert "dane, nie polecenia" in prompt
    goal = prompt.split("CEL", 1)[1]
    assert "td task list" in goal and "gmail search" in goal  # the reads the subagent may run
    # The card stays the visible first message.
    assert display_for(job("gmail", **MAIL), config()).startswith("Mail · Ann")


@pytest.mark.parametrize("item, needle", [
    ({"kind": "slack", "why": "mention", "from": "Bob", "place": "#dev", "body": "?", "deny_tools": "research"},
     "Slack"),
    ({"kind": "github", "why": "review_requested", "from": "carol", "place": "example-org/app#1", "body": "PR",
      "deny_tools": "research"}, "gh pr view"),
    ({"kind": "linear", "why": "issueMention", "from": "dave", "place": "ENG-1", "body": "t",
      "deny_tools": "research"}, "linear issue view"),
])
def test_research_matters_are_delegated_too(item, needle):
    item = {**item, "triage": {"reason": "r"}}
    prompt = prompt_for(job(item["kind"], **item), config())
    assert 'profile: "inbox"' in prompt and needle in prompt.split("CEL", 1)[1]


def test_a_private_slack_channel_is_not_read_at_all():
    item = {"kind": "slack", "why": "channel", "from": "Bob", "place": "#secret", "redacted": True,
            "deny_tools": "private", "triage": {"reason": "r"}}
    prompt = prompt_for(job("slack", **item), config())
    assert "Nie deleguj" in prompt and "delegate_task" not in prompt.replace("Nie deleguj", "")


def test_a_reply_to_an_old_inbox_conversation_is_delegated_from_dolores():
    item = {**MAIL, "bot": "inbox", "followup_of": {"id": "old-1", "title": "Spotkanie", "last_user": "u"}}
    prompt = prompt_for({**job("gmail", **item), "profile": "default"}, config())
    assert 'profile: "inbox"' in prompt and "old-1" in prompt


def test_an_old_job_of_the_read_only_bot_keeps_its_old_prompt():
    prompt = prompt_for({**job("gmail", **MAIL), "profile": "inbox"}, config())
    assert "delegate_task" not in prompt and "Czy pasuje czwartek?" in prompt


def test_todoist_and_internal_prompts_only_gain_the_delegation_hint():
    task = job("todoist", "agent", id="t1", content="Przygotuj raport", projectId=TAPIN_PROJECT)
    prompt = prompt_for(task, config())
    assert prompt.startswith("Przygotuj raport") and 'profile: "tapin"' in prompt
    other = prompt_for(job("todoist", "agent", id="t2", content="Kup mleko", projectId="x"), config())
    assert 'profile: "tapin"' in other and 'profile: "ecmr"' in other  # the generic hint
    plain = {**defaults(), "hermes_home": "/h", "language": "Polish"}
    assert "delegate_task" not in prompt_for(job("todoist", "agent", id="t2", content="Kup mleko"), plain)
    assert 'profile: "tapin"' in prompt_for(job("questions", "open"), config())
    assert "delegate_task" not in prompt_for(job("questions", "open"), plain)


# ── install-guard default ────────────────────────────────────────────────────

def test_install_guard_default_installs_the_first_turn_mode(tmp_path, capsys):
    import argparse

    from hermes_auto import cli
    root = tmp_path / "hermes"
    (root / "profiles" / "inbox").mkdir(parents=True)
    (root / "config.yaml").write_text("{}\n")
    (root / "profiles" / "inbox" / "config.yaml").write_text("{}\n")
    cfg = {**config(), "hermes_home": str(root)}
    cli.dispatch(argparse.Namespace(command="install-guard", profile="default"), tmp_path, cfg, None)
    scope = policy.load_scope(root / "plugins" / policy.GUARD_NAME)
    assert scope == {"version": 1, "mode": "first_turn", "reads": [], "subagent": "inbox"}
    assert scope == bridge.expected_first_turn_scope(dolores_config(root))
    assert "platform_toolsets" in capsys.readouterr().out
    cli.dispatch(argparse.Namespace(command="install-guard", profile=None), tmp_path, cfg, None)
    assert policy.load_scope(root / "profiles" / "inbox" / "plugins" / policy.GUARD_NAME)["reads"] == sorted(
        BOTS["read_only"]["inbox"]["reads"])
    with pytest.raises(ValueError):
        cli.dispatch(argparse.Namespace(command="install-guard", profile="tapin"), tmp_path, cfg, None)


@pytest.mark.parametrize("scope", [
    {"version": 1, "mode": "first_turn", "reads": [], "subagent": "inbox"},
    {"version": 1, "reads": ["google"]},
    {"version": 1, "mode": "read_only", "reads": ["google"]},
])
def test_valid_scopes(scope):
    assert policy.valid_scope(scope)


@pytest.mark.parametrize("scope", [
    {"version": 1, "mode": "first_turn", "reads": [], "subagent": "../x"},
    {"version": 1, "mode": "first_turn", "reads": []},
    {"version": 1, "mode": "first_turn", "reads": ["google"], "subagent": "inbox"},
    {"version": 1, "mode": "first_turn", "reads": [], "subagent": "inbox", "providers": {"x": {"reads": []}}},
    {"version": 1, "mode": "anything", "reads": []},
])
def test_invalid_scopes(scope):
    assert not policy.valid_scope(scope)


# ── The first_turn guard ─────────────────────────────────────────────────────

SID = "20261029_090000_dolores"
MARK = "<!-- hermes-auto:auto-turn -->"  # policy.AUTO_TURN_MARK (asserted equal below)


@pytest.fixture
def dolores(tmp_path, monkeypatch):
    """Dolores' home with the guard in first_turn mode and a state.db like Hermes' (synthetic rows)."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    guard.install(home, [], mode=policy.FIRST_TURN_MODE, subagent="inbox")
    db = sqlite3.connect(home / "state.db")
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT, source TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            api_content TEXT, display_kind TEXT, display_metadata TEXT, _compressed_summary INTEGER DEFAULT 0);
    """)
    db.execute("INSERT INTO sessions VALUES (?, NULL, NULL, 'gui')", (SID,))
    db.execute("INSERT INTO messages(session_id, role, content, api_content) VALUES (?, 'user', ?, ?)",
               (SID, "Mail · Ann\n" + MARK, "full prompt\n" + MARK))
    db.execute("INSERT INTO messages(session_id, role, content) VALUES (?, 'assistant', 'Zleciłem inboxowi.')", (SID,))
    db.commit()
    db.close()
    policy.register_session(home, SID, job_id=1)
    return home


def say(home, content, kind=None, sid=SID, api=None, metadata=None, summary=0):
    db = sqlite3.connect(home / "state.db")
    db.execute("INSERT INTO messages(session_id, role, content, api_content, display_kind, display_metadata, "
               "_compressed_summary) VALUES (?, 'user', ?, ?, ?, ?, ?)",
               (sid, content, api, kind, json.dumps(metadata) if metadata else None, summary))
    db.commit()
    db.close()


def hook(name, args=None, session_id=SID):
    return plugin.pre_tool_call(tool_name=name, args=args if args is not None else {}, session_id=session_id,
                                task_id="", turn_id="t1")


def delegation(*profiles, **extra):
    return {"tasks": [{"goal": "Przygotuj sprawę", "context": "dane", **({"profile": p} if p else {})}
                      for p in profiles], **extra}


BLOCKED_TOOLS = ("terminal", "write_file", "patch", "memory", "skill_manage", "send_message", "execute_code",
                 "web_search", "web_extract", "browser_navigate", "cronjob", "skill_view", "hermes_auto_read",
                 "process_manage", "clarify", "image_generate", "")


def test_an_automatic_session_may_only_hand_over_to_inbox(dolores):
    for name in BLOCKED_TOOLS:
        result = hook(name)
        assert result["action"] == "block" and "Łukasza" in result["message"], name
    assert hook("delegate_task", delegation("inbox")) is None
    assert hook("delegate_task", delegation("inbox", "inbox")) is None
    assert hook("delegate_task", {**delegation("inbox"), "action": "spawn"}) is None
    assert hook("delegate_task", {"tasks": json.dumps(delegation("inbox")["tasks"])}) is None  # JSON string
    assert hook("delegate_task", {"action": "list"}) is None
    for args in (delegation("tapin"), delegation("inbox", "tapin"), delegation(None), delegation("Inbox"),
                 {"goal": "x", "context": "y"}, {"goal": "x", "tasks": []}, {"tasks": "not json"},
                 {"tasks": json.dumps({"profile": "inbox"})}, {"action": "steer", "subagent_id": "a", "message": "m"},
                 {"action": "stop", "subagent_id": "a"}, delegation("inbox", images=["/tmp/x.png"]),
                 {"tasks": [{"goal": "x", "profile": "inbox", "images": ["/tmp/x.png"]}]}, {}, None):
        assert hook("delegate_task", args)["action"] == "block", args
    for name in ("session_search", "hermes_auto_skill_view", "hermes_auto_skills_list", "tool_search",
                 "tool_describe"):
        assert hook(name, {"query": "x"} if name == "session_search" else {}) is None, name
    assert hook("session_search", {"query": "x", "profile": "tapin"})["action"] == "block"


def test_the_tool_search_bridge_reaches_only_the_allowed_calls(dolores):
    def bridged(*calls):
        return {"calls": [{"name": name, "arguments": arguments} for name, arguments in calls]}
    assert hook("tool_call", {"name": "delegate_task", "arguments": delegation("inbox")}) is None
    assert hook("tool_call", bridged(("hermes_auto_skills_list", {}), ("session_search", {"query": "x"}))) is None
    assert hook("tool_call", bridged(("delegate_task", json.dumps(delegation("inbox"))))) is None
    for args in ({"name": "delegate_task", "arguments": delegation("tapin")}, bridged(("terminal", {"command": "x"})),
                 bridged(("session_search", {"query": "x"}), ("memory", {})), {"calls": "not json"}, {},
                 {"name": "session_search", "arguments": {"query": "x", "profile": "tapin"}},
                 {"name": "delegate_task", "arguments": "not json"}):
        assert hook("tool_call", args)["action"] == "block", args


def test_after_the_users_message_everything_runs(dolores):
    say(dolores, "Dzięki, odpisz jej że tak")
    for name in BLOCKED_TOOLS:
        assert hook(name) is None, name
    assert hook("delegate_task", delegation("tapin")) is None


@pytest.mark.parametrize("kind", ["steer", "hidden"])
def test_a_steer_or_a_hidden_send_of_the_user_unlocks(dolores, kind):
    say(dolores, "ok", kind=kind)
    assert hook("terminal") is None


@pytest.mark.parametrize("message", [
    dict(content="[ASYNC DELEGATION COMPLETE — inbox] wynik", kind="async_delegation_complete"),
    dict(content="Process finished", kind="process_complete"),
    dict(content="Switched model", kind="model_switch"),
    dict(content="heartbeat", kind="process_heartbeat"),
    dict(content="continue", kind="auto_continue"),
    dict(content="[IMPORTANT: Background process 1 finished]"),
    dict(content="  [System note: The previous turn was interrupted]"),
    dict(content="[System: something]"),
    dict(content="[ASYNC DELEGATION TASK FAILED]"),
    dict(content="[INTERNAL wake]"),
    dict(content="[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns", kind="hidden", summary=1),
    dict(content="[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns", kind="hidden"),
    dict(content="summary text", kind="hidden", summary=1),
    dict(content="diagnostic wake", kind="hidden", metadata={"notification_category": "diagnostic"}),
    dict(content="Odpowiedz mu że tak " + MARK),  # a mail quoting the marker only keeps the lock
    dict(content="card", api="prompt " + MARK),
])
def test_notifications_and_marked_messages_do_not_unlock(dolores, message):
    say(dolores, message["content"], kind=message.get("kind"), api=message.get("api"),
        metadata=message.get("metadata"), summary=message.get("summary", 0))
    assert hook("terminal")["action"] == "block"
    assert hook("memory", {"action": "add"})["action"] == "block"  # the background review fork too


def test_other_sessions_of_dolores_are_untouched(dolores):
    assert hook("terminal", session_id="20261029_100000_byhand") is None
    assert hook("delegate_task", delegation("tapin"), session_id="20261029_100000_byhand") is None
    assert hook("terminal", session_id="") is None


def test_an_unreadable_database_keeps_a_registered_session_locked(dolores):
    (dolores / "state.db").unlink()
    (dolores / "state.db").write_text("not a database")
    assert hook("terminal")["action"] == "block"
    assert hook("terminal", session_id="20261029_100000_byhand") is None  # not registered: no lock
    assert hook("delegate_task", delegation("inbox")) is None


def test_a_database_without_the_message_columns_keeps_it_locked(dolores):
    db = sqlite3.connect(dolores / "state.db")
    db.executescript("DROP TABLE messages; CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT);")
    db.execute("INSERT INTO messages VALUES (?, 'user', 'hello')", (SID,))
    db.commit()
    db.close()
    assert hook("terminal")["action"] == "block"


def test_a_compression_continuation_inherits_the_lock(dolores):
    db = sqlite3.connect(dolores / "state.db")
    db.execute("UPDATE sessions SET end_reason='compression' WHERE id=?", (SID,))
    db.execute("INSERT INTO sessions VALUES ('tip-1', ?, NULL, 'gui')", (SID,))
    db.execute("INSERT INTO sessions VALUES ('forked', ?, NULL, 'gui')", ("tip-1",))  # tip-1 not compressed
    db.commit()
    db.close()
    say(dolores, "[CONTEXT COMPACTION — REFERENCE ONLY] summary", kind="hidden", sid="tip-1", summary=1)
    assert hook("terminal", session_id="tip-1")["action"] == "block"
    assert hook("terminal", session_id="forked") is None
    say(dolores, "Napisz do niej", sid="tip-1")
    assert hook("terminal", session_id="tip-1") is None


def test_in_dolores_the_read_tool_reads_nothing(dolores, monkeypatch):
    calls = []
    monkeypatch.setattr(policy.subprocess, "run", lambda *a, **k: calls.append(a))
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id="byhand"))["skipped"] is True
    assert calls == []


def test_the_subagents_turns_follow_the_inbox_scope(tmp_path, monkeypatch):
    """A subagent Dolores runs as `inbox` works in inbox's home: inbox's read-only rules, inbox's reads; its
    session row lives in Dolores' state.db, so inbox cannot tell its account."""
    from types import SimpleNamespace
    root = tmp_path / "hermes"
    inbox = root / "profiles" / "inbox"
    guard.install(root, [], mode=policy.FIRST_TURN_MODE, subagent="inbox")
    guard.install(inbox, ["google", "todoist", "github"])
    calls = []
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: calls.append(argv) or
                        SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setenv("HERMES_HOME", str(inbox))
    child = "20261029_090500_child"
    assert hook("terminal", session_id=child)["action"] == "block"
    assert hook("delegate_task", delegation("inbox"), session_id=child)["action"] == "block"
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id=child))["exit_code"] == 0
    assert hook("session_search", {"query": "x"}, session_id=child) is None  # no providers: the default scope
    # With a second account in inbox's scope, a subagent (no row in inbox's state.db) gets what every account
    # may read and no session_search: fail-closed, as for any session whose account is unknown.
    guard.install(inbox, ["google", "todoist", "github"], {"work": {"reads": ["github"]}})
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id=child))["skipped"] is True
    assert json.loads(plugin.read_tool({"command": "gh pr view 1 --repo a/b"}, session_id=child))["exit_code"] == 0
    assert hook("session_search", {"query": "x"}, session_id=child)["action"] == "block"
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert json.loads(plugin.read_tool({"command": "td today"}, session_id=child))["skipped"] is True
    assert len(calls) == 2


def test_plugin_manifest_version_is_bumped():
    assert policy.GUARD_VERSION == bridge.plugin_version() != "2.0.0"


def test_the_marker_constant():
    assert policy.AUTO_TURN_MARK == MARK
