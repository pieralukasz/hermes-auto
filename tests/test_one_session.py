"""No bots, no subagents (decision of 29.10.2026): every automatic session lives in the default profile (Dolores).

A third-party matter's first, automatic turn may only read (the guard's `first_turn` mode: hermes_auto_read with
the read grammar, raw skill reads, session_search in this profile), with the reads chosen by the session's account
(`routes`); the user's first message opens every tool. Todoist tasks run with full tools from the start. TapIn
matters run on the work account through `routes`, a Slack DM stays private.
"""
import argparse
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_auto import bridge, guard
from hermes_auto import guard_plugin as plugin
from hermes_auto.config import defaults, first_turn_scope, model_route, read_config
from hermes_auto.guard_plugin import policy
from hermes_auto.hermes import Hermes, job_route
from hermes_auto.runner import display_for, process_jobs, prompt_for
from hermes_auto.store import Store

TAPIN_PROJECT, ECMR_PROJECT = "tapinProject0001", "ecmrProject00001"
WORK = "anthropic-work"
ALL = ["github", "google", "linear", "slack", "todoist"]
# The work account reads work sources and Todoist (the user agreed), no calendar, Gmail or earlier conversations.
WORK_SCOPE = {"reads": ["github", "linear", "slack", "todoist"], "session_search": False}
FIRST_TURN = {"reads": ALL, "providers": {WORK: WORK_SCOPE}}
# The routes as before bots: the DM rule before the general Slack rule.
ROUTES = [{"match": {"source": "slack", "dm": True}, "provider": "anthropic"},
          {"match": {"source": "slack"}, "provider": WORK},
          {"match": {"source": "github", "owner": "example-work"}, "provider": WORK},
          {"match": {"source": "todoist", "projectId": TAPIN_PROJECT}, "provider": WORK},
          {"match": {"source": "linear"}, "provider": WORK}]


def config(**extra):
    return {**defaults(), "hermes_home": "/h", "language": "Polish", "routes": ROUTES, "first_turn": FIRST_TURN,
            **extra}


def job(source, mode="draft", **payload):
    return {"id": 7, "source": source, "mode": mode, "title": "Synthetic", "payload": json.dumps(payload),
            "session_id": "reserved-7", "profile": "", "status": "pending", "attempts": 0}


# ── Config ───────────────────────────────────────────────────────────────────

def test_config_takes_the_first_turn_scope_and_refuses_the_old_bots(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"first_turn": FIRST_TURN, "routes": ROUTES}))
    cfg = read_config(tmp_path)
    assert first_turn_scope(cfg) == {"reads": ALL, "providers": {WORK: WORK_SCOPE}}
    # Without the section every read stays and no account has its own entry.
    assert first_turn_scope({}) == {"reads": ALL, "providers": {}}
    for broken in ({"first_turn": {"reads": ["shell"]}},
                   {"first_turn": {"reads": ALL, "providers": {WORK: {"reads": ["shell"]}}}},
                   {"first_turn": {"reads": ALL, "providers": {WORK: {"reads": [], "session_search": "no"}}}},
                   {"bots": {"default": "default", "inbox": "inbox"}}):
        (tmp_path / "config.json").write_text(json.dumps(broken))
        with pytest.raises(ValueError):
            read_config(tmp_path)


@pytest.mark.parametrize("source, payload, provider", [
    ("slack", {"dm": True}, "anthropic"),                     # a DM stays private, before the general Slack rule
    ("slack", {"dm": False}, WORK),
    ("slack", {}, WORK),
    ("github", {"owner": "example-work"}, WORK),
    ("github", {"owner": "someone-else"}, None),
    ("linear", {}, WORK),
    ("todoist", {"projectId": TAPIN_PROJECT}, WORK),
    ("todoist", {"projectId": ECMR_PROJECT}, None),          # eCMR: an ordinary private session
    ("gmail", {}, None),
])
def test_routes_pick_the_account_of_every_job(source, payload, provider):
    assert model_route(config(), source, payload).get("provider") == provider
    assert job_route(job(source, **payload), config()).get("provider") == provider


