import base64
import copy

import pytest

from hermes_auto.config import sources
from hermes_auto.store import Store
from hermes_auto_gmail import Gmail
from hermes_auto_proton import parse_event, root_id
from hermes_auto_todoist import poll_todoist, task_mode


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


def task(recurring=False):
    return {"id": "T1", "content": "Example", "labels": ["hermes"],
            "due": {"date": "2026-10-03", "isRecurring": recurring}}


class TodoistFake:
    def __init__(self, task):
        self.tasks = [task]
        self.events = []
        self.calls = []
    def __call__(self, command):
        self.calls.append(command)
        return {"results": self.events if "activity" in command else self.tasks, "nextCursor": None}


CFG = {"td_command": "td", "todoist": {"label": "hermes"}, "default_mode": "draft"}


def test_recurring_reschedule_does_not_mean_completed(store):
    fake = TodoistFake(task(True))
    poll_todoist(CFG, store, fake)
    fake.tasks[0]["due"]["date"] = "2026-10-04"
    poll_todoist(CFG, store, fake)
    assert len(store.rows()) == 1
    fake.events = [{"objectId": "T1", "eventDate": "2026-10-04T09:00:00Z"}]
    poll_todoist(CFG, store, fake)
    poll_todoist(CFG, store, fake)
    assert len(store.rows()) == 2


def test_once_reschedule_remove_readd_no_new_session(store):
    fake = TodoistFake(task())
    poll_todoist(CFG, store, fake)
    fake.tasks = []
    poll_todoist(CFG, store, fake)
    fake.tasks = [task()]
    fake.tasks[0]["due"]["date"] = "2027-10-04"
    poll_todoist(CFG, store, fake)
    assert len(store.rows()) == 1
    assert not any("activity" in command for command in fake.calls)


def test_activity_pagination_and_latest_occurrence(store):
    fake = TodoistFake(task(True))
    def pages(command):
        if "activity" not in command:
            return fake(command)
        if "--cursor" not in command:
            return {"results": [{"objectId": "T1", "eventDate": "2026-10-03T12:00:00Z"}], "nextCursor": "page2"}
        return {"results": [{"objectId": "T1", "eventDate": "2026-10-03T10:00:00Z"}], "nextCursor": None}
    poll_todoist(CFG, store, pages)
    assert store.rows()[0]["event_key"].endswith("12:00:00Z")


def test_failed_completion_fetch_does_not_enqueue_duplicate(store):
    fake = TodoistFake(task(True))
    def failure(command):
        if "activity" in command:
            raise RuntimeError("offline")
        return fake(command)
    with pytest.raises(RuntimeError):
        poll_todoist(CFG, store, failure)
    assert not store.rows()


def test_mode_conflict_rejected():
    t = task()
    t["labels"] += ["hermes-open", "hermes-research"]
    with pytest.raises(ValueError, match="conflicting"):
        task_mode(t, "draft")


def gmail_message(mid, sender, outgoing=False):
    return {"id": mid, "labelIds": ["SENT"] if outgoing else [], "payload": {
        "mimeType": "multipart/mixed", "headers": [
            {"name": "From", "value": sender}, {"name": "Subject", "value": "Question"},
            {"name": "In-Reply-To", "value": "<parent@example.test>"}],
        "parts": [{"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Reply text").decode()}}]}}


def test_gmail_reads_unlabelled_reply_in_labelled_thread(store):
    gmail = object.__new__(Gmail)
    data = {"profile": {"emailAddress": "me@example.test"},
            "labels": {"labels": [{"id": "watch", "name": "Hermes/Watch"}]},
            "threads": {"threads": [{"id": "T"}]},
            "threads/T": {"messages": [gmail_message("old", "other@example.test")]}}
    gmail.get = lambda path, **kwargs: copy.deepcopy(data[path])
    settings = {"label": "Hermes/Watch", "own_addresses": ["alias@example.test"]}
    gmail.poll(settings, store)
    assert not store.rows()
    data["threads/T"]["messages"] += [gmail_message("new", "other@example.test"),
                                        gmail_message("mine", "alias@example.test"),
                                        gmail_message("sent", "unknown-alias@example.test", outgoing=True)]
    gmail.poll(settings, store)
    gmail.poll(settings, store)
    assert len(store.rows()) == 1
    assert store.rows()[0]["external_id"] == "new"
    assert "Reply text" in store.rows()[0]["payload"]


def test_proton_rfc_ids_and_own_alias_exclusion():
    from email.parser import BytesParser
    raw = b'From: Other <other@example.test>\r\nSubject: Reply\r\nMessage-ID: <reply@example.test>\r\nReferences: <root@example.test> <parent@example.test>\r\nIn-Reply-To: <parent@example.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello'
    assert root_id(BytesParser().parsebytes(raw)) == "<root@example.test>"
    assert parse_event(raw, ["me@example.test"])["eligible"]
    assert not parse_event(raw, ["other@example.test"])["eligible"]
    assert parse_event(raw.replace(b'Message-ID:', b'X-Message-ID:'), []) is None


def test_independent_entry_points():
    assert set(sources()) >= {"todoist", "gmail", "proton"}
