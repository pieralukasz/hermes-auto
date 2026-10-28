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
            -- Triage: one row per inbox item the sorter has seen, with its decision.
            CREATE TABLE IF NOT EXISTS triage_items (
                account TEXT NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL,
                decision TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}', briefed INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(account, item_id)
            );
            -- Source items to mark read once the Hermes session (or brief) showing them is ready.
            CREATE TABLE IF NOT EXISTS read_marks (
                source TEXT NOT NULL, account TEXT NOT NULL, item_id TEXT NOT NULL, job_key TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source, account, item_id)
            );
            -- Replies routed into the existing Hermes session that sent the original message.
            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
                session_id TEXT NOT NULL, title TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # The bot (Hermes profile) a job's session lives in; '' = created before bots (the default profile).
        if "profile" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
            self.db.commit()
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
        allowed = {"status", "error", "attempts", "session_id", "profile"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid state fields")
        assignments = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE jobs SET {assignments},updated=CURRENT_TIMESTAMP WHERE id=?",
                        (*fields.values(), job_id))
        self.db.commit()

    def rebind_session(self, old, new):
        """Move every reference to session `old` (a job's reserved id) to `new` (the id Hermes gave it):
        the job, routed replies and the meta keys that hold a session id (a thread's session, the pending
        create), in one transaction. Other meta values are left alone even when equal to `old`."""
        with self.db:
            self.db.execute("UPDATE jobs SET session_id=?,updated=CURRENT_TIMESTAMP WHERE session_id=?", (new, old))
            self.db.execute("UPDATE followups SET session_id=?,updated=CURRENT_TIMESTAMP WHERE session_id=?",
                            (new, old))
            self.db.execute("UPDATE meta SET value=? WHERE value=? AND (key GLOB 'thread_session:*' OR key GLOB "
                            "'create:*')", (json.dumps(new), json.dumps(old)))

    def session_profile(self, session_id):
        """The bot profile of a session hermes-auto created ('default' for any other session, e.g. one a reply
        was traced to by its send receipt in the default profile)."""
        row = self.db.execute("SELECT profile FROM jobs WHERE session_id=? ORDER BY id DESC LIMIT 1",
                              (session_id,)).fetchone()
        return (row[0] if row else "") or "default"

    def background_runs(self):
        """IDs of `running` jobs that have a background run handle; `collect` settles them, not recovery."""
        return {job["id"] for job in self.rows() if job["status"] == "running" and self.get_meta(f"run:{job['id']}")}

    def recover(self, keep=None):
        """Jobs left mid-step by a stopped runner need a look; background runs (`keep`) are still tracked.

        Without `keep`, every job with a background run handle is kept: whether its process is still alive is
        decided by `Hermes.collect`, which marks a dead run needs_attention itself.
        """
        keep = self.background_runs() if keep is None else set(keep)
        for (job_id,) in self.db.execute("SELECT id FROM jobs WHERE status IN ('running','creating')").fetchall():
            if job_id not in keep:
                self.db.execute("UPDATE jobs SET status='needs_attention', error=? WHERE id=?",
                                ("Runner stopped before confirming completion. Inspect session before retrying.",
                                 job_id))
        # A reply that was running may already be in its session: never replay it on its own.
        self.db.execute("UPDATE followups SET status='needs_attention', error=?, updated=CURRENT_TIMESTAMP "
                        "WHERE status='running'",
                        ("Runner stopped before confirming the reply reached its session. Inspect the session, "
                         "then use retry-followup.",))
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
