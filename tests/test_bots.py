"""Bots: which Hermes profile a job's session lives in (config `bots`), and that the choice sticks to the job."""
import json
import sqlite3

import pytest

from hermes_auto import bots, triage
from hermes_auto.config import defaults, read_config
from hermes_auto.hermes import Hermes
from hermes_auto.runner import process_jobs
from hermes_auto.store import Store

TAPIN_PROJECT, ECMR_PROJECT = "6gRXxxmWCvfGjx7q", "6hRQcfPv9R8PqMgx"
BOTS = {
    "default": "default", "inbox": "inbox",
    "read_only": {"inbox": {"reads": ["google", "todoist", "github", "linear", "slack"]}},
    "rules": [{"match": {"source": "todoist", "projectId": TAPIN_PROJECT}, "profile": "tapin"},
              {"match": {"source": "todoist", "projectId": ECMR_PROJECT}, "profile": "ecmr"},
              {"match": {"source": "gmail"}, "profile": "ecmr"}],  # a full bot for third-party mail: ignored
}
ROUTES = [{"match": {"source": "slack", "dm": True}, "provider": "anthropic"},
          {"match": {"source": "slack"}, "provider": "anthropic-tapin"},
          {"match": {"source": "github", "owner": "gigaverse-app"}, "provider": "anthropic-tapin"},
          {"match": {"source": "linear"}, "provider": "anthropic-tapin"}]


def config(**extra):
    return {**defaults(), "hermes_home": "/h", "bots": BOTS, **extra}


def job(source, mode="draft", **payload):
    return {"source": source, "mode": mode, "payload": json.dumps(payload)}


@pytest.mark.parametrize("item, expected", [
    (job("gmail", deny_tools=True), "inbox"),  # the gmail rule names a full bot: never for third-party content
    (job("proton", deny_tools=True), "inbox"),
    (job("slack", deny_tools="research", dm=True), "inbox"),
    (job("slack", deny_tools="research", dm=False), "inbox"),
    (job("slack", deny_tools="private", private=True), "inbox"),
    (job("linear", deny_tools="research"), "inbox"),
    (job("github", deny_tools="research", owner="gigaverse-app"), "inbox"),
    (job("todoist", "agent", projectId=TAPIN_PROJECT), "tapin"),
    (job("todoist", "agent", projectId=ECMR_PROJECT), "ecmr"),
    (job("todoist", "agent", projectId="other"), "default"),
    (job("gmail", "agent"), "ecmr"),  # the user's agent label: full tools, first full rule that matches
    (job("brief", "open"), "default"),
    (job("proposals", "open"), "default"),
])
def test_each_job_goes_to_its_bot(item, expected):
    assert bots.choose(item, config()) == expected


@pytest.mark.parametrize("item, provider", [
    (job("slack", dm=True), "anthropic"),  # TapIn workspace DMs may be friends: the private account
    (job("slack", dm=False), "anthropic-tapin"),
    (job("github", owner="gigaverse-app"), "anthropic-tapin"),
    (job("github", owner="pieralukasz"), None),
    (job("linear"), "anthropic-tapin"),
    (job("gmail"), None),
])
def test_one_inbox_bot_with_the_account_chosen_by_routes(item, provider):
    from hermes_auto.hermes import job_route
    assert bots.choose({**item, "payload": json.dumps({**json.loads(item["payload"]), "deny_tools": True})},
                       config()) == "inbox"
    assert job_route(item, config(routes=ROUTES)).get("provider") == provider


def test_a_reply_stays_in_the_read_only_bot_of_its_conversation_but_never_moves_into_a_full_one():
    two = config(bots={**BOTS, "read_only": {**BOTS["read_only"], "inbox2": {"reads": []}}})
    assert bots.choose(job("slack", deny_tools="research", bot="inbox2", dm=True), two) == "inbox2"
    assert bots.choose(job("gmail", deny_tools=True, bot="default"), config()) == "inbox"
    assert bots.choose(job("todoist", "agent", bot="inbox"), config()) == "default"


def test_without_bots_everything_stays_in_the_default_profile():
    assert bots.choose(job("gmail", deny_tools=True), {**defaults(), "hermes_home": "/h"}) == "default"


