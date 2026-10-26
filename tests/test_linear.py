"""Linear source and its sorter path on synthetic notifications (no network, no `linear` CLI)."""
import pytest

from hermes_auto import cli, triage
from hermes_auto.config import defaults, model_route
from hermes_auto.hermes import deny_mode
from hermes_auto.runner import prompt_for
from hermes_auto.store import Store
from hermes_auto_linear import INBOX, MARK_READ, READ_STATE, Linear, Source, iso

from test_triage import Fake

NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {"linear": {"enabled": True}},
            "triage": {"enabled": True, "brief_times": ["07:45", "16:30"]}}


def note(nid, at, kind="issueNewComment", read=False, comment="Can you take a look at Req2?"):
    return {"id": nid, "type": kind, "createdAt": iso(at), "readAt": iso(at + 5) if read else None,
            "title": "Self preview shrinks", "subtitle": "szymon commented", "isLinearActor": False,
            "botActor": None, "actor": {"displayName": "szymon"}, "url": "https://linear.app/acme/issue/WEB-1/x",
            "issue": {"id": "issue-1", "identifier": "WEB-1", "title": "Self preview shrinks",
                      "url": "https://linear.app/acme/issue/WEB-1/x", "priorityLabel": "High",
                      "state": {"name": "To Fix"}, "team": {"key": "WEB"}, "assignee": {"displayName": "lucas"},
                      "project": {"name": "Live"}, "description": "desc"},
            "comment": {"body": comment, "user": {"displayName": "szymon"}} if comment else None}


class FakeApi:
    """`linear api` stand-in: the inbox query pages by `first`, the read-state query and the mutation."""

    def __init__(self, items, page=2):
        self.items, self.page, self.calls = items, page, []

    def __call__(self, document, variables=None):
        self.calls.append((document, variables or {}))
        if document == INBOX:
            # Linear orders newest first; the adapter must not rely on order.
            fresh = sorted((n for n in self.items if n["createdAt"] > variables["since"]),
                           key=lambda n: n["createdAt"], reverse=True)
            start = int(variables.get("after") or 0)
            chunk = fresh[start:start + self.page]
            more = start + self.page < len(fresh)
            return {"viewer": {"displayName": "lucas", "organization": {"urlKey": "acme"}},
                    "notifications": {"nodes": chunk,
                                      "pageInfo": {"hasNextPage": more, "endCursor": str(start + self.page)}}}
        if document == READ_STATE:
            return {"notifications": {"nodes": [{"id": n["id"], "readAt": n["readAt"]} for n in self.items
                                                if n["id"] in variables["ids"]]}}
        if document == MARK_READ:
            for n in self.items:
                if n["id"] == variables["id"]:
                    n["readAt"] = variables["at"]
            return {"notificationUpdate": {"success": True}}
        raise AssertionError(document)


def linear_with(api, now, **settings):
    return Linear({**Source.defaults(), **settings}, api=api, now=lambda: now)


def test_baseline_then_each_new_notification_once_across_pages(store):
    api = FakeApi([note("old", NOW - 600)])
    candidates, key, cursor = linear_with(api, NOW).inbox({}, store)
    assert candidates == [] and api.calls == []  # the first check only sets the baseline
    store.set_meta(key, cursor)
    api.items += [note("a", NOW + 10), note("b", NOW + 20), note("c", NOW + 30, kind="projectUpdatePrompt"),
                  note("d", NOW + 40, kind="issueAssignedToYou", comment=None)]
    candidates, key, cursor = linear_with(api, NOW + 60).inbox({}, store)
    assert [c["id"] for c in candidates] == ["a", "b", "d"]  # oldest first, reminder skipped, all pages read
    store.set_meta(key, cursor)
    assert linear_with(api, NOW + 120).inbox({}, store)[0] == []
    item = candidates[0]
    assert item["kind"] == "linear" and item["thread_id"] == "issue-1" and item["from"] == "szymon"
    assert item["team"] == "WEB" and item["place"] == "WEB-1" and "Req2" in item["body"]
    assert "Opis: desc" in candidates[2]["body"]  # no comment: the issue description instead


