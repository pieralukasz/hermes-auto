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
