"""Review fixes for the Slack and GitHub sources: backlog beyond `limit`, and safe marking read at the source."""
import pytest

from hermes_auto.store import Store

import test_github as tg
import test_slack as ts

NOW = ts.NOW


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


# ── Backlog beyond `limit` ───────────────────────────────────────────────────

def test_slack_conversations_cut_by_limit_come_next_time(store):
    fake = ts.FakeSlack()
    for i in range(3):
        fake.channel(f"D{i}", is_im=True, user="UCHRIS", last_read="0")
    key = ts.baseline(store, fake)
    for i in range(3):
        fake.post(f"D{i}", "UCHRIS", f"{NOW + 10 + i:.6f}", f"pytanie {i}")
    first, _, cursor = ts.slack_with(fake, NOW + 2000).inbox({}, store, limit=1)
    store.set_meta(key, cursor)
    second, _, cursor = ts.slack_with(fake, NOW + 2100).inbox({}, store, limit=1)
    store.set_meta(key, cursor)
    third, _, _ = ts.slack_with(fake, NOW + 2200).inbox({}, store, limit=40)
    assert [c["channel"] for c in first + second + third] == ["D0", "D1", "D2"]


def test_slack_backlog_flag_stays_set_until_cut_and_quiet_bursts_are_handed(store):
    fake = ts.FakeSlack()
    for i in range(2):
        fake.channel(f"D{i}", is_im=True, user="UCHRIS", last_read="0")
    key = ts.baseline(store, fake)
    assert store.get_meta("source_backlog:slack") is False
    fake.post("D0", "UCHRIS", f"{NOW + 10:.6f}", "pierwsze")
    fake.post("D1", "UCHRIS", f"{NOW + 11:.6f}", "drugie")
    _, _, cursor = ts.slack_with(fake, NOW + 2000).inbox({}, store, limit=1)
    store.set_meta(key, cursor)
    assert store.get_meta("source_backlog:slack") is True  # D1 was cut off
    fake.post("D0", "UCHRIS", f"{NOW + 2090:.6f}", "jeszcze pisze")
    _, _, cursor = ts.slack_with(fake, NOW + 2100).inbox({}, store, limit=40)
    store.set_meta(key, cursor)
    assert store.get_meta("source_backlog:slack") is True  # D0 is still being written: poll again
    _, _, cursor = ts.slack_with(fake, NOW + 2300).inbox({}, store, limit=40)
    assert store.get_meta("source_backlog:slack") is False


def test_slack_replay_does_not_touch_the_backlog_flag(store):
    fake = ts.FakeSlack()
    fake.channel("D0", is_im=True, user="UCHRIS", last_read="0")
    fake.post("D0", "UCHRIS", f"{NOW - 50:.6f}", "x")
    ts.slack_with(fake, NOW).inbox({}, store, limit=40, since_ms=(NOW - 100) * 1000)
    assert store.get_meta("source_backlog:slack") is None


def test_github_notifications_cut_by_limit_come_next_time(store):
    api = tg.FakeApi([])
    _, key, cursor = tg.github_with(api, NOW).inbox({}, store)
    store.set_meta(key, cursor)
    assert store.get_meta("source_backlog:github") is False
    api.items += [tg.notification(str(n), NOW + 10 + n) for n in range(3)]
    seen = []
    for step in range(3):
        got, _, cursor = tg.github_with(api, NOW + 1000 + 60 * step).inbox({}, store, limit=1)
        store.set_meta(key, cursor)
        seen += [c["thread_id"] for c in got]
        assert store.get_meta("source_backlog:github") is (step < 2)
    assert seen == ["0", "1", "2"]


# ── Marking read at the source ───────────────────────────────────────────────