# ── Every job in the default profile ─────────────────────────────────────────

class FakeHermes:
    def __init__(self, existing=()):
        self.created, self.started, self.existing = [], [], set(existing)

    def create(self, job, prompt):
        self.created.append((job["id"], job["profile"]))
        return job["session_id"]

    def start(self, job, prompt, display=None):
        self.started.append((job["id"], prompt, display))
        return {"pid": 1, "dir": "x", "deadline": 0}

    def collect(self, handle):
        return None

    def exists(self, job):
        return job["session_id"] in self.existing

    def message_count(self, job):
        return 0


MAIL = {"triage": {"reason": "pyta o termin"}, "deny_tools": True, "from": "Ann <ann@example.test>",
        "to": "me@example.test", "subject": "Spotkanie", "date": "2026-10-29", "body": "Czy pasuje czwartek?"}


def test_every_new_job_opens_in_the_default_profile(tmp_path):
    store = Store(tmp_path, initialize=True)
    cfg = config(sources={"gmail": {"enabled": True}, "todoist": {"enabled": True}})
    store.enqueue("a", "gmail", "1", "Mail", "draft", MAIL)
    store.enqueue("b", "todoist", "2", "Task", "agent", {"id": "2", "content": "x", "projectId": TAPIN_PROJECT})
    # A "↩" reply to a conversation of the retired inbox bot opens in the default profile too.
    store.enqueue("c", "gmail", "3", "↩ Mail", "draft", {**MAIL, "bot": "inbox",
                                                           "followup_of": {"id": "old-1", "title": "Spotkanie"}})
    store.db.commit()
    fake = FakeHermes()
    process_jobs(store, cfg, fake)
    assert sorted(fake.created) == [(1, "default"), (2, "default"), (3, "default")]
    prompts = {job_id: prompt for job_id, prompt, _ in fake.started}
    assert all("delegate_task" not in prompt and 'profile: "' not in prompt for prompt in prompts.values())
    store.close()


def test_a_retry_of_an_old_job_in_a_retired_profile_asks_for_a_new_session(tmp_path):
    store = Store(tmp_path, initialize=True)
    store.enqueue("a", "gmail", "1", "Mail", "draft", MAIL)
    store.db.commit()
    store.update(1, status="retry_pending", profile="inbox")
    fake = FakeHermes(existing={store.job(1)["session_id"]})
    process_jobs(store, config(sources={"gmail": {"enabled": True}}), fake)
    row = store.job(1)
    assert row["status"] == "needs_attention" and "new-session" in row["error"] and "inbox" in row["error"]
    assert fake.started == [] and fake.created == []
    # The fresh generation opens in the default profile.
    new = store.job(store.new_generation(1))
    process_jobs(store, config(sources={"gmail": {"enabled": True}}), fake)
    assert store.job(new["id"])["profile"] == "default" and fake.created == [(new["id"], "default")]
    store.close()


def test_third_party_jobs_never_get_full_tools_and_todoist_always_does(tmp_path):
    hermes = Hermes.__new__(Hermes)
    hermes.config = {**config(), "hermes_home": str(tmp_path)}
    assert hermes.tools_of(job("gmail", "agent")) == (False, True)  # the agent label: still someone's mail
    assert hermes.tools_of(job("gmail", **MAIL)) == (False, True)
    assert hermes.tools_of(job("slack", deny_tools="research")) == (False, "research")
    assert hermes.tools_of(job("slack", deny_tools="private")) == (False, "private")
    assert hermes.tools_of(job("todoist", "agent", projectId=ECMR_PROJECT)) == (True, False)
    assert hermes.tools_of(job("brief", "open")) == (False, False)
    assert hermes.budget(job("gmail", **MAIL)) == hermes.config["agent_run_budget_seconds"]


