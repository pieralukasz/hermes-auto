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
        if url.endswith("/pulls/7/reviews"):
            return getattr(self, "reviews", [])
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


class CommentApi(FakeApi):
    def __init__(self, items, comment):
        super().__init__(items)
        self.comment = comment

    def get(self, url, **params):
        if url.endswith("/comments/9"):
            return {"html_url": "https://github.com/acme/web/pull/7#issuecomment-9", **self.comment}
        return super().get(url, **params)


REPORT = "## Test step failed\n<!-- pytest-obligation:357d5419 -->"
BOT = {"login": "github-actions[bot]", "type": "Bot"}


@pytest.mark.parametrize("comment, kept", [
    ({"body": REPORT, "user": BOT}, False),
    ({"body": "Deploy failed", "user": BOT}, True),
    ({"body": REPORT, "user": {"login": "oz", "type": "User"}}, True),
])
def test_skip_comments_mutes_one_bot_report(store, comment, kept):
    api = CommentApi([notification("1", NOW - 600)], comment)
    settings = {**Source.defaults(), "skip_comments": [
        {"author": "github-actions[bot]", "contains": "<!-- pytest-obligation"}]}
    _, key, cursor = GitHub(settings, http=api, now=lambda: NOW).inbox({}, store)
    store.set_meta(key, cursor)
    api.items.append(notification("2", NOW + 10, reason="author"))
    candidates, _, cursor = GitHub(settings, http=api, now=lambda: NOW + 60).inbox({}, store)
    assert [c["thread_id"] for c in candidates] == (["2"] if kept else [])
    store.set_meta(key, cursor)
    assert GitHub(settings, http=api, now=lambda: NOW + 120).inbox({}, store)[0] == []  # muted stays handled


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
    assert listener.events == 1  # first answer after a start: one catch-up check
    listener.check()
    assert session.headers_sent[1] == {"If-Modified-Since": "Sun, 25 Oct 2026 07:00:00 GMT"}
    listener.check()
    assert listener.events == 1  # only a watched-repo update
    listener.check()
    assert listing(tmp_path)["nudge_at"] == NOW + 50 and listener.events == 2


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


def test_zero_interval_disables_own_polling(store):
    assert not Source().due({"poll_seconds": 0}, store, NOW - 4000, NOW)


# ── Updates in a thread that already has a session ────────────────────────────

HUMAN = {"login": "oz", "type": "User"}


def update(api, at, me="me"):
    """The next update of thread 2 as the adapter hands it to the sorter."""
    api.items = [notification("2", at, comment=api.comment is not None)]
    return github_with(api, at + 30).candidate(me, api.items[0])


class ThreadApi(FakeApi):
    def __init__(self):
        super().__init__([])
        self.comment, self.reviews = {"id": 9, "body": "can you look?", "user": HUMAN,
                                      "created_at": "2026-10-27T00:00:00Z"}, []

    def get(self, url, **params):
        if url.endswith("/comments/9"):
            return {"html_url": "https://github.com/acme/web/pull/7#issuecomment-9", **self.comment}
        return super().get(url, **params)


def opened(store, config, api):
    """Thread 2 gets its first session through the classifier."""
    fake = Fake(replies=[[{"i": 0, "decision": "session", "reason": "review", "summary": "oz prosi"}]])
    triage.triage_mail(store, config, fake, "github", "me", [update(api, NOW)], "k", {"since": 1})
    assert len(store.rows()) == 1
    return fake


def decisions(store):
    return [row["decision"] for row in store.db.execute("SELECT decision FROM triage_items ORDER BY rowid")]


def test_adapter_reports_the_newest_comment_or_review(store):
    api = ThreadApi()
    item = update(api, NOW)
    assert item["activity"] == {"id": "comment:9", "author": "oz", "bot": False}
    api.reviews = [{"id": 5, "state": "CHANGES_REQUESTED", "body": "fix the cart", "user": {"login": "ann"},
                    "submitted_at": "2026-10-27T01:00:00Z", "html_url": "https://github.com/acme/web/pull/7#r5"},
                   {"id": 6, "state": "PENDING", "user": {"login": "ann"}}]
    item = update(api, NOW + 100)
    assert item["activity"]["id"] == "review:5" and item["from"] == "ann" and item["url"].endswith("#r5")
    assert "Ostatnia recenzja (ann, CHANGES_REQUESTED): fix the cart" in item["body"]
    api.comment, api.reviews = None, []
    item = update(api, NOW + 200)
    assert item["activity"]["id"] == "" and "Bez nowego komentarza ani recenzji" in item["body"]