def test_github_never_marks_a_thread_and_says_so(monkeypatch):
    """PATCH /notifications/threads/{id} reads the whole thread, including updates nobody has shown yet,
    and no check before it closes that race: the adapter reports every item unsupported instead."""
    import hermes_auto_github as gh
    calls, real = [], gh.Http

    class Http:
        def __init__(self, token):
            calls.append("token")

        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError(name)

    monkeypatch.setattr(gh, "Http", Http)
    monkeypatch.setattr(gh, "gh_token", lambda command: calls.append("gh") or "t")
    result = gh.Source().mark_read({"sources": {"github": {}}}, [{"id": "1@a", "thread_id": "1"},
                                                               {"id": "2@a", "thread_id": "2"}])
    assert result == {"1@a": "unsupported", "2@a": "unsupported"} and calls == []
    assert not hasattr(real, "mark_thread_read")  # no thread-wide write left to call by mistake


class SlackMarkHttp:
    """conversations.* with Slack's semantics: history between `oldest` and `latest`, both exclusive."""

    def __init__(self, channels, broken=()):
        self.channels, self.broken, self.marked = channels, set(broken), []

    def api(self, method, _post=False, **params):
        from hermes_auto_slack import SlackError
        channel = self.channels[params["channel"]]
        if params["channel"] in self.broken:
            raise SlackError(f"Slack {method} failed: channel_not_found")
        if method == "conversations.info":
            return {"channel": {k: v for k, v in channel.items() if k != "messages"}}
        if method == "conversations.history":
            low, high = float(params.get("oldest", 0)), float(params.get("latest", 1e12))
            inside = [{"ts": ts, "user": "U"} for ts in channel["messages"] if low < float(ts) < high]
            return {"messages": sorted(inside, key=lambda m: -float(m["ts"]))[:int(params["limit"])],
                    "has_more": len(inside) > int(params["limit"])}
        assert method == "conversations.mark" and _post
        self.marked.append((params["channel"], params["ts"]))
        channel["last_read"] = params["ts"]
        return {"ok": True}


def slack(item_id, channel, ts, fresh=None, thread_ts=""):
    return {"id": item_id, "channel": channel, "ts": ts, "thread_ts": thread_ts, "fresh_ts": fresh or [ts]}


def test_slack_marks_a_dm_only_over_messages_it_has_shown():
    from hermes_auto_slack import Source
    http = SlackMarkHttp({
        "D1": {"is_im": True, "last_read": "10.0", "messages": ["11.0", "12.0"]},
        "D2": {"is_im": True, "last_read": "99.0", "messages": ["50.0"]},
        "D3": {"is_im": True, "last_read": "1.0", "messages": ["5.0", "20.0"]},   # 5.0: before the baseline
        "G4": {"is_mpim": True, "last_read": "1.0", "messages": ["10.0", "15.0", "20.0", "30.0"]},
    })
    items = [slack("a", "D1", "11.0"), slack("b", "D1", "12.0"),
             slack("c", "D2", "50.0"),                             # marker already past it
             slack("d", "D3", "20.0"),                             # would hide 5.0, never shown
             slack("f", "G4", "10.0"), slack("g", "G4", "20.0")]  # 15.0 between them was never shown
    result = Source().mark_read({"sources": {"slack": {}}}, items, http=http)
    assert http.marked == [("D1", "12.0"), ("G4", "10.0")]  # 30.0 arrived later: stays unread
    assert result == {"a": "done", "b": "done", "c": "done", "d": "unsupported", "f": "done", "g": "unsupported"}


def test_slack_never_marks_channels_or_threads():
    """A channel's marker covers every message in it, not only the mention; threads have no public API."""
    from hermes_auto_slack import Source
    http = SlackMarkHttp({"C1": {"last_read": "1.0", "messages": ["20.0"]},
                          "G1": {"is_private": True, "last_read": "1.0", "messages": ["20.0"]},
                          "D1": {"is_im": True, "last_read": "1.0", "messages": ["20.0"]}})
    items = [slack("c", "C1", "20.0"), slack("p", "G1", "20.0"), slack("t", "D1", "40.0", thread_ts="20.0")]
    assert Source().mark_read({"sources": {"slack": {}}}, items, http=http) == {
        "c": "unsupported", "p": "unsupported", "t": "unsupported"}
    assert http.marked == []