def test_old_jobs_of_a_retired_profile_are_read_without_raising(tmp_path):
    """Sessions of the retired bots (inbox, tapin, ecmr) live in profiles that may be archived: archive states and
    inspections answer "missing" without starting a bridge for a profile that is gone."""
    root = tmp_path / "hermes"
    (root / "profiles" / "tapin").mkdir(parents=True)
    (root / "profiles" / "tapin" / "state.db").write_bytes(b"")
    hermes = Hermes.__new__(Hermes)
    hermes.config, hermes.home = {**config(), "hermes_home": str(root)}, tmp_path
    calls = []

    def invoke(action, job=None, profile=None, **extra):
        calls.append((action, profile))
        if action == "archive_states":
            return {"states": {sid: False for sid in extra["session_ids"]}}
        return {"exists": True, "messages": 1, "archived": False, "owned_by": None, "last_active": None}
    hermes.invoke = invoke
    states = hermes.archive_states([("a", "default"), ("b", "inbox"), ("c", "tapin"), ("d", "")])
    assert states == {"a": False, "b": None, "c": False, "d": False}
    assert sorted(calls) == [("archive_states", "default"), ("archive_states", "tapin")]
    assert hermes.exists({"session_id": "b", "profile": "inbox"}) is False
    assert hermes.inspect("b", profile="ecmr")["exists"] is False
    assert hermes.exists({"session_id": "c", "profile": "tapin"}) is True
    assert hermes.session_excerpts(["b"], profile="inbox") == {}


def test_doctor_checks_only_the_default_profile():
    hermes = Hermes.__new__(Hermes)
    hermes.config = config()
    seen = []
    hermes.invoke = lambda action, job=None, profile=None, **extra: seen.append(profile) or {"ok": True, "ready": True}
    report = hermes.doctor()
    assert seen == ["default"] and report["ok"] is True and report["ready"] is True


def test_profile_homes_and_the_config_the_bridge_gets():
    from hermes_auto.hermes import profile_home
    assert profile_home(config(), "default") == "/h" and profile_home(config(), "") == "/h"
    assert profile_home(config(), "inbox") == "/h/profiles/inbox"
    with pytest.raises(ValueError):
        profile_home(config(), "../x")
    hermes = Hermes.__new__(Hermes)
    hermes.config = {**config(), "first_turn": {"reads": ["todoist", "google"]}}
    sent = hermes.profile_config("default")
    assert sent["hermes_home"] == "/h" and "bot" not in sent
    assert sent["first_turn"] == {"reads": ["google", "todoist"], "providers": {}}  # normalized for the bridge
    assert hermes.profile_config("inbox")["hermes_home"] == "/h/profiles/inbox"


def test_the_store_keeps_the_profile_column_of_an_old_state(tmp_path):
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, generation INTEGER NOT NULL "
               "DEFAULT 0, source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL, mode TEXT NOT NULL, "
               "payload TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending', "
               "attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT "
               "CURRENT_TIMESTAMP, updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(event_key, generation))")
    db.execute("INSERT INTO jobs(event_key,source,external_id,title,mode,payload,session_id,status) VALUES "
               "('k','gmail','1','t','draft','{}','old','ready')")
    db.commit()
    db.close()
    (tmp_path / "initialized").touch()
    store = Store(tmp_path)
    assert store.rows()[0]["profile"] == "" and store.session_profile("old") == "default"
    store.update(1, profile="inbox")
    assert store.session_profile("old") == "inbox" and store.session_profile("unknown") == "default"
    store.close()