def test_limit_holds_the_cursor_and_flags_a_backlog(store):
    api = FakeApi([])
    store.set_meta(*linear_with(api, NOW).inbox({}, store)[1:])
    api.items += [note(str(n), NOW + n) for n in range(1, 6)]
    candidates, key, cursor = linear_with(api, NOW + 60).inbox({}, store, limit=2)
    assert [c["id"] for c in candidates] == ["1", "2"] and store.get_meta("source_backlog:linear")
    store.set_meta(key, cursor)
    candidates, key, cursor = linear_with(api, NOW + 70).inbox({}, store, limit=2)
    assert [c["id"] for c in candidates] == ["3", "4"]


def test_read_in_linear_still_reaches_the_sorter_unless_skip_read(store):
    api = FakeApi([note("a", NOW + 10, read=True)])
    assert not linear_with(api, NOW).candidate("lucas", "acme", api.items[0])["read"]
    assert linear_with(api, NOW, skip_read=True).candidate("lucas", "acme", api.items[0])["read"]


def test_session_researches_never_writes_and_uses_the_work_route(store, config):
    api = FakeApi([note("a", NOW + 10)])
    item = linear_with(api, NOW).candidate("lucas", "acme", api.items[0])
    routes = {"routes": [{"match": {"source": "linear"}, "provider": "work"}]}
    assert model_route(routes, "linear", item) == {"provider": "work"}
    fake = Fake(replies=[[{"i": 0, "decision": "session", "reason": "QA pyta", "summary": "szymon pyta"}]])
    triage.triage_mail(store, config, fake, "linear", "lucas", [item], "k", {"since": 1})
    system, user = fake.prompts[0]
    assert "Lineara" in system and user[0]["type"] == "issueNewComment" and user[0]["where"] == "WEB-1"
    job = store.rows()[0]
    assert deny_mode(job) == "research" and job["title"].startswith("Linear: WEB-1")
    prompt = prompt_for(job, config)
    assert "NIGDY sam nic nie zmieniaj w Linearze" in prompt and "LINEAR-" in prompt and "tapin-linear" in prompt


def test_brief_section(store, config):
    api = FakeApi([note("a", NOW + 10)])
    item = linear_with(api, NOW).candidate("lucas", "acme", api.items[0])
    fake = Fake(replies=[[{"i": 0, "decision": "brief", "reason": "", "summary": "QA PASS"}]])
    triage.triage_mail(store, config, fake, "linear", "lucas", [item], "k", {"since": 1})
    text = triage.render_brief([dict(row) for row in store.db.execute("SELECT * FROM triage_items")], config)
    assert "## Linear (1)" in text and "QA PASS" in text and "WEB-1" in text


def test_mark_read_marks_only_unread_shown_notifications():
    api = FakeApi([note("a", NOW + 10), note("b", NOW + 20, read=True), note("c", NOW + 30)])
    config = {"sources": {"linear": {"enabled": True}}}
    result = Source().mark_read(config, [{"id": "a"}, {"id": "b"}], api=api)
    assert result == {"a": "done", "b": "done"}
    mutated = [variables["id"] for document, variables in api.calls if document == MARK_READ]
    assert mutated == ["a"] and api.items[2]["readAt"] is None


def test_polled_on_its_own_interval():
    source = Source()
    assert source.due({}, None, NOW - 130, NOW) and not source.due({}, None, NOW - 60, NOW)
    assert not source.due({"poll_seconds": 0}, None, NOW - 4000, NOW)


def test_sorter_reads_linear_with_the_other_message_sources():
    assert "linear" in cli.MAIL_LIKE and triage.deny_profile({"kind": "linear"}) == "research"
