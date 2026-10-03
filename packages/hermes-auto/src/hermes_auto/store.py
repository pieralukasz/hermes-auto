from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import sqlite3
import uuid
from pathlib import Path


def session_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:12]


@contextlib.contextmanager
def locked(home: Path):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(home / "run.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another hermes-auto operation is running; try again later") from exc
        yield


class Store:
    def __init__(self, home: Path, *, initialize: bool = False, read_only: bool = False):
        self.path = home / "state.sqlite3"
        marker = home / "initialized"
        if not self.path.exists() and (marker.exists() or not initialize):
            raise RuntimeError("Automation state is missing. Restore its backup; refusing to start from zero.")
        self.db = (sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) if read_only
                   else sqlite3.connect(self.path))
        self.db.row_factory = sqlite3.Row
        if read_only:
            return
        os.chmod(self.path, 0o600)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL,
                mode TEXT NOT NULL, payload TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(event_key, generation)
            );
            CREATE TABLE IF NOT EXISTS watched_streams (
                account TEXT NOT NULL, stream_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(account, stream_id)
            );
            CREATE TABLE IF NOT EXISTS seen_items (
                account TEXT NOT NULL, item_id TEXT NOT NULL, PRIMARY KEY(account, item_id)
            );
        """)
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Automation database is damaged; restore a backup")
        marker.touch(mode=0o600)

    def close(self):
        self.db.close()

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def enqueue(self, event_key, source, external_id, title, mode, payload):
        self.db.execute(
            "INSERT INTO jobs(event_key,source,external_id,title,mode,payload,session_id) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_key,generation) DO UPDATE SET "
            "status='pending',title=excluded.title,mode=excluded.mode,payload=excluded.payload,error='' "
            "WHERE jobs.status='deferred'",
            (event_key, source, external_id, title, mode, json.dumps(payload, ensure_ascii=False), session_id()),
        )
        # Caller commits the event and its cursor together.

    def rows(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY id")]

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"No job {job_id}")
        return dict(row)

    def update(self, job_id, **fields):
        allowed = {"status", "error", "attempts", "session_id"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid state fields")
        assignments = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE jobs SET {assignments},updated=CURRENT_TIMESTAMP WHERE id=?",
                        (*fields.values(), job_id))
        self.db.commit()

    def recover(self):
        self.db.execute("UPDATE jobs SET status='needs_attention', error=? WHERE status IN ('running','creating')",
                        ("Runner stopped before confirming completion. Inspect session before retrying.",))
        self.db.commit()

    def new_generation(self, job_id):
        job = self.job(job_id)
        generation = self.db.execute("SELECT MAX(generation)+1 FROM jobs WHERE event_key=?",
                                     (job["event_key"],)).fetchone()[0]
        self.db.execute(
            "INSERT INTO jobs(event_key,generation,source,external_id,title,mode,payload,session_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job["event_key"], generation, job["source"], job["external_id"], job["title"],
             job["mode"], job["payload"], session_id()),
        )
        self.db.commit()

    def backup(self):
        destination = self.path.with_name("state.backup.sqlite3")
        with sqlite3.connect(destination) as backup:
            self.db.backup(backup)
        os.chmod(destination, 0o600)