def test_a_reply_to_an_old_inbox_conversation_reads_it_there_and_opens_in_default(tmp_path):
    from hermes_auto import triage
    store = Store(tmp_path, initialize=True)
    store.enqueue("slack:T:1", "slack", "1", "Slack · x", "draft", {"deny_tools": "research", "dm": True})
    store.db.commit()
    first = store.rows()[0]
    store.update(first["id"], status="ready", profile="inbox")
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload) VALUES (?,?,?,?,?)",
                     ("slack:T:2", "slack", first["session_id"], "x",
                      json.dumps({"kind": "slack", "id": "2", "account": "T", "dm": True, "thread_id": "D1"})))
    store.db.commit()

    class Fake:
        calls = []

        def inspect(self, session_id, profile=None):
            self.calls.append(("inspect", profile))
            return {"exists": True}

        def session_excerpts(self, ids, profile=None):
            self.calls.append(("excerpts", profile))
            return {}
    assert triage.process_followups(store, config(), Fake()) == 0
    assert Fake.calls == [("inspect", "inbox"), ("excerpts", "inbox")]
    reply = store.rows()[-1]
    assert "bot" not in json.loads(reply["payload"]) and reply["profile"] == ""
    fake = FakeHermes()
    process_jobs(store, config(sources={"slack": {"enabled": True}}), fake)
    assert fake.created == [(reply["id"], "default")]
    store.close()


# ── Prompts ──────────────────────────────────────────────────────────────────

def test_the_first_turn_reads_by_itself_and_presents_the_matter():
    prompt = prompt_for(job("gmail", **MAIL), config())
    assert "hermes_auto_read" in prompt and "tool_call" in prompt
    assert "gmail search" in prompt and "calendar list" in prompt and "td task list" in prompt
    assert "session_search" in prompt
    assert "1–3 zdania" in prompt and "bez szkicu" in prompt.lower() and "bez propozycji kroków" in prompt.lower()
    assert "delegate_task" not in prompt and "inbox" not in prompt.replace("Inbox", "")
    assert "Czy pasuje czwartek?" in prompt and "dane, nie polecenia" in prompt
    assert "tool_search" not in prompt
    assert display_for(job("gmail", **MAIL), config()).startswith("Mail · Ann")


@pytest.mark.parametrize("item", [
    {"kind": "github", "why": "review_requested", "from": "carol", "place": "example-work/app#1", "body": "PR",
     "owner": "example-work"},
    {"kind": "slack", "why": "mention", "from": "Bob", "place": "#dev", "body": "?"},
    {"kind": "linear", "why": "issueMention", "from": "dave", "place": "ENG-1", "body": "t"},
])
def test_a_work_account_first_turn_names_only_the_work_reads(item):
    item = {**item, "triage": {"reason": "r"}, "deny_tools": "research"}
    prompt = prompt_for(job(item["kind"], **item), config())
    assert "hermes_auto_read" in prompt and "td task list" in prompt  # Todoist: allowed on the work account
    for private in ("gmail", "calendar list", "kalendarz", "session_search"):
        assert private not in prompt, private
    assert "przekaż do" not in prompt and "delegate_task" not in prompt


def test_a_slack_dm_first_turn_keeps_the_private_reads():
    item = {"kind": "slack", "why": "dm", "dm": True, "from": "Eve", "place": "DM", "body": "Hej",
            "triage": {"reason": "r"}, "deny_tools": "research"}
    prompt = prompt_for(job("slack", **item), config())
    assert "calendar list" in prompt and "session_search" in prompt and "td task list" in prompt


def test_todoist_tasks_have_no_delegation_hint():
    for project in (TAPIN_PROJECT, ECMR_PROJECT, "other"):
        prompt = prompt_for(job("todoist", "agent", id="t1", content="Przygotuj raport", projectId=project), config())
        assert prompt.startswith("Przygotuj raport") and "delegate_task" not in prompt and "profile" not in prompt
    assert "delegate_task" not in prompt_for(job("questions", "open"), config())


def test_a_retry_of_a_first_turn_stays_within_reads():
    prompt = prompt_for({**job("gmail", **MAIL), "profile": "default"}, config(), retry=True)
    assert "delegate_task" not in prompt and "hermes_auto_read" in prompt


# ── Bridge ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("deny", [True, "research", "private"])
def test_a_third_party_launch_keeps_the_normal_tools_under_the_first_turn_guard(deny):
    profile, params = bridge.launch_plan({"deny_tools": deny, "full_tools": False, "job": {"mode": "draft"},
                                          "route": {"provider": WORK}}, config())
    assert profile == policy.FIRST_TURN
    assert "toolsets" not in params and "ignore_rules" not in params and params["provider"] == WORK
    assert (params["max_turns"], params["run_budget"]) == (60, 900)
    assert bridge.launch_plan({"deny_tools": False, "full_tools": True, "job": {"mode": "agent"}}, config())[0] == (
        policy.FULL)