def test_slack_mark_failure_stays_with_its_conversation():
    from hermes_auto_slack import Source
    http = SlackMarkHttp({"D1": {"is_im": True, "last_read": "1.0", "messages": ["5.0"]},
                          "D2": {"is_im": True, "last_read": "1.0", "messages": ["6.0"]}}, broken={"D1"})
    result = Source().mark_read({"sources": {"slack": {}}}, [slack("a", "D1", "5.0"), slack("b", "D2", "6.0")],
                                http=http)
    assert http.marked == [("D2", "6.0")]
    assert result["b"] == "done" and result["a"].startswith("retry") and "channel_not_found" in result["a"]


# ── Core: flush_read_marks ───────────────────────────────────────────────────

from hermes_auto import triage  # noqa: E402
from hermes_auto.config import defaults  # noqa: E402
from test_triage import Fake, mail  # noqa: E402


def cfg(source="gmail", **triage_options):
    return {**defaults(), "language": "Polish", "sources": {source: {"enabled": True}},
            "triage": {"enabled": True, "brief_times": ["07:45", "16:30"], **triage_options}}


def marks(store):
    return {row[0]: (row[1], row[2], row[3]) for row in
            store.db.execute("SELECT item_id, status, attempts, error FROM read_marks")}


def skipped(store, config, *ids, source="gmail", items=None):
    items = items or [mail(item_id, f"T{item_id}") for item_id in ids]
    hermes = Fake(replies=[[{"i": i, "decision": "skip", "reason": "", "summary": ""} for i in range(len(items))]])
    triage.triage_mail(store, config, hermes, source, "me@example.test", items)


class Mapping:
    calls, answer = [], {}

    def mark_read(self, config, items):
        Mapping.calls.append([item["id"] for item in items])
        return {item["id"]: Mapping.answer[item["id"]] for item in items if item["id"] in Mapping.answer}


def test_flush_honours_the_per_item_statuses_an_adapter_returns(store):
    config = cfg()
    skipped(store, config, "A", "B", "C", "D")
    Mapping.calls, Mapping.answer = [], {"A": "done", "B": "unsupported", "C": "retry: 503"}
    assert triage.flush_read_marks(store, config, {"gmail": Mapping})
    got = marks(store)
    assert got["A"][:2] == ("done", 0) and got["B"][:2] == ("unsupported", 0)
    assert got["C"][:2] == ("pending", 1) and "503" in got["C"][2]
    assert got["D"][:2] == ("pending", 1) and got["D"][2]  # no answer for it: not marked
    triage.flush_read_marks(store, config, {"gmail": Mapping})
    assert Mapping.calls[-1] == ["C", "D"]


class Poison:
    calls = []

    def mark_read(self, config, items):
        Poison.calls.append([item["id"] for item in items])
        if any(item["id"] == "BAD" for item in items):
            raise RuntimeError("400 Invalid id BAD")


def test_one_failing_item_does_not_fail_the_others(store):
    config = cfg()
    skipped(store, config, "BAD", "GOOD")
    Poison.calls = []
    for _ in range(triage.MAX_READ_ATTEMPTS):
        triage.flush_read_marks(store, config, {"gmail": Poison})
    assert marks(store)["GOOD"][0] == "done" and marks(store)["BAD"][0] == "failed"
    assert ["GOOD"] in Poison.calls


def test_mark_read_switched_off_later_marks_nothing(store):
    config = cfg()
    skipped(store, config, "A")
    Mapping.calls, Mapping.answer = [], {"A": "done"}
    triage.flush_read_marks(store, cfg(mark_read=False), {"gmail": Mapping})
    assert Mapping.calls == [] and marks(store)["A"][0] == "disabled"
    assert not triage.read_marks_waiting(store)


@pytest.mark.parametrize("settings", [{"enabled": False}, {"enabled": True, "mark_read": False}])
def test_disabled_source_or_its_mark_read_off_marks_nothing(store, settings):
    config = cfg()
    skipped(store, config, "A")
    Mapping.calls, Mapping.answer = [], {"A": "done"}
    triage.flush_read_marks(store, {**config, "sources": {"gmail": settings}}, {"gmail": Mapping})
    assert Mapping.calls == [] and marks(store)["A"][0] == "disabled"


