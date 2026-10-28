"""The gateway names a run's session: the reserved id is the creation key and every reference follows."""
import json

import pytest

from hermes_auto.config import defaults
from hermes_auto.hermes import Hermes
from hermes_auto.runner import process_jobs
from hermes_auto.store import Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path, initialize=True)
    yield store
    store.close()


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {"example": {"enabled": True}}}


class FakeHermes:
    def __init__(self, fail_after_create=False):
        self.created, self.started, self.sessions = [], [], set()
        self.fail_after_create = fail_after_create

    def create(self, job, prompt):
        self.created.append(job["session_id"])
        new = "local-" + job["session_id"]
        self.sessions.add(new)
        if self.fail_after_create:
            self.fail_after_create = False
            raise RuntimeError("interrupted after the gateway committed")
        return new

    def exists(self, job):
        return job["session_id"] in self.sessions

    def message_count(self, job):
        return 0

    def start(self, job, prompt, display=None):
        self.started.append(job["session_id"])
        return {"pid": 0, "dir": "x", "deadline": 0}

    def collect(self, handle):
        return None


def test_every_reference_moves_to_the_gateway_session(store, config):
    store.enqueue("e1", "example", "1", "Title", "draft", {})
    store.db.commit()
    reserved = store.rows()[0]["session_id"]
    store.set_meta("thread_session:gmail:a:t1", reserved)
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload) VALUES ('f','gmail',?,'t','{}')",
                     (reserved,))
    store.db.commit()
    hermes = FakeHermes()
    process_jobs(store, config, hermes)
    job = store.rows()[0]
    assert job["session_id"] == "local-" + reserved and job["status"] == "running"
    assert hermes.started == ["local-" + reserved]
    assert store.get_meta("thread_session:gmail:a:t1") == "local-" + reserved
    assert store.db.execute("SELECT session_id FROM followups").fetchone()[0] == "local-" + reserved
    assert store.get_meta(f"create:{job['id']}") is None


def test_a_create_interrupted_after_the_gateway_committed_is_finished_not_called_deleted(store, config):
    store.enqueue("e1", "example", "1", "Title", "draft", {})
    store.db.commit()
    reserved = store.rows()[0]["session_id"]
    hermes = FakeHermes(fail_after_create=True)
    process_jobs(store, config, hermes)
    job = store.rows()[0]
    assert job["status"] == "needs_attention" and job["session_id"] == reserved
    store.update(job["id"], status="retry_pending")
    process_jobs(store, config, hermes)
    job = store.rows()[0]
    # The same creation key again: the gateway answers with the session it already made.
    assert hermes.created == [reserved, reserved]
    assert job["status"] == "running" and job["session_id"] == "local-" + reserved


def test_an_open_job_keeps_its_reserved_id(store, config):
    class Open(FakeHermes):
        def create(self, job, prompt):
            self.created.append(job["session_id"])
            return job["session_id"]
    store.enqueue("brief", "example", "b", "Brief", "open", {"open_message": "x"})
    store.db.commit()
    reserved = store.rows()[0]["session_id"]
    process_jobs(store, config, Open())
    assert store.rows()[0]["session_id"] == reserved and store.rows()[0]["status"] == "ready"


def test_a_bridge_refusal_reaches_the_job_as_written(tmp_path):
    hermes = object.__new__(Hermes)
    hermes.home = tmp_path
    stderr = "Traceback...\nhermes-auto-refusal: Refused: use `hermes-auto new-session 7`.\n"
    with pytest.raises(RuntimeError, match=r"^Refused: use `hermes-auto new-session 7`\.$"):
        hermes.parse("run", 3, "", stderr)
    assert "hermes-auto-refusal" in json.loads((tmp_path / "last-error.json").read_text())["error"]


def test_a_create_refused_until_the_guard_is_active_can_be_retried(store, config, tmp_path, monkeypatch):
    """The bridge refused the create (guard not loaded yet): nothing exists in Hermes, `create:<job>` stays,
    and `hermes-auto retry` after activation finishes that create instead of marking the job deleted."""
    import argparse

    from hermes_auto import cli

    class Refusing(FakeHermes):
        refuse = True

        def create(self, job, prompt):
            if self.refuse:
                raise RuntimeError("Refused: the running Hermes gateway has not loaded the hermes-auto-guard plugin")
            return super().create(job, prompt)
    hermes = Refusing()
    store.enqueue("e1", "example", "1", "Title", "draft", {})
    store.db.commit()
    reserved = store.rows()[0]["session_id"]
    process_jobs(store, config, hermes)
    job = store.rows()[0]
    assert job["status"] == "needs_attention" and "hermes-auto-guard" in job["error"]
    assert hermes.sessions == set() and store.get_meta(f"create:{job['id']}") == reserved
    monkeypatch.setattr(cli, "Hermes", lambda *args: hermes)
    cli.dispatch(argparse.Namespace(command="retry", job_id=job["id"]), tmp_path, config, store)
    assert store.rows()[0]["status"] == "retry_pending"
    hermes.refuse = False
    process_jobs(store, config, hermes)
    job = store.rows()[0]
    assert job["status"] == "running" and job["session_id"] == "local-" + reserved


def test_retry_of_a_session_deleted_by_the_user_still_refuses(store, config, tmp_path, monkeypatch):
    import argparse

    from hermes_auto import cli
    store.enqueue("e1", "example", "1", "Title", "draft", {})
    store.db.commit()
    job = store.rows()[0]
    store.update(job["id"], status="needs_attention")
    monkeypatch.setattr(cli, "Hermes", lambda *args: FakeHermes())
    with pytest.raises(ValueError, match="will not be recreated"):
        cli.dispatch(argparse.Namespace(command="retry", job_id=job["id"]), tmp_path, config, store)
    assert store.rows()[0]["status"] == "deleted"


def test_rebinding_moves_only_session_references(store):
    store.set_meta("thread_session:gmail:a:t1", "old-id")
    store.set_meta("create:7", "old-id")
    store.set_meta("cursor:example", "old-id")  # an unrelated value that happens to be equal
    store.set_meta("threadXsession:x", "old-id")  # `_` would be a LIKE wildcard
    store.rebind_session("old-id", "new-id")
    assert store.get_meta("threadXsession:x") == "old-id"
    assert store.get_meta("thread_session:gmail:a:t1") == "new-id" and store.get_meta("create:7") == "new-id"
    assert store.get_meta("cursor:example") == "old-id"
