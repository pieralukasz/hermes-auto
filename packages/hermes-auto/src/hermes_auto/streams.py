from __future__ import annotations

def observe_stream(store, *, source, account, stream_id, events, mode, active=True):
    """First observation is a baseline; only subsequently seen incoming replies create jobs.

    Message snapshots use provider-stable IDs and are committed atomically with their jobs.
    Read/unread flags and changing timestamps never affect deduplication.
    """
    identity = f"{source}:{account}"
    old = store.db.execute("SELECT active FROM watched_streams WHERE account=? AND stream_id=?",
                           (identity, stream_id)).fetchone()
    baseline = old is None or not old["active"]
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO watched_streams VALUES (?,?,?)",
                         (identity, stream_id, int(active)))
        if not active:
            return
        for event in events:
            item_id = event["id"]
            seen = store.db.execute("SELECT 1 FROM seen_items WHERE account=? AND item_id=?",
                                    (identity, item_id)).fetchone()
            if not seen and not baseline and event["eligible"]:
                payload = {**event["payload"], "stream_id": stream_id, "account": account}
                store.enqueue(f"{identity}:{item_id}", source, item_id,
                              event["title"], mode, payload)
            store.db.execute("INSERT OR IGNORE INTO seen_items VALUES (?,?)", (identity, item_id))


def deactivate_missing(store, identity, active_ids):
    with store.db:
        for row in store.db.execute("SELECT stream_id FROM watched_streams WHERE account=?", (identity,)):
            if row[0] not in active_ids:
                store.db.execute("UPDATE watched_streams SET active=0 WHERE account=? AND stream_id=?",
                                 (identity, row[0]))
                source, account = identity.split(":", 1)
                store.db.execute("UPDATE jobs SET status='cancelled',error=? WHERE source=? "
                                 "AND json_extract(payload,'$.account')=? "
                                 "AND json_extract(payload,'$.stream_id')=? "
                                 "AND status IN ('pending','created','retry_pending')",
                                 ("Source stream is no longer watched", source, account, row[0]))