@pytest.mark.parametrize("off", ["global", "source", "source_mark"])
def test_disable_cancels_marks_even_before_job_is_ready(store, off):
    config = cfg()
    hermes = Fake(replies=[[{"i": 0, "decision": "session", "reason": "", "summary": "x"}]])
    triage.triage_mail(store, config, hermes, "gmail", "me@example.test", [mail("A", "T1")])
    disabled = cfg(mark_read=False) if off == "global" else cfg()
    if off != "global":
        disabled["sources"]["gmail"]["enabled" if off == "source" else "mark_read"] = False
    Mapping.calls, Mapping.answer = [], {"A": "done"}
    triage.flush_read_marks(store, disabled, {"gmail": Mapping})
    assert marks(store)["A"][0] == "disabled"
    store.update(store.rows()[0]["id"], status="ready")
    triage.flush_read_marks(store, config, {"gmail": Mapping})
    assert Mapping.calls == []


def slack_item(ts, channel="D1"):
    return {"kind": "slack", "id": f"{channel}:@{ts}", "thread_id": channel, "account": "T0", "read": False,
            "from": "Ola", "to": "Łukasz", "subject": "Slack: Ola · DM", "date": "", "why": "dm",
            "channel": channel, "thread_ts": "", "ts": ts, "fresh_ts": [ts], "place": "DM", "dm": True,
            "body": "x", "url": "https://x.slack.test/", "sent": []}


class SlackRecorder:
    calls = []

    def mark_read(self, config, items):
        SlackRecorder.calls.append([item["id"] for item in items])
        return {item["id"]: "done" for item in items}


def job_for(store, external_id):
    return [job for job in store.rows() if job["external_id"] == external_id][-1]


@pytest.mark.parametrize("earlier", ["needs_attention", "running"])
def test_later_dm_message_waits_while_an_earlier_one_is_not_shown(store, earlier):
    config = cfg("slack")
    hermes = Fake(replies=[[{"i": 0, "decision": "session", "reason": "pyta", "summary": ""}],
                           [{"i": 0, "decision": "session", "reason": "pyta", "summary": ""}]])
    triage.triage_mail(store, config, hermes, "slack", "T0", [slack_item("11.0")])
    store.update(job_for(store, "D1:@11.0")["id"], status=earlier)  # failed, or still running
    triage.triage_mail(store, config, hermes, "slack", "T0", [slack_item("20.0", channel="D2")])
    store.db.execute("DELETE FROM meta WHERE key LIKE 'thread_session:%'")
    store.db.commit()
    triage.triage_mail(store, config, Fake(replies=[[{"i": 0, "decision": "session", "reason": "", "summary": ""}]]),
                       "slack", "T0", [slack_item("30.0")])
    for external_id in ("D1:@30.0", "D2:@20.0"):
        store.update(job_for(store, external_id)["id"], status="ready")
    SlackRecorder.calls = []
    triage.flush_read_marks(store, config, {"slack": SlackRecorder})
    assert SlackRecorder.calls == [["D2:@20.0"]]  # D1 at 30.0 would pass 11.0, which nobody has seen
    assert not triage.read_marks_waiting(store)
    store.update(job_for(store, "D1:@11.0")["id"], status="ready")
    assert triage.read_marks_waiting(store)
    triage.flush_read_marks(store, config, {"slack": SlackRecorder})
    assert sorted(SlackRecorder.calls[-1]) == ["D1:@11.0", "D1:@30.0"]


def test_skipped_dm_message_waits_for_the_brief_of_an_earlier_one(store):
    import datetime as dt
    config = cfg("slack")
    hermes = Fake(replies=[[{"i": 0, "decision": "brief", "reason": "", "summary": "info"}],
                           [{"i": 0, "decision": "skip", "reason": "thx", "summary": ""}]])
    triage.triage_mail(store, config, hermes, "slack", "T0", [slack_item("11.0")])
    triage.triage_mail(store, config, hermes, "slack", "T0", [slack_item("20.0")])
    SlackRecorder.calls = []
    triage.flush_read_marks(store, config, {"slack": SlackRecorder})
    assert SlackRecorder.calls == []
    triage.maybe_brief(store, config, now=dt.datetime(2026, 10, 25, 7, 50))
    brief = [job for job in store.rows() if job["source"] == "brief"][0]
    store.update(brief["id"], status="ready")
    triage.flush_read_marks(store, config, {"slack": SlackRecorder})
    assert [sorted(call) for call in SlackRecorder.calls] == [["D1:@11.0", "D1:@20.0"]]