def test_profile_homes():
    assert bots.profile_home(config(), "default") == "/h"
    assert bots.profile_home(config(), "inbox") == "/h/profiles/inbox"
    with pytest.raises(ValueError):
        bots.profile_home(config(), "../x")


@pytest.mark.parametrize("broken", [
    {**BOTS, "inbox": "default"},
    {**BOTS, "default": "inbox"},
    {**BOTS, "read_only": {}},
    {**BOTS, "rules": [{"match": {}, "profile": "x"}]},
    {**BOTS, "rules": [{"match": {"source": "x"}, "profile": "../etc"}]},
    {**BOTS, "read_only": {"inbox": {}}},
])
def test_config_validation(tmp_path, broken):
    (tmp_path / "config.json").write_text(json.dumps({"bots": BOTS}))
    assert read_config(tmp_path)["bots"] == BOTS
    (tmp_path / "config.json").write_text(json.dumps({"bots": broken}))
    with pytest.raises(ValueError):
        read_config(tmp_path)


def test_the_store_adds_the_profile_column_to_an_old_state(tmp_path):
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


class FakeHermes:
    def __init__(self):
        self.created = []

    def create(self, job, prompt):
        self.created.append((job["id"], job["profile"]))
        return job["session_id"]

    def start(self, job, prompt, display=None):
        return {"pid": 1, "dir": "x", "deadline": 0}

    def collect(self, handle):
        return None


def test_the_bot_is_chosen_once_before_the_session_exists(tmp_path):
    store = Store(tmp_path, initialize=True)
    cfg = config(sources={"gmail": {"enabled": True}, "todoist": {"enabled": True}}, language="Polish")
    store.enqueue("a", "gmail", "1", "Mail", "draft", {"deny_tools": True, "triage": {"reason": "x"}})
    store.enqueue("b", "todoist", "2", "Task", "agent", {"id": "2", "content": "x", "projectId": TAPIN_PROJECT})
    store.db.commit()
    fake = FakeHermes()
    process_jobs(store, cfg, fake)
    assert sorted(fake.created) == [(1, "inbox"), (2, "tapin")]
    assert {row["id"]: row["profile"] for row in store.rows()} == {1: "inbox", 2: "tapin"}
    # A changed mapping does not move a job whose session already exists.
    store.new_generation(1)
    assert store.rows()[-1]["profile"] == ""  # chosen again when its new session is created


def test_bridge_requests_target_the_bots_home(tmp_path, monkeypatch):
    seen = []
    hermes = Hermes.__new__(Hermes)
    hermes.config, hermes.home, hermes.python = config(), tmp_path, "python"
    monkeypatch.setattr(Hermes, "invoke", lambda self, action, job=None, profile=None, **extra:
                        seen.append((action, profile, extra)) or {"states": {sid: False for sid in
                                                                            extra.get("session_ids", [])}})
    states = hermes.archive_states([("a", "inbox"), ("b", "default"), ("c", "inbox")])
    assert states == {"a": False, "b": False, "c": False}
    assert [(action, profile, extra["session_ids"]) for action, profile, extra in seen] == [
        ("archive_states", "inbox", ["a", "c"]), ("archive_states", "default", ["b"])]
    inbox = hermes.profile_config("inbox")
    assert inbox["hermes_home"] == "/h/profiles/inbox"
    assert inbox["bot"] == {"name": "inbox", "read_only": True,
                            "reads": ["github", "google", "linear", "slack", "todoist"]}
    assert hermes.profile_config("tapin")["bot"]["read_only"] is False
    legacy = Hermes.__new__(Hermes)
    legacy.config = {**defaults(), "hermes_home": "/h"}
    assert legacy.profile_config("default") is legacy.config


def test_a_reply_opens_its_new_session_in_the_bot_of_the_conversation(tmp_path):
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
        excerpts = []

        def inspect(self, session_id, profile=None):
            self.excerpts.append(("inspect", profile))
            return {"exists": True}

        def session_excerpts(self, ids, profile=None):
            self.excerpts.append(("excerpts", profile))
            return {}
    fake = Fake()
    assert triage.process_followups(store, config(), fake) == 0
    assert fake.excerpts == [("inspect", "inbox"), ("excerpts", "inbox")]
    reply = store.rows()[-1]
    assert json.loads(reply["payload"])["bot"] == "inbox"
    assert bots.choose(reply, config()) == "inbox"