def test_the_expected_scope_is_the_config_section():
    assert bridge.expected_scope(config()) == {"version": 2, "mode": "first_turn", "reads": ALL,
                                               "providers": {WORK: WORK_SCOPE}}
    assert bridge.expected_scope({**config(), "first_turn": {"reads": ["todoist", "google"]}}) == {
        "version": 2, "mode": "first_turn", "reads": ["google", "todoist"]}
    assert bridge.expected_scope(config()) == guard.scope_data(ALL, {WORK: WORK_SCOPE})


@pytest.fixture
def json_settings(monkeypatch):
    """Hermes' strict YAML reader is not importable here: the test writes config.yaml as JSON (valid YAML)."""
    monkeypatch.setattr(bridge, "raw_settings",
                        lambda cfg: json.loads((__import__("pathlib").Path(cfg["hermes_home"]) / "config.yaml")
                                               .read_text()))


def write_default(home, *, enabled=True, toolsets=("terminal", "hermes-auto", "session_search"), approvals="manual",
                  scope=True, disabled=()):
    import shutil
    home.mkdir(parents=True, exist_ok=True)
    settings = {"platform_toolsets": {"cli": list(toolsets)}, "approvals": {"mode": approvals},
                "agent": {"disabled_toolsets": list(disabled)}}
    if enabled:
        settings["plugins"] = {"enabled": ["other-plugin", "hermes-auto-guard"]}
    (home / "config.yaml").write_text(json.dumps(settings))
    shutil.rmtree(home / "plugins", ignore_errors=True)
    if scope:
        guard.install(home, ALL, {WORK: WORK_SCOPE})


def test_readiness_needs_no_delegation_backend(tmp_path, json_settings):
    home = tmp_path / "hermes"
    write_default(home)
    cfg = {**config(), "hermes_home": str(home)}
    assert bridge.readiness_problems(cfg) == []  # no delegation toolset, no advanced-delegation plugin
    for change, needle in ((dict(enabled=False), "plugins.enabled"), (dict(scope=False), "install-guard"),
                           (dict(toolsets=("terminal",)), "hermes-auto"),
                           (dict(disabled=("hermes-auto",)), "disabled_toolsets"),
                           (dict(approvals="off"), "approvals")):
        write_default(home, **change)
        assert any(needle in problem for problem in bridge.readiness_problems(cfg)), (change, needle)
    write_default(home)
    guard.install(home, ["google"])  # another account split than config.json says
    assert any("install-guard" in problem for problem in bridge.readiness_problems(cfg))


class FakeClient:
    def __init__(self, version=None):
        self.plugin = {"name": policy.GUARD_NAME, "enabled": True, "hooks": 1, "tools": 3,
                       "version": version or bridge.plugin_version()}

    async def rpc(self, method, **_):
        assert method == "plugins.list"
        return {"plugins": [self.plugin]}


def test_a_third_party_session_needs_the_guard_in_the_running_gateway(tmp_path, json_settings):
    home = tmp_path / "hermes"
    write_default(home)
    cfg = {**config(), "hermes_home": str(home)}
    run = lambda client, profile=policy.FIRST_TURN: asyncio.run(  # noqa: E731
        bridge.require_ready(client, cfg, profile, {"id": 3, "source": "gmail"}, {}))
    run(FakeClient())
    with pytest.raises(bridge.Refusal, match="2.2.2"):
        run(FakeClient(version="2.2.2"))
    write_default(home, approvals="off")
    with pytest.raises(bridge.Refusal, match="approvals"):
        run(FakeClient(), policy.FULL)