def test_reply_waits_while_its_earlier_session_is_still_running(store):
    """The earlier session's first answer is still being written: its context comes when it is done."""
    config = cfg("slack")
    hermes = Fake(replies=[[{"i": 0, "decision": "session", "reason": "pyta", "summary": ""}]])
    triage.triage_mail(store, config, hermes, "slack", "T0", [slack_item("11.0")])
    first = job_for(store, "D1:@11.0")
    store.update(first["id"], status="running")
    hermes.sessions[first["session_id"]] = {}
    triage.triage_mail(store, config, hermes, "slack", "T0", [slack_item("20.0")])  # same DM: a reply
    triage.process_followups(store, config, hermes)
    status = store.db.execute("SELECT status FROM followups").fetchone()[0]
    assert status == "pending" and not [job for job in store.rows() if job["external_id"] == "D1:@20.0"]
    store.update(first["id"], status="ready")
    triage.process_followups(store, config, hermes)
    assert job_for(store, "D1:@20.0")


def test_private_channel_items_get_the_private_profile():
    """Private-channel text must not be re-read through research tools: the session runs without tools."""
    assert triage.deny_profile({**slack_item("1.0", channel="G1"), "private": True, "redacted": True}) == "private"
    assert triage.deny_profile({**slack_item("1.0", channel="G1"), "private": True, "redacted": False}) == "private"
    assert triage.deny_profile(slack_item("1.0")) == "research"
    assert triage.deny_profile({"kind": "github"}) == "research"
    assert triage.deny_profile(mail("M1", "T1")) is True


# ── A listener nudge that lands while a check is reading the source ─────────

@pytest.mark.parametrize("name", ["slack", "github"])
def test_stale_read_time_cannot_keep_a_consumed_nudge_due(store, tmp_path, name):
    from importlib import import_module
    from hermes_auto import listening
    Source = import_module(f"hermes_auto_{name}").Source
    store.set_meta(f"source_read_at:{name}", NOW - 1000)
    listening.write_state(tmp_path, name, {"nudge_at": NOW - 500})
    assert not Source().due({"poll_seconds": 0, "listener_poll_seconds": 0}, store, NOW, NOW + 300)


def test_slack_nudge_during_a_check_is_due_after_it(store, tmp_path):
    from hermes_auto import listening
    from hermes_auto_slack import Source
    fake = ts.FakeSlack()
    fake.channel("D0", is_im=True, user="UCHRIS", last_read="0")
    ts.slack_with(fake, NOW).inbox({}, store)                      # the check reads Slack at NOW ...
    listening.write_state(tmp_path, "slack", {"nudge_at": NOW + 20, "hints": {}})  # ... a message lands ...
    settings = {"poll_seconds": 0, "listener_poll_seconds": 0, "quiet_seconds": 90}
    from hermes_auto.cli import settle_checked
    settle_checked(store, ["slack"], [], NOW)
    assert Source().due(settings, store, store.get_meta("last_poll:slack"), NOW + 200)


def test_github_nudge_during_a_check_is_due_after_it(store, tmp_path):
    from hermes_auto import listening
    from hermes_auto_github import Source
    _, key, cursor = tg.github_with(tg.FakeApi([]), NOW).inbox({}, store)
    listening.write_state(tmp_path, "github", {"nudge_at": NOW + 20})
    from hermes_auto.cli import settle_checked
    settle_checked(store, ["github"], [], NOW)
    assert Source().due({"poll_seconds": 0}, store, store.get_meta("last_poll:github"), NOW + 3600)
