"""Close the source item when the user archives the prepared Hermes session.

Archiving is the user's explicit "done" signal. Only an archive observed after the session was seen
open counts: sessions that were already archived when first observed (before this feature, or a
baseline after restoring state) never close anything on their own.
"""
from __future__ import annotations

WATCHED = ("ready", "needs_attention")


def seen_key(job_id):
    return f"archive_seen:{job_id}"


def mark_open(store, job_id):
    store.set_meta(seen_key(job_id), "open")


def finishable(job, config, installed, blocked):
    settings = config["sources"].get(job["source"], {})
    source = installed.get(job["source"])
    return (job["status"] in WATCHED and settings.get("enabled") and job["source"] not in blocked
            and settings.get("complete_on_archive", True) and source is not None
            and callable(getattr(source, "finish", None)))


def sync_finished(store, config, hermes_factory, installed, blocked=()):
    rows = store.rows()
    # A failed attempt superseded by `new-session` is not the user's result; only the latest counts.
    latest = {}
    for job in rows:
        latest[job["event_key"]] = max(latest.get(job["event_key"], 0), job["generation"])
    candidates = [job for job in rows if job["generation"] == latest[job["event_key"]]
                  and finishable(job, config, installed, blocked)]
    if not candidates:
        return []
    states = hermes_factory().archive_states(job["session_id"] for job in candidates)
    errors = []
    for job in candidates:
        state = states.get(job["session_id"])
        if state is None:
            continue  # Deleted sessions are handled by the runner, never treated as "done".
        key = seen_key(job["id"])
        if not state:
            if store.get_meta(key) != "open":
                mark_open(store, job["id"])
            continue
        if store.get_meta(key) != "open":
            # Archived before we ever saw it open: baseline only, no action.
            if store.get_meta(key) is None:
                store.set_meta(key, "archived_at_baseline")
            continue
        try:
            outcome = installed[job["source"]]().finish(config, job)
        except Exception as exc:
            errors.append(f"{job['source']}: finishing job {job['id']} failed: {exc}")
            store.update(job["id"], error=f"Archived, but closing the source item failed: {exc}")
            continue
        store.update(job["id"], status="done", error=outcome)
        store.set_meta(key, "finished")
    return errors
