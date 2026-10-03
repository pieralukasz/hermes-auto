from __future__ import annotations

import datetime as dt
import json
import subprocess
import shutil


def command_json(command: list[str]) -> dict:
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        # Credential-bearing stderr and task contents do not belong in service logs.
        raise RuntimeError(f"{command[0]} failed (exit {result.returncode}); check its authentication")
    return json.loads(result.stdout)


def items(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("Unexpected Todoist response; refusing incomplete discovery")
    return data["results"]


def task_mode(task, default):
    selected = [mode for mode in ("open", "draft", "research")
                if f"hermes-{mode}" in task.get("labels", [])]
    if len(selected) > 1:
        raise ValueError(f"Task {task['id']} has conflicting hermes mode labels")
    return selected[0] if selected else default


def due_time_reached(task, now=None):
    """A task with a due time waits for it; date-only tasks are due all day.

    Todoist returns floating times as naive local ISO strings and fixed-timezone
    times as UTC with a trailing Z.
    """
    value = (task.get("due") or {}).get("date") or ""
    if "T" not in value:
        return True
    due = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if due.tzinfo is None:
        current = now.replace(tzinfo=None) if now and now.tzinfo else (now or dt.datetime.now())
        return current >= due
    current = now if now and now.tzinfo else (now or dt.datetime.now()).astimezone()
    return current >= due


def poll_todoist(config, store, run=command_json, now=None):
    td = config["td_command"]
    label = config["todoist"]["label"]
    listed = items(run([td, "task", "list", "--filter", f"@{label} & (today | overdue)", "--all", "--json"]))
    # Before its due time a task is treated like one scheduled later: not yet opted in.
    tasks = [task for task in listed if due_time_reached(task, now)]
    recurring = [t for t in tasks if (t.get("due") or {}).get("isRecurring")]
    completions = {}
    if recurring:
        # Replay overlap, deduplicate by timestamp. Only a completion starts a new occurrence.
        # No event IDs: some td versions render large API IDs as lossy floating point numbers.
        since = store.get_meta("todoist_since", dt.date.today().isoformat())
        cursor = None
        seen_cursors = set()
        while True:
            command = [td, "activity", "--type", "task", "--event", "completed", "--since", since,
                       "--limit", "100", "--json"]
            if cursor:
                command += ["--cursor", cursor]
            page = run(command)
            for event in items(page):
                task_id = str(event["objectId"])
                stamp = event["eventDate"]
                completions[task_id] = max(completions.get(task_id, ""), stamp)
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise RuntimeError("Todoist repeated a pagination cursor")
            seen_cursors.add(cursor)
    with store.db:
        live_ids = {str(task["id"]) for task in tasks}
        for row in store.db.execute("SELECT id,external_id,status FROM jobs WHERE source='todoist' "
                                    "AND status IN ('pending','created','retry_pending')").fetchall():
            if row["external_id"] not in live_ids:
                status = "deferred" if row["status"] == "pending" else "needs_attention"
                store.db.execute("UPDATE jobs SET status=?,error=? WHERE id=?",
                                 (status, "Task is no longer opted in and due", row["id"]))
        for task in tasks:
            task_id = str(task["id"])
            recurrence = bool((task.get("due") or {}).get("isRecurring"))
            occurrence = "once"
            if recurrence:
                previous = store.get_meta(f"completion:{task_id}", "initial")
                occurrence = max(previous, completions.get(task_id, previous)) if previous != "initial" else (
                    completions.get(task_id, "initial"))
                store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                 (f"completion:{task_id}", json.dumps(occurrence)))
            # Migration tombstones deliberately suppress a known task until explicitly reset.
            if store.get_meta(f"legacy:{task_id}", False):
                continue
            event_key = f"todoist:{task_id}:{occurrence}"
            if recurrence:
                # A completed occurrence may still be queued (scan-only mode or a backlog).
                # Do not prepare that obsolete occurrence alongside today's one.
                store.db.execute("UPDATE jobs SET status='cancelled',error=? WHERE source='todoist' "
                                 "AND external_id=? AND event_key<>? "
                                 "AND status IN ('pending','deferred','created','retry_pending')",
                                 ("Recurring occurrence was completed before preparation", task_id, event_key))
            store.enqueue(event_key, "todoist", task_id, task["content"],
                          task_mode(task, config["default_mode"]), task)
        if recurring:
            store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                             ("todoist_since", json.dumps((dt.date.today() - dt.timedelta(days=1)).isoformat())))


class Source:
    @staticmethod
    def defaults():
        return {"enabled": True, "label": "hermes", "command": shutil.which("td") or "td"}

    def poll(self, config, store):
        source = config["sources"]["todoist"]
        poll_todoist({**config, "td_command": source["command"], "todoist": source}, store)

    def setup(self, config):
        source = config["sources"]["todoist"]
        command = source["command"]
        existing = {label["name"] for label in items(command_json([command, "label", "list", "--json"]))}
        for label in [source["label"], "hermes-open", "hermes-draft", "hermes-research"]:
            if label not in existing:
                command_json([command, "label", "create", "--name", label, "--json"])
