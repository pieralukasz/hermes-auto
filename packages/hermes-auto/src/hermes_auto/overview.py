"""Read-only views for a status app: what the sorter decided, and whether each source is alive.

Bodies of mail and messages never leave here; only headers, the sorter's own reason and summary,
and links. Everything reads the store and the listener heartbeat files; nothing writes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# Decisions an item can be opened from by hand: the sorter did not give it a session.
OPENABLE = ("skip", "brief", "read")


def _title(payload):
    # `name`: the short name the sorter gave it (Haiku); older items have only their subject.
    return str(payload.get("name") or payload.get("subject") or payload.get("content") or payload.get("snippet")
               or "")[:200]


def events(store, limit=200):
    """Newest sorter decisions first, each with the session it led to (if any)."""
    rows = store.db.execute(
        "SELECT t.rowid AS rowid, t.account, t.item_id, t.kind, t.decision, t.reason, t.summary, t.payload, "
        "t.created, j.id AS job_id, j.status AS job_status, j.session_id AS job_session "
        "FROM triage_items t LEFT JOIN jobs j ON j.id = ("
        "  SELECT id FROM jobs WHERE event_key = t.account || ':' || t.item_id ORDER BY generation DESC LIMIT 1) "
        "ORDER BY t.created DESC, t.rowid DESC LIMIT ?", (int(limit),)).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        source = row["account"].split(":", 1)[0]
        session = row["job_session"]
        if not session and row["decision"] == "followup":
            # A reply routed into an existing session: its record names that session.
            followup = store.db.execute("SELECT session_id FROM followups WHERE event_key=?",
                                        (f"{row['account']}:{row['item_id']}",)).fetchone()
            session = followup[0] if followup else None
        result.append({
            "account": row["account"], "item_id": row["item_id"], "source": source, "kind": row["kind"],
            "decision": row["decision"], "reason": row["reason"], "summary": row["summary"],
            "created": row["created"], "title": _title(payload),
            "from": str(payload.get("from") or "")[:200], "place": str(payload.get("place") or "")[:200],
            "url": str(payload.get("url") or ""), "job_id": row["job_id"], "job_status": row["job_status"],
            "session_id": session or None,
            "can_open": row["kind"] != "task" and row["decision"] in OPENABLE and row["job_id"] is None,
        })
    return result


def _listener(path: Path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return {key: data.get(key) for key in ("connected", "heartbeat", "last_event_at", "events_since_start")}


def sources(config, store, home: Path, now=None):
    """Per enabled source: last successful read, a pending retry, and its push listener's heartbeat."""
    now = time.time() if now is None else now
    result = {}
    for name, settings in sorted(config.get("sources", {}).items()):
        if not settings.get("enabled"):
            continue
        retry = store.get_meta(f"source_retry:{name}")
        result[name] = {
            "last_poll": store.get_meta(f"last_poll:{name}"),
            "retry_at": (retry or {}).get("next_at") if isinstance(retry, dict) else None,
            "listener": _listener(home / f"{name}-listener.json"),
        }
    relay = _listener(home / "relay-listener.json")
    return {"sources": result, "relay": relay, "last_check": store.get_meta("last_poll"), "now": now}