def test_third_party_content_never_gets_the_bounded_web_launch(tmp_path, json_settings):
    """A job with someone else's text but no read profile (never made by Hermes.tools_of) would get the old bounded
    launch with web tools and no guard: refused before anything is sent."""
    home = tmp_path / "hermes"
    write_default(home)
    cfg = {**config(), "hermes_home": str(home)}
    with pytest.raises(bridge.Refusal, match="first-turn"):
        asyncio.run(bridge.require_ready(FakeClient(), cfg, None, {"id": 3, "source": "gmail"}, {}))
    for job, request in (({"source": "brief"}, {}), ({"source": "todoist"}, {"full_tools": True})):
        asyncio.run(bridge.require_ready(FakeClient(), cfg, None, job, request))  # internal and own: pass


class GatewayClient:
    def __init__(self, sid):
        self.calls, self.sid = [], sid

    async def rpc(self, method, **fields):
        self.calls.append((method, fields))
        if method == "runtime.describe":
            return {"session_create": {"sources": ["gui"], "parameters": [
                "toolsets", "max_turns", "run_budget", "provider", "model", "cwd", "ignore_rules"]}}
        if method == "session.create":
            return {"stored_session_id": self.sid}
        return {}


def test_a_third_party_session_is_registered_before_its_prompt(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    client = GatewayClient("20261029_120000_work")
    monkeypatch.setattr(bridge, "run_gateway", lambda work: asyncio.run(work(client)))

    async def ready(*args):
        return None
    monkeypatch.setattr(bridge, "require_ready", ready)
    db = SimpleNamespace(get_session=lambda sid: {"title": "GitHub · carol"})
    request = {"deny_tools": "research", "full_tools": False, "job": {"mode": "draft", "source": "github"},
               "route": {"provider": WORK}, "title": "GitHub · carol"}
    sid = bridge.create_agent_session(request, {**config(), "hermes_home": str(home)},
                                      {"id": 9, "session_id": "reserved-9", "source": "github"}, db)
    created = dict(client.calls)["session.create"]
    assert sid == "20261029_120000_work" and "toolsets" not in created and created["provider"] == WORK
    assert policy.is_registered(home, sid)


# ── install-guard ────────────────────────────────────────────────────────────

def test_install_guard_writes_the_default_profiles_scope(tmp_path, capsys):
    from hermes_auto import cli
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n")
    cfg = {**config(), "hermes_home": str(root)}
    cli.dispatch(argparse.Namespace(command="install-guard", profile=None), tmp_path, cfg, None)
    scope = policy.load_scope(root / "plugins" / policy.GUARD_NAME)
    assert scope == bridge.expected_scope(cfg)
    out = capsys.readouterr().out
    assert "plugins.enabled" in out and "hermes-auto" in out and "delegation" not in out
    for other in ("inbox", "tapin"):
        with pytest.raises(ValueError):
            cli.dispatch(argparse.Namespace(command="install-guard", profile=other), tmp_path, cfg, None)


@pytest.mark.parametrize("scope", [
    {"version": 2, "mode": "first_turn", "reads": ALL},
    {"version": 2, "mode": "first_turn", "reads": [], "providers": {WORK: WORK_SCOPE}},
])
def test_valid_scopes(scope):
    assert policy.valid_scope(scope)


@pytest.mark.parametrize("scope", [
    {"version": 1, "mode": "first_turn", "reads": [], "subagent": "inbox"},  # the delegator's scope (guard 2.x)
    {"version": 1, "reads": ["google"]},                                     # a read-only bot's scope
    {"version": 2, "mode": "read_only", "reads": ["google"]},
    {"version": 2, "mode": "first_turn", "reads": ["shell"]},
    {"version": 2, "mode": "first_turn", "reads": [], "subagent": "inbox"},
    {"version": 2, "mode": "first_turn", "reads": [], "providers": {WORK: {"reads": ["x"]}}},
])
def test_invalid_scopes(scope):
    assert not policy.valid_scope(scope)


def test_guard_version_is_3():
    assert policy.GUARD_VERSION == bridge.plugin_version() and policy.GUARD_VERSION.startswith("3.")


# ── The first_turn guard ─────────────────────────────────────────────────────

SID, WORK_SID = "20261029_090000_private", "20261029_091000_work"
MARK = policy.AUTO_TURN_MARK


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The default profile with the guard (reads by account) and a state.db like Hermes' (synthetic rows): one
    automatic session on the private account, one on the work account."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    guard.install(home, ALL, {WORK: WORK_SCOPE})
    db = sqlite3.connect(home / "state.db")
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT, source TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
            api_content TEXT, display_kind TEXT, display_metadata TEXT, _compressed_summary INTEGER DEFAULT 0);
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    for sid, request in ((SID, {}), (WORK_SID, {"provider": WORK})):
        db.execute("INSERT INTO sessions VALUES (?, NULL, NULL, 'gui')", (sid,))
        db.execute("INSERT INTO messages(session_id, role, content, api_content) VALUES (?, 'user', ?, ?)",
                   (sid, "Mail · Ann\n" + MARK, "full prompt\n" + MARK))
        db.execute("INSERT INTO state_meta VALUES (?, ?)",
                   (plugin.POLICY_KEY + sid, json.dumps({"policy": {"request_json": json.dumps(request)}})))
        policy.register_session(home, sid, job_id=1)
    db.commit()
    db.close()
    return home


