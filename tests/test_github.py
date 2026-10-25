"""GitHub source, its sorter path and listener, on synthetic notifications (no network)."""
import json

import pytest

from hermes_auto import listening, triage
from hermes_auto.config import defaults
from hermes_auto.hermes import deny_mode
from hermes_auto.runner import prompt_for
from hermes_auto.store import Store
from hermes_auto_github import GitHub, Source, html_url, iso
from hermes_auto_github.listener import GitHubListener

from test_triage import Fake

NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {"github": {"enabled": True}},
            "triage": {"enabled": True, "brief_times": ["07:45", "16:30"]}}


def notification(nid, at, reason="review_requested", unread=True, repo="acme/web", comment=True):
    subject = {"title": "feat: cart", "type": "PullRequest",
               "url": f"https://api.github.com/repos/{repo}/pulls/7",
               "latest_comment_url": f"https://api.github.com/repos/{repo}/issues/comments/9" if comment else None}
    return {"id": nid, "updated_at": iso(at), "reason": reason, "unread": unread, "subject": subject,
            "repository": {"full_name": repo, "owner": {"login": repo.split("/")[0]}}}


class FakeApi:
    def __init__(self, items):
        self.items = items
        self.queries = []

    def get(self, url, **params):
        if url == "/user":
            return {"login": "me"}
        if url == "/notifications":
            self.queries.append(params)
            return [item for item in self.items if item["updated_at"] >= params["since"]][:50] \
                if params.get("page", 1) == 1 else []
        if url.endswith("/pulls/7"):
            return {"html_url": "https://github.com/acme/web/pull/7", "state": "open", "user": {"login": "oz"},
                    "body": "opis"}
        if url.endswith("/comments/9"):
            return {"html_url": "https://github.com/acme/web/pull/7#issuecomment-9", "body": "can you look?",
                    "user": {"login": "oz", "type": "User"}}
        raise AssertionError(url)


def github_with(api, now):
    return GitHub({**Source.defaults()}, http=api, now=lambda: now)


def test_html_url():
    assert html_url("https://api.github.com/repos/o/r/pulls/12") == "https://github.com/o/r/pull/12"
    assert html_url("https://api.github.com/repos/o/r/issues/3") == "https://github.com/o/r/issues/3"
    assert html_url("https://evil.test/x") == ""


def test_inbox_baseline_then_new_updates_once(store):
    api = FakeApi([notification("1", NOW - 600)])
    _, key, cursor = github_with(api, NOW).inbox({}, store)
    store.set_meta(key, cursor)
    api.items += [notification("2", NOW + 10), notification("3", NOW + 20, reason="subscribed")]
    candidates, _, cursor = github_with(api, NOW + 60).inbox({}, store)
    assert [c["thread_id"] for c in candidates] == ["2"]  # old one is baseline, watched repo skipped
    item = candidates[0]
    assert item["kind"] == "github" and item["from"] == "oz" and not item["read"]
    assert item["url"] == "https://github.com/acme/web/pull/7#issuecomment-9" and "can you look?" in item["body"]
    store.set_meta(key, cursor)
    assert github_with(api, NOW + 120).inbox({}, store)[0] == []
    api.items[1] = notification("2", NOW + 100, unread=False)  # a new update in the same thread, read on GitHub
    candidates, _, _ = github_with(api, NOW + 180).inbox({}, store)
    assert [(c["thread_id"], c["read"]) for c in candidates] == [("2", True)]


def test_own_latest_comment_counts_as_read(store):
    api = FakeApi([notification("2", NOW + 10)])
    item = github_with(api, NOW + 60).candidate("oz", api.items[0])
    assert item["read"]


def test_github_session_researches_and_never_publishes(store, config):
    api = FakeApi([notification("2", NOW + 10)])
    item = github_with(api, NOW + 60).candidate("me", api.items[0])
    fake = Fake(replies=[[{"i": 0, "decision": "session", "reason": "prośba o review", "summary": "oz prosi"}]])
    triage.triage_mail(store, config, fake, "github", "me", [item], "k", {"since": 1})
    system, user = fake.prompts[0]
    assert "GitHub" in system and user[0]["reason"] == "review_requested"
    job = store.rows()[0]
    assert deny_mode(job) == "research" and job["title"].startswith("GitHub: feat: cart")
    prompt = prompt_for(job, config)
    assert "NIGDY sam nic nie publikuj na GitHubie" in prompt and "GITHUB-" in prompt


def test_github_brief_section(store, config):
    api = FakeApi([notification("2", NOW + 10)])
    item = github_with(api, NOW + 60).candidate("me", api.items[0])
    fake = Fake(replies=[[{"i": 0, "decision": "brief", "reason": "", "summary": "PR zmergowany"}]])
    triage.triage_mail(store, config, fake, "github", "me", [item], "k", {"since": 1})
    text = triage.render_brief([dict(row) for row in store.db.execute("SELECT * FROM triage_items")], config)
    assert "## GitHub (1)" in text and "PR zmergowany" in text


class Response:
    def __init__(self, status, items=(), modified="Sun, 25 Oct 2026 07:00:00 GMT"):
        self.status_code, self.items = status, list(items)
        self.ok = status < 400
        self.headers = {"X-Poll-Interval": "60", "Last-Modified": modified}

    def json(self):
        return self.items


class Session:
    def __init__(self, responses):
        self.responses, self.headers_sent = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.headers_sent.append(headers or {})
        return self.responses.pop(0)


def test_listener_baseline_conditional_requests_and_nudge(tmp_path):
    session = Session([Response(200, [notification("1", NOW - 60)]), Response(304),
                       Response(200, [notification("1", NOW - 60), notification("2", NOW + 30, reason="subscribed")]),
                       Response(200, [notification("3", NOW + 40)])])
    ran = []
    listener = GitHubListener(tmp_path, {}, session=session, clock=lambda: NOW + 50,
                              runner=lambda: ran.append(1) or (0, ""))
    assert listener.check() == 60
    assert listing(tmp_path).get("nudge_at") is None  # first answer is the baseline
    listener.check()
    assert session.headers_sent[1] == {"If-Modified-Since": "Sun, 25 Oct 2026 07:00:00 GMT"}
    listener.check()
    assert listing(tmp_path).get("nudge_at") is None  # only a watched-repo update
    listener.check()
    assert listing(tmp_path)["nudge_at"] == NOW + 50 and listener.events == 1


def listing(home):
    return listening.read_state(home, "github")


def test_due_polls_without_listener_and_waits_for_nudge_with_it(store, tmp_path):
    source = Source()
    assert source.due({}, store, NOW - 400, NOW)
    listening.write_state(tmp_path, "github", {"connected": True, "heartbeat": NOW - 5, "last_event_at": NOW - 5})
    assert not source.due({}, store, NOW - 400, NOW)
    listening.write_state(tmp_path, "github", {"connected": True, "heartbeat": NOW - 5, "last_event_at": NOW - 5,
                                               "nudge_at": NOW - 30})
    assert source.due({}, store, NOW - 400, NOW)
    assert json.dumps(listing(tmp_path))
