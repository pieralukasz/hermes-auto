"""Agent completes the task itself, then the user archives the session: nothing else may close.

Synthetic Todoist state only; `td` is never executed.
"""
import json
import subprocess

import pytest

import hermes_auto_todoist
from hermes_auto.finish import sync_finished
from hermes_auto.store import Store


class FakeTd:
    """Minimal in-memory Todoist for one task, answering the read/write commands finish uses."""

    def __init__(self, due, recurring, next_due=None):
        self.task = {"id": "T1", "content": "Brief", "labels": ["hermes"], "checked": False,
                     "isDeleted": False, "due": {"date": due, "isRecurring": recurring}}
        self.next_due = next_due
        self.events = []
        self.writes = []

    def complete(self, when="2026-10-03T07:40:00Z"):
        if self.task["due"]["isRecurring"]:
            self.task["due"]["date"] = self.next_due
        else:
            self.task["checked"] = True
        self.events.append({"objectId": "T1", "eventDate": when})

    def read(self, command):
        if command[1] == "activity":
            return {"results": list(self.events), "nextCursor": None}
        return json.loads(json.dumps(self.task))

    def write(self, command):
        self.writes.append(command)
        self.complete()

    def run(self, command, **kwargs):
        """Stands in for subprocess.run inside the adapter: no real `td` process starts."""
        if command[1:3] == ["task", "complete"]:
            self.write(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(self.read(command)), "")


class Archive:
    def __init__(self):
        self.archived = {}

    def archive_states(self, sessions):  # (session id, bot profile) pairs
        return {sid: self.archived.get(sid) for sid, _profile in sessions}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


CONFIG = {"sources": {"todoist": {"enabled": True, "complete_on_archive": True, "command": "td"}}}


def prepared_job(store, fake):
    store.enqueue("todoist:T1:initial", "todoist", "T1", "Brief", "agent", fake.task)
    store.db.execute("UPDATE jobs SET created='2026-10-03 05:29:00'")
    store.db.commit()
    job = store.rows()[0]
    store.update(job["id"], status="ready")
    return store.rows()[0]


def archive_after_agent_completion(store, fake, monkeypatch):
    monkeypatch.setattr(hermes_auto_todoist.subprocess, "run", fake.run)
    job = prepared_job(store, fake)
    hermes = Archive()
    hermes.archived[job["session_id"]] = False
    installed = {"todoist": hermes_auto_todoist.Source}
    assert sync_finished(store, CONFIG, lambda: hermes, installed) == []  # seen open
    fake.complete()  # the agent finished the work and ticked the task itself
    hermes.archived[job["session_id"]] = True  # later the user archives the conversation
    for _ in range(3):
        assert sync_finished(store, CONFIG, lambda: hermes, installed) == []
    return store.rows()[0]


def test_one_off_completed_by_agent_is_not_completed_again(store, monkeypatch):
    fake = FakeTd("2026-10-03", recurring=False)
    job = archive_after_agent_completion(store, fake, monkeypatch)
    assert fake.writes == []
    assert job["status"] == "done" and "already completed" in job["error"]


def test_daily_occurrence_completed_by_agent_keeps_next_one_open(store, monkeypatch):
    fake = FakeTd("2026-10-03T07:30:00", recurring=True, next_due="2026-10-04T07:30:00")
    job = archive_after_agent_completion(store, fake, monkeypatch)
    assert fake.writes == [] and fake.task["due"]["date"] == "2026-10-04T07:30:00"
    assert job["status"] == "done"


def test_same_day_next_occurrence_is_not_closed_by_archive(store, monkeypatch):
    # e.g. "every 12 hours": completing 07:30 leaves the next occurrence on the same day.
    fake = FakeTd("2026-10-03T07:30:00", recurring=True, next_due="2026-10-03T19:30:00")
    job = archive_after_agent_completion(store, fake, monkeypatch)
    assert fake.writes == [] and fake.task["due"]["date"] == "2026-10-03T19:30:00"
    assert job["status"] == "done"


def test_archive_without_agent_completion_still_completes_once(store, monkeypatch):
    fake = FakeTd("2026-10-03T07:30:00", recurring=True, next_due="2026-10-04T07:30:00")
    monkeypatch.setattr(hermes_auto_todoist.subprocess, "run", fake.run)
    job = prepared_job(store, fake)
    hermes = Archive()
    hermes.archived[job["session_id"]] = False
    installed = {"todoist": hermes_auto_todoist.Source}
    sync_finished(store, CONFIG, lambda: hermes, installed)
    hermes.archived[job["session_id"]] = True
    for _ in range(3):
        sync_finished(store, CONFIG, lambda: hermes, installed)
    assert fake.writes == [["td", "task", "complete", "id:T1"]]
    assert fake.task["due"]["date"] == "2026-10-04T07:30:00"


def test_completion_before_preparation_does_not_block_archive(store, monkeypatch):
    # The previous occurrence's completion (before this job was enqueued) is not this occurrence.
    fake = FakeTd("2026-10-03T07:30:00", recurring=True, next_due="2026-10-04T07:30:00")
    fake.events.append({"objectId": "T1", "eventDate": "2026-10-02T08:00:00Z"})
    fake.events.append({"objectId": "OTHER", "eventDate": "2026-10-03T09:00:00Z"})
    monkeypatch.setattr(hermes_auto_todoist.subprocess, "run", fake.run)
    job = prepared_job(store, fake)
    outcome = hermes_auto_todoist.finish_task("td", job)
    assert fake.writes == [["td", "task", "complete", "id:T1"]] and "task completed" in outcome
