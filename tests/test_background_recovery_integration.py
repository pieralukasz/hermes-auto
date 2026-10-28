"""Background agent runs (main 9a7b1c3) meet the strict recovery of the review branch.

A check recovers interrupted work before polling. Only work that really stopped may become
needs_attention: a background run that is still alive stays `running` and is collected later.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import pytest

from hermes_auto import cli
from hermes_auto.config import defaults
from hermes_auto.hermes import Hermes
from hermes_auto.runner import process_jobs
from hermes_auto.store import Store

TASK = {"id": "T", "content": "Zrób raport VAT", "description": "z KSeF"}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, initialize=True)
    yield result
    result.close()


@pytest.fixture
def config():
    return {**defaults(), "language": "Polish", "sources": {}, "triage": {"enabled": False}}


class BackgroundHermes:
    """Fake bridge: `start` hands back a handle, `collect` returns None until `finished` is set."""

    instances = []

    def __init__(self, config=None, home=None, messages=0):
        self.messages, self.finished, self.error = messages, None, None
        self.creates, self.started, self.runs, self.collected = [], [], [], []
        BackgroundHermes.instances.append(self)

    def create(self, job, prompt):
        self.creates.append(prompt)

    def exists(self, job):
        return True

    def message_count(self, job):
        return self.messages

    def inspect(self, session_id, profile=None):
        return {"exists": True, "last_active": None}

    def unarchive(self, session_id):
        pass

    def archive_states(self, sessions):  # (session id, bot profile) pairs
        return {sid: False for sid, _profile in sessions}

    def run(self, job, prompt):  # synchronous followup runs
        self.runs.append(job["id"])
        return {"session_id": job["session_id"]}

    def start(self, job, prompt, display=None):
        self.started.append(prompt)
        return {"pid": 1, "dir": "fake", "deadline": time.time() + 900}

    def collect(self, handle):
        self.collected.append(handle)
        if self.error:
            raise RuntimeError(self.error)
        return self.finished


def running_job(store):
    """A job whose run was started in the background by an earlier check."""
    store.enqueue("todoist:T:once", "todoist", "T", "Zrób raport VAT", "agent", TASK)
    store.db.commit()
    job = store.rows()[0]
    store.update(job["id"], status="running", attempts=1)
    store.set_meta(f"run:{job['id']}", {"pid": 1, "dir": "fake", "deadline": time.time() + 900})
    return store.job(job["id"])


def add_followup(store, status, session):
    store.db.execute("INSERT INTO followups(event_key,source,session_id,title,payload,status) VALUES (?,?,?,?,?,?)",
                     (f"gmail:me:M1:{status}", "gmail", session, "Re: Faktura",
                      json.dumps({"id": "M1", "subject": "Re: Faktura", "from": "jan@kontrahent.test",
                                  "date": "Fri, 23 Oct 2026 10:00:00 +0200", "body": "Dzień dobry"}), status))
    store.db.commit()
    return store.db.execute("SELECT MAX(id) FROM followups").fetchone()[0]


def followup_status(store, row_id):
    return store.db.execute("SELECT status FROM followups WHERE id=?", (row_id,)).fetchone()[0]


def check(store, config, tmp_path, monkeypatch, hermes):
    monkeypatch.setattr(cli, "Hermes", lambda *_: hermes)
    cli.dispatch(argparse.Namespace(command="run"), tmp_path, config, store)


def test_scheduled_check_keeps_a_live_background_run_and_collects_it_later(store, config, tmp_path, monkeypatch):
    job = running_job(store)
    hermes = BackgroundHermes()
    check(store, config, tmp_path, monkeypatch, hermes)
    assert store.job(job["id"])["status"] == "running"  # recovery before poll left the live run alone
    assert store.get_meta(f"run:{job['id']}")
    assert hermes.collected  # it was looked at, still working
    hermes.finished = {"session_id": job["session_id"]}
    check(store, config, tmp_path, monkeypatch, hermes)
    assert store.job(job["id"])["status"] == "ready"
    assert not store.get_meta(f"run:{job['id']}")


def test_scheduled_check_marks_a_dead_background_run_needs_attention(store, config, tmp_path, monkeypatch):
    job = running_job(store)
    runs = tmp_path / "runs"
    runs.mkdir()
    directory = runs / "job-1-dead"
    directory.mkdir()
    gone = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    gone.wait()
    store.set_meta(f"run:{job['id']}", {"pid": gone.pid, "dir": str(directory), "deadline": time.time() + 900})
    real = Hermes.__new__(Hermes)
    real.home, real.config = tmp_path, config
    hermes = BackgroundHermes()
    hermes.collect = real.collect
    check(store, config, tmp_path, monkeypatch, hermes)
    assert store.job(job["id"])["status"] == "needs_attention"
    assert "exit code" in store.job(job["id"])["error"]
    assert not store.get_meta(f"run:{job['id']}")


def test_job_running_without_a_background_handle_is_still_recovered(store, config, tmp_path, monkeypatch):
    job = running_job(store)
    store.set_meta(f"run:{job['id']}", None)  # a synchronous run from before the upgrade, interrupted
    check(store, config, tmp_path, monkeypatch, BackgroundHermes())
    assert store.job(job["id"])["status"] == "needs_attention"


def test_reply_waits_while_a_background_run_holds_its_session(store, config, tmp_path, monkeypatch):
    job = running_job(store)
    reply = add_followup(store, "pending", job["session_id"])
    hermes = BackgroundHermes()
    check(store, config, tmp_path, monkeypatch, hermes)
    assert hermes.runs == []  # no second writer in a conversation the agent is still working in
    assert followup_status(store, reply) == "pending"
    assert store.job(job["id"])["status"] == "running"


def test_interrupted_reply_is_recovered_while_the_background_run_is_kept(store, config, tmp_path, monkeypatch):
    job = running_job(store)
    reply = add_followup(store, "running", "OTHER")
    check(store, config, tmp_path, monkeypatch, BackgroundHermes())
    assert followup_status(store, reply) == "needs_attention"
    assert store.job(job["id"])["status"] == "running"


def test_empty_session_retry_starts_the_original_task_in_the_background(store, config, tmp_path, monkeypatch):
    store.enqueue("todoist:T:once", "todoist", "T", "Zrób raport VAT", "agent", TASK)
    store.db.commit()
    job = store.rows()[0]
    store.update(job["id"], status="retry_pending")  # after needs_attention + `hermes-auto retry`
    hermes = BackgroundHermes(messages=0)
    process_jobs(store, {**config, "sources": {"todoist": {"enabled": True}}}, hermes)
    assert len(hermes.creates) == 1 and len(hermes.started) == 1
    assert "Zrób raport VAT" in hermes.started[0] and "z KSeF" in hermes.started[0]
    assert store.job(job["id"])["status"] == "running"
    check(store, config, tmp_path, monkeypatch, hermes)  # the next check must not flag the live retry
    assert store.job(job["id"])["status"] == "running"
    assert len(hermes.started) == 1  # and must not start it twice


# ── Process identity and file provenance of a background run ─────────────────

def bare_hermes(home, config):
    hermes = Hermes.__new__(Hermes)
    hermes.home, hermes.config = home, config
    return hermes


def run_dir(home, name="job-1-x"):
    directory = home / "runs" / name
    directory.mkdir(parents=True)
    return directory


def test_collect_does_not_take_an_unrelated_live_process_for_the_run(tmp_path, config):
    directory = run_dir(tmp_path)
    handle = {"pid": os.getpid(), "dir": str(directory), "deadline": time.time() + 900}  # a reused pid
    with pytest.raises(RuntimeError, match="exit code"):
        bare_hermes(tmp_path, config).collect(handle)


def test_collect_waits_for_the_run_process_it_started(tmp_path, config):
    directory = run_dir(tmp_path)
    process = subprocess.Popen(["/bin/sh", "-c", "sleep 30; :", "hermes-auto-run", str(directory / "request.json")],
                               start_new_session=True)
    try:
        handle = {"pid": process.pid, "dir": str(directory), "deadline": time.time() + 900}
        assert bare_hermes(tmp_path, config).collect(handle) is None
    finally:
        process.kill()
        process.wait()


def test_collect_refuses_a_run_directory_outside_its_runs_folder(tmp_path, config):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "exit_code").write_text("0")
    (outside / "keep.txt").write_text("x")
    with pytest.raises(RuntimeError, match="run directory"):
        bare_hermes(tmp_path / "home", config).collect({"pid": 1, "dir": str(outside), "deadline": 0})
    assert (outside / "keep.txt").exists()  # never read or removed


def test_start_records_a_handle_that_collect_accepts(tmp_path, config, monkeypatch):
    hermes = bare_hermes(tmp_path, config)
    hermes.python = sys.executable
    monkeypatch.setattr("hermes_auto.hermes.__file__", str(tmp_path / "hermes.py"))  # bridge.py beside it
    (tmp_path / "bridge.py").write_text(
        "import json\nprint(json.dumps({'type': 'result', 'session_id': 'S9'}))\n")
    handle = hermes.start({"id": 7, "source": "todoist", "mode": "agent", "payload": "{}"}, "prompt")
    for _ in range(100):
        result = hermes.collect(handle)
        if result is not None:
            break
        time.sleep(0.05)
    assert result["session_id"] == "S9"
    assert not os.path.exists(handle["dir"])
