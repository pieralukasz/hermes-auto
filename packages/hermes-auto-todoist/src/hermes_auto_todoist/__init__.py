from __future__ import annotations

import datetime as dt
import json
import subprocess
import shutil


def command_run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed (exit {result.returncode}); check its authentication")


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


MODES = ("open", "draft", "research", "agent")


def task_mode(task, default):
    selected = [mode for mode in MODES
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
    # One label (hermes) runs the full agent. Legacy mode labels still opt in and pick their mode.
    opt_in = " | ".join(f"@{name}" for name in [label, *(f"hermes-{mode}" for mode in MODES)])
    # Waiting or blocked tasks keep the label but do not start until the label is removed.
    listed = items(run([td, "task", "list", "--filter",
                        f"({opt_in}) & (today | overdue) & !@czekam & !@zablokowane", "--all", "--json"]))
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
                                    "AND status IN ('pending','created','retry_pending') "
                                    "AND json_extract(payload,'$.triage_run') IS NULL").fetchall():
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
                          task_mode(task, config["todoist"].get("mode", "agent")), task)
        if recurring:
            store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                             ("todoist_since", json.dumps((dt.date.today() - dt.timedelta(days=1)).isoformat())))


def utc_stamp(value):
    stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def completed_after(td, task_id, since, read):
    """Whether Todoist logged a completion of this task at or after `since` (the job's UTC enqueue time)."""
    start = utc_stamp(since)
    cursor = None
    seen_cursors = set()
    while True:
        # One day earlier: the activity filter takes a date, possibly in the account's timezone.
        command = [td, "activity", "--type", "task", "--event", "completed",
                   "--since", (start.date() - dt.timedelta(days=1)).isoformat(), "--limit", "100", "--json"]
        if cursor:
            command += ["--cursor", cursor]
        page = read(command)
        for event in items(page):
            if str(event["objectId"]) == task_id and utc_stamp(event["eventDate"]) >= start:
                return True
        cursor = page.get("nextCursor")
        if not cursor:
            return False
        if cursor in seen_cursors:
            raise RuntimeError("Todoist repeated a pagination cursor")
        seen_cursors.add(cursor)


def finish_task(td, job, read=command_json, write=command_run):
    """Complete the task the user marked done by archiving its session; skip what is already closed.

    A recurring task is completed only while it still shows the occurrence this job prepared: its due
    date then equals the one recorded at enqueue time, and Todoist has logged no completion of the task
    since the job was enqueued (the agent may already have ticked it). Otherwise completing again would
    close a later occurrence, which can fall on the same day for sub-daily recurrences.
    """
    task_id = job["external_id"]
    # Without --full, td omits `checked` and `isDeleted`.
    task = read([td, "task", "view", f"id:{task_id}", "--json", "--full"])
    if task.get("isDeleted"):
        return "Archived in Hermes; the task was deleted in Todoist"
    if task.get("checked"):
        return "Archived in Hermes; the task was already completed in Todoist"
    due = task.get("due") or {}
    if due.get("isRecurring"):
        # Compare days: changing only today's time keeps the occurrence; completing moves the day.
        prepared = (((json.loads(job["payload"]).get("due")) or {}).get("date") or "")[:10]
        if (due.get("date") or "")[:10] != prepared:
            return "Archived in Hermes; this occurrence was already completed or moved in Todoist"
        if completed_after(td, task_id, job["created"], read):
            return "Archived in Hermes; this occurrence was already completed in Todoist"
    write([td, "task", "complete", f"id:{task_id}"])
    return "Archived in Hermes; task completed in Todoist"


def inbox_project_id(td, read=command_json):
    for project in items(read([td, "project", "list", "--all", "--json"])):
        if "/project/inbox-" in str(project.get("url", "")) or project.get("inboxProject"):
            return str(project["id"])
    raise RuntimeError("Todoist Inbox project not found")


def inbox_tasks(td, read=command_json):
    return items(read([td, "task", "list", "--project", f"id:{inbox_project_id(td, read)}", "--all", "--json"]))


def filter_tasks(td, names, read=command_json):
    """Tasks of the named saved filters, deduplicated by id; `_filter` names the first one that listed it."""
    saved = {f["name"]: f["id"] for f in items(read([td, "filter", "list", "--json"]))}
    out, seen = [], set()
    for name in names:
        if name not in saved:
            continue
        for task in items(read([td, "filter", "view", f"id:{saved[name]}", "--all", "--json"])):
            if str(task["id"]) not in seen:
                seen.add(str(task["id"]))
                out.append({**task, "_filter": name})
    return out


def waiting_tasks(td, labels, read=command_json):
    """Tasks waiting on something (@czekam, @zablokowane) with their last three comments."""
    query = " | ".join(f"@{label}" for label in labels)
    tasks = items(read([td, "task", "list", "--filter", query, "--all", "--json"]))
    for task in tasks:
        comments = items(read([td, "comment", "list", f"id:{task['id']}", "--all", "--json"]))
        task["comments"] = [" ".join(str(c.get("content") or "").split())[:300] for c in comments][-3:]
    return tasks


def add_comment(td, task_id, text, write=command_run):
    write([td, "comment", "add", f"id:{task_id}", "--content", text])