@pytest.mark.parametrize("change", ["push", "same comment", "bot comment"])
def test_thread_update_without_news_opens_nothing(store, config, change):
    api = ThreadApi()
    opened(store, config, api)
    if change == "push":
        api.comment = None
    elif change == "bot comment":
        api.comment = {"id": 10, "body": "Preview ready", "user": BOT, "created_at": "2026-10-27T02:00:00Z"}
    fake = Fake()
    report = triage.triage_mail(store, config, fake, "github", "me", [update(api, NOW + 100)], "k", {"since": 2})
    assert [decision for decision, _, _ in report] == ["skip"] and fake.prompts == []
    assert len(store.rows()) == 1 and store.db.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 0
    assert decisions(store) == ["session", "skip"]


@pytest.mark.parametrize("verdict, followups, recorded", [("session", 1, "followup"), ("brief", 0, "brief"),
                                                          ("skip", 0, "skip")])
def test_new_human_comment_in_a_thread_goes_through_the_classifier(store, config, verdict, followups, recorded):
    api = ThreadApi()
    opened(store, config, api)
    api.comment = {"id": 11, "body": "why this way?", "user": HUMAN, "created_at": "2026-10-27T03:00:00Z"}
    fake = Fake(replies=[[{"i": 0, "decision": verdict, "reason": "", "summary": "oz pyta"}]])
    triage.triage_mail(store, config, fake, "github", "me", [update(api, NOW + 100)], "k", {"since": 2})
    system, user = fake.prompts[0]
    assert user[0]["followup"] is True and "followup=true" in system
    assert store.db.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == followups
    assert decisions(store) == ["session", recorded]  # a session verdict continues as a "↩" session


def test_two_updates_in_one_batch_compare_with_each_other(store, config):
    api = ThreadApi()
    opened(store, config, api)
    api.comment = {"id": 11, "body": "why?", "user": HUMAN, "created_at": "2026-10-27T03:00:00Z"}
    first = update(api, NOW + 100)
    second = {**update(api, NOW + 200)}
    fake = Fake(replies=[[{"i": 0, "decision": "brief", "reason": "", "summary": "oz pyta"}]])
    triage.triage_mail(store, config, fake, "github", "me", [first, second], "k", {"since": 2})
    assert len(fake.prompts[0][1]) == 1 and decisions(store) == ["session", "skip", "brief"]


def test_legacy_payload_compares_the_shown_comment(store, config):
    api = ThreadApi()
    opened(store, config, api)
    row = store.db.execute("SELECT rowid, payload FROM triage_items").fetchone()
    legacy = {key: value for key, value in json.loads(row[1]).items() if key != "activity"}
    store.db.execute("UPDATE triage_items SET payload=? WHERE rowid=?", (json.dumps(legacy), row[0]))
    store.db.commit()
    report = triage.triage_mail(store, config, Fake(), "github", "me", [update(api, NOW + 100)], "k", {"since": 2})
    assert [decision for decision, _, _ in report] == ["skip"]


def test_followup_title_keeps_one_arrow(store):
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload) VALUES (?,?,?,?,?)",
                     ("github:me:2@x", "github", "s1", "t", json.dumps({"id": "2@x", "thread_id": "2",
                                                                        "account": "me", "kind": "github"})))
    store.db.commit()
    fake = Fake(sessions={"s1": {}})
    fake.session_excerpts = lambda ids: {"s1": {"title": "↩ ↩ ↩ GitHub: feat: cart"}}
    row = dict(store.db.execute("SELECT * FROM followups").fetchone())
    triage.redirect_followup(store, row, json.loads(row["payload"]), "r", fake)
    assert store.rows()[0]["title"] == "↩ GitHub: feat: cart"