def say(home, content, sid=SID, kind=None):
    db = sqlite3.connect(home / "state.db")
    db.execute("INSERT INTO messages(session_id, role, content, display_kind) VALUES (?, 'user', ?, ?)",
               (sid, content, kind))
    db.commit()
    db.close()


def hook(name, args=None, session_id=SID):
    return plugin.pre_tool_call(tool_name=name, args=args if args is not None else {}, session_id=session_id,
                                task_id="", turn_id="t1")


def fake_run(monkeypatch):
    calls = []
    monkeypatch.setattr(policy.subprocess, "run", lambda argv, **kw: calls.append(argv) or
                        SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    return calls


def read(command, session_id=SID):
    return json.loads(plugin.read_tool({"command": command}, session_id=session_id))


GOOGLE = "python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail search x"
BLOCKED = ("terminal", "write_file", "patch", "memory", "skill_manage", "send_message", "execute_code", "web_search",
           "web_extract", "browser_navigate", "cronjob", "skill_view", "process_manage", "delegate_task", "clarify", "")


def test_the_first_turn_only_reads(home, monkeypatch):
    calls = fake_run(monkeypatch)
    for name in BLOCKED:
        result = hook(name, {"tasks": [{"goal": "x", "profile": "inbox"}]} if name == "delegate_task" else {})
        assert result["action"] == "block" and "Łukasza" in result["message"], name
    for name in ("hermes_auto_read", "hermes_auto_skill_view", "hermes_auto_skills_list"):
        assert hook(name, {"command": "td today"} if name == "hermes_auto_read" else {}) is None, name
    assert hook("session_search", {"query": "x"}) is None
    assert hook("chat_history_lookup", {"query": "x"}) is None  # the OAuth wire alias of session_search
    assert hook("session_search", {"query": "x", "profile": "tapin"})["action"] == "block"
    assert hook("tool_search", {"queries": ["x"]})["action"] == "block"
    assert hook("tool_describe", {"names": ["hermes_auto_read", "session_search"]}) is None
    assert hook("tool_describe", {"names": ["delegate_task"]})["action"] == "block"
    assert hook("tool_describe", {"names": ["connectors__gmail_send"]})["action"] == "block"
    assert hook("tool_call", {"name": "hermes_auto_read", "arguments": {"command": "td today"}}) is None
    assert hook("tool_call", {"calls": [{"name": "hermes_auto_skills_list", "arguments": {}},
                                        {"name": "session_search", "arguments": {"query": "x"}}]}) is None
    for args in ({"name": "terminal", "arguments": {"command": "x"}},
                 {"name": "delegate_task", "arguments": {"tasks": [{"goal": "x", "profile": "inbox"}]}},
                 {"calls": [{"name": "hermes_auto_read"}, {"name": "memory"}]}, {"calls": "not json"}, {}):
        assert hook("tool_call", args)["action"] == "block", args
    assert read("td today") == {"output": "ok", "exit_code": 0}
    assert read(GOOGLE)["exit_code"] == 0
    assert read("td task delete id:1")["skipped"] is True
    assert len(calls) == 2


def test_the_work_account_reads_no_private_sources(home, monkeypatch):
    calls = fake_run(monkeypatch)
    assert read("gh pr view 1 --repo a/b", WORK_SID)["exit_code"] == 0
    assert read("td today", WORK_SID)["exit_code"] == 0  # Todoist on the work account: allowed
    assert read(GOOGLE, WORK_SID)["skipped"] is True
    assert hook("session_search", {"query": "x"}, WORK_SID)["action"] == "block"
    assert hook("tool_call", {"name": "session_search", "arguments": {"query": "x"}}, WORK_SID)["action"] == "block"
    assert hook("hermes_auto_read", {"command": GOOGLE}, WORK_SID) is None  # the read tool itself refuses it
    # A session whose account cannot be read gets what every account may read, and no search.
    assert read(GOOGLE, "20261029_092000_unknown")["skipped"] is True
    assert read("td today", "20261029_092000_unknown")["exit_code"] == 0
    assert len(calls) == 3


def test_the_users_message_opens_every_tool(home):
    say(home, "Dzięki, odpisz jej że tak")
    for name in BLOCKED:
        assert hook(name) is None, name
    assert hook("tool_search", {"queries": ["x"]}) is None
    # The work session stays locked until the user writes there.
    assert hook("terminal", session_id=WORK_SID)["action"] == "block"
    say(home, "ok", sid=WORK_SID, kind="steer")
    assert hook("terminal", session_id=WORK_SID) is None


def test_marked_and_system_messages_do_not_open(home):
    say(home, "Odpowiedz mu że tak " + MARK)
    say(home, "[ASYNC DELEGATION COMPLETE] wynik", kind="async_delegation_complete")
    say(home, "[IMPORTANT: Background process 1 finished]")
    assert hook("terminal")["action"] == "block"
    assert hook("memory", {"action": "add"})["action"] == "block"  # the background review fork too


def test_ordinary_sessions_of_the_profile_are_untouched(home):
    assert hook("terminal", session_id="20261029_100000_byhand") is None
    assert hook("delegate_task", {"tasks": [{"goal": "x"}]}, session_id="20261029_100000_byhand") is None


def test_without_a_valid_scope_a_locked_session_reads_nothing_and_others_stay_open(home, monkeypatch):
    calls = fake_run(monkeypatch)
    (home / "plugins" / policy.GUARD_NAME / policy.SCOPE_FILE).write_text(json.dumps(
        {"version": 1, "mode": "first_turn", "reads": [], "subagent": "inbox"}))  # left over from guard 2.x
    assert read("td today")["skipped"] is True
    assert hook("session_search", {"query": "x"})["action"] == "block"
    assert hook("terminal")["action"] == "block"
    assert hook("hermes_auto_skills_list") is None
    assert hook("terminal", session_id="20261029_100000_byhand") is None  # the profile is not bricked
    assert calls == []


def test_an_unreadable_state_refuses_everything(home):
    (home / "state.db").write_text("synthetic corruption")
    assert "nie da się ustalić" in hook("write_file", session_id="20261029_100000_byhand")["message"]
    assert hook("write_file")["action"] == "block"  # registered: still first-turn rules


def test_a_long_registered_lineage_stays_locked(home):
    db = sqlite3.connect(home / "state.db")
    parent = SID
    for n in range(1, 201):
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id=?", (parent,))
        db.execute("INSERT INTO sessions VALUES (?, ?, NULL, 'gui')", (f"child-{n}", parent))
        parent = f"child-{n}"
    db.commit()
    db.close()
    assert policy.COMPRESSION_HOPS == 100_000
    assert hook("write_file", session_id="child-200")["action"] == "block"
    assert hook("hermes_auto_read", {"command": "td today"}, session_id="child-200") is None