def build_outline(td, read=command_json):
    """Lists with their path, description, sections and a few task titles: the sorter's map."""
    projects = items(read([td, "project", "list", "--all", "--json"]))
    by_id = {str(project["id"]): project for project in projects}

    def path(project):
        names, seen = [], set()
        while project and str(project["id"]) not in seen:
            seen.add(str(project["id"]))
            names.append(project["name"])
            project = by_id.get(str(project.get("parentId"))) if project.get("parentId") else None
        return " › ".join(reversed(names))

    samples = {}
    for task in items(read([td, "task", "list", "--all", "--json"])):
        bucket = samples.setdefault(str(task.get("projectId")), [])
        if len(bucket) < 6 and not task.get("parentId"):
            bucket.append(" ".join(str(task.get("content", "")).split())[:80])
    outline = []
    for project in projects:
        if "/project/inbox-" in str(project.get("url", "")) or project.get("isArchived"):
            continue
        sections = items(read([td, "section", "list", "--project", f"id:{project['id']}", "--all", "--json"]))
        outline.append({"id": str(project["id"]), "path": path(project),
                        "description": " ".join(str(project.get("description") or "").split()),
                        "sections": [{"id": str(s["id"]), "name": s["name"]} for s in sections],
                        "samples": samples.get(str(project["id"]), [])})
    return {"projects": outline}


def apply_decision(td, task, decision, agent_label, read=command_json, write=command_run):
    """Move, prioritise and label one Inbox task; read it back. Returns what was changed."""
    applied = {}
    ref = f"id:{task['id']}"
    task = read([td, "task", "view", ref, "--full", "--json"])
    if task.get("checked") or task.get("isDeleted"):
        raise RuntimeError(f"Task {task['id']} is no longer active")
    # Tasks already filed (Do organizacji, current agent tasks) may be planned in place: no move then.
    same_place = (decision.get("project_id") == str(task.get("projectId"))
                  and (decision.get("section_id") or None) == (task.get("sectionId") or None))
    if decision.get("priority"):
        write([td, "task", "update", ref, "--priority", decision["priority"]])
        applied["priority"] = decision["priority"]
    recurring = bool((task.get("due") or {}).get("isRecurring"))
    labels = list(task.get("labels", []))
    wanted = ([agent_label] if decision.get("agent") else []) + list(decision.get("extra_labels") or [])
    added = [label for label in wanted if label not in labels]
    if added:
        write([td, "task", "update", ref, "--labels", ",".join(labels + added)])
        applied["labels"] = added
    if decision.get("agent"):
        applied["agent"] = True
        # No due date: the scheduled poller would never pick it up, so the sorter may start it itself.
        applied["run"] = not task.get("due") and not recurring
    if decision.get("project_id") and not same_place:
        command = [td, "task", "move", ref, "--project", f"id:{decision['project_id']}"]
        if decision.get("section_id"):
            command += ["--section", f"id:{decision['section_id']}"]
        write(command)
        applied["project_id"] = decision["project_id"]
    current = read([td, "task", "view", ref, "--full", "--json"])
    if decision.get("project_id") and str(current.get("projectId")) != decision["project_id"]:
        raise RuntimeError(f"Task {task['id']} did not move; check Todoist")
    if decision.get("section_id") and str(current.get("sectionId")) != decision["section_id"]:
        raise RuntimeError(f"Task {task['id']} section was not updated")
    if decision.get("priority") and current.get("priority") != 5 - int(decision["priority"][1]):
        raise RuntimeError(f"Task {task['id']} priority was not updated")
    if not set(labels + added).issubset(current.get("labels", [])):
        raise RuntimeError(f"Task {task['id']} labels were not preserved")
    return applied


class Source:
    @staticmethod
    def defaults():
        return {"enabled": True, "label": "hermes", "mode": "agent", "complete_on_archive": True,
                "command": shutil.which("td") or "td"}

    def inbox_tasks(self, settings):
        return inbox_tasks(settings["command"])

    def task(self, settings, task_id):
        return command_json([settings["command"], "task", "view", f"id:{task_id}", "--full", "--json"])

    def filter_tasks(self, settings, names):
        return filter_tasks(settings["command"], names)

    def waiting_tasks(self, settings, labels):
        return waiting_tasks(settings["command"], labels)

    def add_comment(self, settings, task_id, text):
        add_comment(settings["command"], task_id, text)

    def outline(self, settings, store, max_age_hours=24):
        cached = store.get_meta("todoist_outline")
        if cached and dt.datetime.now().timestamp() - cached["at"] < max_age_hours * 3600:
            return cached["outline"]
        outline = build_outline(settings["command"])
        store.set_meta("todoist_outline", {"at": dt.datetime.now().timestamp(), "outline": outline})
        return outline

    def apply(self, settings, task, decision, agent_label):
        return apply_decision(settings["command"], task, decision, agent_label)

    def finish(self, config, job):
        return finish_task(config["sources"]["todoist"]["command"], job)

    def poll(self, config, store):
        source = config["sources"]["todoist"]
        poll_todoist({**config, "td_command": source["command"], "todoist": source}, store)

    def setup(self, config):
        source = config["sources"]["todoist"]
        command = source["command"]
        existing = {label["name"] for label in items(command_json([command, "label", "list", "--json"]))}
        for label in [source["label"]]:
            if label not in existing:
                command_json([command, "label", "create", "--name", label, "--json"])
