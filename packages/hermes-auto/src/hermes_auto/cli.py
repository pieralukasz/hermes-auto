from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import default_home, defaults, private_json, read_config, sources
from .finish import sync_finished
from .hermes import Hermes
from .runner import process_jobs
from .store import Store, locked


MAIL_LIKE = ("gmail", "proton", "slack", "github", "linear")


def poll(config, store, only=None):
    """Read every enabled source (or just `only`); returns errors as "name: message"."""
    installed = sources()
    errors = []
    for name, settings in config["sources"].items():
        if not settings.get("enabled") or (only is not None and name not in only):
            continue
        try:
            if name not in installed:
                raise RuntimeError(f"Install hermes-auto-{name}")
            if settings.get("mode", config["default_mode"]) not in ("open", "draft", "research", "agent"):
                raise ValueError("Unknown source mode")
            installed[name]().poll(config, store)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if only is None:
        store.set_meta("source_errors", errors)
    else:
        # A partial check replaces only its own sources' errors.
        kept = [error for error in store.get_meta("source_errors", []) if error.split(":", 1)[0] not in only]
        store.set_meta("source_errors", kept + errors)
    return errors


def run_triage(config, store, bridge, installed, blocked=(), only=None):
    """Sorter step: mail and Inbox tasks get decisions; replies return to their sessions; briefs.

    Returns (errors, failures). A source that failed to read keeps its cursor and retries later.
    """
    from . import triage
    options = triage.settings(config)
    if not options["enabled"]:
        return [], 0
    errors, failures = [], 0
    new_mail = []
    for name in MAIL_LIKE:
        settings = config["sources"].get(name, {})
        source = installed.get(name)
        if not settings.get("enabled") or name in blocked or source is None or not hasattr(source, "inbox"):
            continue
        if only is not None and name not in only:
            continue
        try:
            candidates, key, cursor = source().inbox(config, store, limit=options["max_mail_per_run"])
            # Items kept after a route's classifier failed are retried even when the source has nothing new.
            accounts = [candidates[0].get("account", "")] if candidates else triage.pending_accounts(store, name)
            if accounts:
                try:
                    report = triage.triage_mail(store, config, bridge(), name, accounts[0], candidates, key, cursor)
                except triage.RoutesPending as pending:
                    # The rest of the batch is recorded; "sorter <name>" is not a read failure, so the source
                    # is not throttled and the other routes' next items are not held back.
                    report = pending.report
                    errors.append(f"sorter {name}: {pending}")
                # Private-channel Slack items carry no content (redacted) and stay out of the waiting check.
                new_mail += [item for _, item, _ in report or [] if not item.get("redacted")]
            else:
                store.set_meta(key, cursor)
        except Exception as exc:
            errors.append(f"triage {name}: {exc}")
    todoist = installed.get("todoist")
    adapter = None
    # Todoist steps run on full checks, Todoist-only checks, and whenever new mail may resolve @czekam.
    todoist_turn = only is None or "todoist" in only or bool(new_mail)
    if (todoist_turn and options["todoist"]["enabled"] and config["sources"].get("todoist", {}).get("enabled")
            and "todoist" not in blocked and todoist is not None and hasattr(todoist, "inbox_tasks")):
        # Separate steps: the sorter raises after queuing its plan when a stored decision still fails to apply
        # (retried next check); that must not skip proposals or the waiting check for this check's new mail.
        try:
            adapter = todoist()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"triage todoist: {exc}")
            adapter = None
        for step, call in () if adapter is None else (("triage todoist", lambda: triage.triage_todoist(store, config, bridge(), adapter)),
                           ("proposals todoist", lambda: triage.propose_agent_tasks(store, config, adapter)),
                           ("waiting todoist", lambda: triage.check_waiting(store, config, bridge(), adapter,
                                                                            new_mail))):
            try:
                call()
            except Exception as exc:
                errors.append(f"{step}: {exc}")
    if store.db.execute("SELECT 1 FROM followups WHERE status IN ('pending','retry_pending')").fetchone():
        failures += triage.process_followups(store, config, bridge())
    try:
        triage.maybe_brief(store, config, hermes=bridge, adapter=adapter)
    except Exception as exc:  # noqa: BLE001 - a failed section source retries at the next check
        errors.append(f"brief: {exc}")
    return errors, failures


def triage_preview(config, store, home, days):
    """Read-only: what the sorter would decide for the last `days` of mail and the current Inbox."""
    from . import triage
    hermes = Hermes(config, home)
    installed = sources()
    since_ms = int((time.time() - days * 86400) * 1000)
    lines = [f"# Podgląd sortownika: ostatnie {days} dni", ""]
    for name in ("gmail", "proton"):
        if not config["sources"].get(name, {}).get("enabled") or name not in installed:
            continue
        candidates, _, _ = installed[name]().inbox(config, store, limit=400, since_ms=since_ms)
        routed, unknown = [], []
        for item in candidates:
            session = hermes.find_session(thread_id=item.get("thread_id"), subject=item.get("subject"),
                                          sent=item["sent"]) if item.get("sent") else None
            (routed.append((item, session)) if session else unknown.append(item))
        decisions = {}
        for start in range(0, len(unknown), 25):
            batch = unknown[start:start + 25]
            for index, verdict in triage.classify_mail(hermes, config, batch).items():
                decisions[start + index] = verdict
        counts = {}
        for verdict in decisions.values():
            counts[verdict["decision"]] = counts.get(verdict["decision"], 0) + 1
        lines += [f"## {name}: {len(candidates)} maili", "",
                  f"Do sesji nadawcy (odpowiedzi): {len(routed)}; " +
                  "; ".join(f"{key}: {value}" for key, value in sorted(counts.items())), ""]
        for item, session in routed:
            lines.append(f"- followup → `{session}` · {item['from']} · {item['subject']}")
        for decision in ("session", "brief", "skip"):
            rows = [(unknown[i], v) for i, v in sorted(decisions.items()) if v["decision"] == decision]
            if rows:
                lines += ["", f"### {decision} ({len(rows)})"]
                lines += [f"- {item['date'][:16]} · {item['from']} · {item['subject']}: {v['summary']} "
                          f"_({v['reason']})_" for item, v in rows]
        lines.append("")
    if config["sources"].get("todoist", {}).get("enabled") and "todoist" in installed:
        adapter = installed["todoist"]()
        tasks = [task for task in adapter.inbox_tasks(config["sources"]["todoist"]) if not task.get("parentId")]
        outline = adapter.outline(config["sources"]["todoist"], store, 24)
        decisions = triage.classify_tasks(hermes, config, tasks, outline) if tasks else {}
        lines += [f"## Todoist Inbox: {len(tasks)} zadań", ""]
        for task in tasks:
            d = decisions[str(task["id"])]
            extra = f" · pytanie: {d['question']}" + (f" (propozycja: {d['proposal']})" if d["proposal"] else "") \
                if d["question"] else ""
            lines.append(f"- **{task['content']}** → {d['project_path'] or 'Inbox'} · "
                         f"{(d['priority'] or '-').upper()} · agent: {'tak' if d['agent'] else 'nie'}"
                         f" ({d['agent_reason']}){extra}")
    report = home / "triage-preview.md"
    fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    return report


def retry_followup(store, hermes, followup_id):
    """Queue a failed reply again, into the same session, only after inspecting that session.

    Refuses when the session is gone (never recreated here) or already holds the reply's text (an
    interrupted run may have delivered it; replaying would duplicate it). Nothing runs now.
    """
    from .triage import one_line
    row = store.db.execute("SELECT * FROM followups WHERE id=?", (followup_id,)).fetchone()
    if row is None:
        raise ValueError(f"No followup {followup_id}")
    if row["status"] != "needs_attention":
        raise ValueError("Only needs_attention followups can be retried")
    item = json.loads(row["payload"])
    # The reply's Date and From header values exactly as the mail prompt prints them (`one_line`, raw From,
    # not a parsed address); both in one user message of the session's lineage = delivered.
    needles = [one_line(item.get("date")), one_line(item.get("from"))]
    if not all(needles):
        raise ValueError("Hermes Auto cannot tell whether this reply already reached its session; "
                         f"open session {row['session_id']} and continue there by hand")
    info = hermes.invoke("inspect", {"session_id": row["session_id"]}, needles=needles)

    def mark(status, error):
        store.db.execute("UPDATE followups SET status=?,error=?,updated=CURRENT_TIMESTAMP WHERE id=?",
                         (status, error, followup_id))
        store.db.commit()

    if not info["exists"]:
        mark("deleted", "Session is missing; the reply was not redirected or recreated")
        raise ValueError("The session was deleted; retry-followup will not recreate it")
    if not isinstance(info.get("delivered"), bool):
        mark("needs_attention", f"Could not verify whether the reply reached session {row['session_id']}; "
             "continue there by hand")
        raise ValueError("Hermes Auto cannot tell whether this reply already reached its session "
                         f"{row['session_id']}; open it and continue there by hand")
    if info.get("delivered") is not False:
        mark("needs_attention", f"The reply already reached session {row['session_id']}; continue there by hand")
        raise ValueError(f"The reply is already in session {row['session_id']}; open it instead of retrying")
    mark("retry_pending", "")


RETRY_FIRST_SECONDS, RETRY_MAX_SECONDS = 60, 1800


def source_failed(error):
    """The source an error line belongs to ("gmail: ...", "triage gmail: ...", "waiting todoist: ..."), if any.

    "sorter <name>: ..." (a model route failed to classify) is deliberately not a source failure: its items
    wait with their own retry delay (`triage.pending_due`) while the source keeps being read."""
    head = error.split(":", 1)[0].split()
    if len(head) == 1:
        return head[0]
    if len(head) == 2 and head[0] in ("triage", "proposals", "waiting"):
        return head[1]
    return None


def settle_checked(store, checked, read_errors, started):
    """After a check: sources read without error move their cursor to the check's *start*, so an event
    that arrived while it ran stays newer than the cursor. A failed source keeps its cursor (the nudge,
    backlog or wake still stands) and waits a growing delay before the next attempt."""
    failed = {source_failed(error) for error in read_errors} - {None}
    for name in checked:
        if name in failed:
            retry = store.get_meta(f"source_retry:{name}", None) or {}
            attempts = int(retry.get("attempts", 0)) + 1
            delay = min(RETRY_FIRST_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
            store.set_meta(f"source_retry:{name}", {"attempts": attempts, "next_at": time.time() + delay})
            continue
        store.set_meta(f"last_poll:{name}", started)
        store.db.execute("DELETE FROM meta WHERE key=?", (f"source_retry:{name}",))
        store.db.commit()


def fast_sources(config, store, now=None):
    """Sources due between the regular checks: the adapter's `due` (listener nudge, own interval, wake),
    or a backlog the source left last time (`source_backlog:<name>`). A source that failed waits out
    its retry delay (`source_retry:<name>`). Items a model route failed to classify make it due once their
    retry delay passed (`triage_pending:<name>:*`)."""
    from .triage import pending_due
    now = time.time() if now is None else now
    installed = sources()
    due = []
    for name, settings in config["sources"].items():
        if not settings.get("enabled"):
            continue
        retry = store.get_meta(f"source_retry:{name}", None)
        if retry and now < float(retry.get("next_at", 0)):
            continue
        if store.get_meta(f"source_backlog:{name}", False) or pending_due(store, name, now):
            due.append(name)
            continue
        last = store.get_meta(f"last_poll:{name}", 0)
        source = installed.get(name)
        if source is not None and hasattr(source, "due"):
            # The adapter decides (e.g. Slack: its listener saw a new message, or its own interval).
            if source().due(settings, store, last, now):
                due.append(name)
            continue
        seconds = settings.get("poll_seconds")
        if seconds and now - last >= seconds:
            due.append(name)
    return due


def brief_due(config, store, now=None):
    """A brief time passed since the last brief (push mode has no periodic check to notice it)."""
    from . import triage
    options = triage.settings(config)
    if not options["enabled"]:
        return False
    import datetime as dt
    slot = triage.current_slot(options["brief_times"], now or dt.datetime.now())
    return bool(slot) and store.get_meta("brief_slot") != slot


def eligible_tick(config, store):
    if config["desktop_only"]:
        result = subprocess.run(["pgrep", "-x", config["desktop_process"]],
                                capture_output=True, timeout=10)
        if result.returncode not in (0, 1):
            raise RuntimeError("Cannot detect the desktop process")
        if result.returncode == 1:
            store.set_meta("desktop_seen", False)
            return False
        opened = not store.get_meta("desktop_seen", False)
        store.set_meta("desktop_seen", True)
    else:
        opened = False
    return opened or time.time() - store.get_meta("last_poll", 0) >= config["poll_seconds"]


def install_launchd(home):
    if sys.platform != "darwin":
        raise RuntimeError("launchd is macOS-only; use a user timer to call 'hermes-auto tick' on Linux")
    entry = Path(sys.executable).parent / "hermes-auto"
    if not entry.exists():
        raise RuntimeError("Install the core package before installing the service")
    agent = Path.home() / "Library/LaunchAgents/io.github.hermes-auto.plist"
    payload = {
        "Label": "io.github.hermes-auto", "ProgramArguments": [str(entry), "--home", str(home), "tick"],
        "RunAtLoad": True, "StartInterval": 60, "AbandonProcessGroup": True,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())},
        "StandardOutPath": str(home / "service.log"), "StandardErrorPath": str(home / "service.log"),
    }
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(plistlib.dumps(payload))
    os.chmod(agent, 0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/io.github.hermes-auto"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(agent)], check=True)
    print(f"Installed {agent}; checks every 60 seconds while the configured desktop process is open")


def parser():
    root = argparse.ArgumentParser(description="Durable opt-in Hermes sessions from separately installed adapters")
    root.add_argument("--home", type=Path, default=default_home())
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create configuration and durable state once")
    commands.add_parser("doctor", help="Check the Hermes API bridge without an LLM call")
    commands.add_parser("install-guard", help="Copy the hermes-auto-guard plugin into the Hermes home "
                        "(does not enable or load it)")
    commands.add_parser("guard-reload", help="Ask the running Hermes gateway to reload its plugins "
                        "(loads hermes-auto-guard without a restart), then show the guard's state")
    commands.add_parser("status", help="Show jobs, source errors and pause state")
    events = commands.add_parser("events", help="Read-only: the sorter's latest decisions, newest first (JSON)")
    events.add_argument("--limit", type=int, default=200)
    opened = commands.add_parser("open-item", help="Explicitly give a skipped, briefed or read message its session")
    opened.add_argument("account", help="Sorter account, e.g. gmail:me@example.test (see 'events')")
    opened.add_argument("item_id")
    commands.add_parser("pause", help="Pause discovery and preparation")
    commands.add_parser("resume", help="Resume discovery and preparation")
    commands.add_parser("tick", help="Scheduled run, respecting desktop presence and polling interval")
    commands.add_parser("run", help="Discover events and prepare pending jobs now")
    scan = commands.add_parser("scan", help="Discover events without starting Hermes or spending LLM tokens")
    scan.add_argument("--dry-run", action="store_true", help="Use a temporary state copy; do not advance baselines")
    preview = commands.add_parser("triage-preview", help="Read-only: what the sorter would decide (spends LLM tokens)")
    preview.add_argument("--days", type=int, default=14)
    source = commands.add_parser("source", help="Enable or disable an installed adapter")
    source.add_argument("action", choices=["list", "enable", "disable", "setup"])
    source.add_argument("name", nargs="?")
    for name in ("retry", "new-session"):
        item = commands.add_parser(name, help="Explicitly continue a failed job" if name == "retry" else
                                   "Explicitly create a fresh conversation for an existing event")
        item.add_argument("job_id", type=int)
    again = commands.add_parser("retry-followup", help="Explicitly retry a failed reply (opens a new session)")
    again.add_argument("followup_id", type=int)
    commands.add_parser("install-launchd", help="Install a macOS user service")
    commands.add_parser("backup", help="Write a consistent backup of automation state")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    home = args.home.expanduser().resolve()
    try:
        if args.command in ("status", "events"):
            store = Store(home, read_only=True)
            try:
                return dispatch(args, home, read_config(home), store)
            finally:
                store.close()
        with locked(home):
            if args.command == "init":
                if not (home / "config.json").exists():
                    private_json(home / "config.json", defaults())
                store = Store(home, initialize=True)
                store.close()
                print(f"Initialized {home}. Install adapters, then run 'hermes-auto source enable NAME'.")
                return 0
            config = read_config(home)
            store = Store(home)
            try:
                return dispatch(args, home, config, store)
            finally:
                store.close()
    except Exception as exc:
        print(f"hermes-auto: {exc}", file=sys.stderr)
        return 1


def dispatch(args, home, config, store):
    command = args.command
    if command == "source":
        installed = sources()
        if args.action == "list":
            print(json.dumps({name: config["sources"].get(name, {"enabled": False})
                              for name in sorted(installed)}, indent=2))
            return 0
        if args.name not in installed:
            raise ValueError(f"Adapter {args.name!r} is not installed")
        if args.action == "setup":
            if args.name not in config["sources"]:
                raise ValueError("Enable the source before setting it up")
            installed[args.name]().setup(config)
            print(f"{args.name}: opt-in labels are ready (no tasks or threads were selected)")
            return 0
        settings = config["sources"].setdefault(args.name, installed[args.name].defaults())
        settings["enabled"] = args.action == "enable"
        private_json(home / "config.json", config)
        print(f"{args.name}: {args.action}d")
    elif command == "install-guard":
        from . import guard
        guard.install(config["hermes_home"])
        print(guard.activation_steps(config["hermes_home"]))
    elif command == "guard-reload":
        print(json.dumps(Hermes(config, home).invoke("reload_plugins"), indent=2))
    elif command == "doctor":
        print(json.dumps(Hermes(config, home).invoke("doctor"), indent=2))
        print("Installed adapters:", ", ".join(sorted(sources())) or "none")
    elif command == "status":
        jobs = [{key: job[key] for key in ("id", "source", "title", "mode", "status", "session_id", "error",
                                           "created", "updated")}
                for job in store.rows()]
        # Replies routed into existing sessions; payloads (mail bodies) stay out of CLI output.
        followups = [dict(row) for row in store.db.execute(
            "SELECT id, source, title, status, session_id, attempts, error FROM followups ORDER BY id")]
        # Sorter tasks it stopped retrying or left alone because the user changed them meanwhile.
        from .triage import stored_state
        issues = []
        for row in store.db.execute("SELECT item_id, decision, reason, payload FROM triage_items WHERE "
                                    "account='todoist' AND decision IN ('needs_attention','user_changed') "
                                    "ORDER BY created, rowid"):
            state = stored_state(row)
            task = state.get("task") or json.loads(row["payload"] or "{}")
            issues.append({"item_id": row["item_id"], "decision": row["decision"], "content": task.get("content", ""),
                           "attempts": state.get("attempts", 0), "error": state.get("error", "")})
        # Items whose model route failed to classify, waiting for a retry (not lost, not marked read).
        waiting = [{"source": key.split(":", 2)[1], "account": key.split(":", 2)[2], "items": len(state["items"]),
                    "attempts": state.get("attempts", 0), "error": state.get("error", ""),
                    "next_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.get("next_at", 0)))}
                   for key, state in ((row[0], json.loads(row[1])) for row in store.db.execute(
                       "SELECT key, value FROM meta WHERE key GLOB 'triage_pending:*'"))]
        from .overview import sources as health
        print(json.dumps({"paused": store.get_meta("paused", False),
                          "source_errors": store.get_meta("source_errors", []), "jobs": jobs,
                          "followups": followups, "triage_issues": issues, "triage_pending": waiting,
                          "health": health(config, store, home)},
                         indent=2, ensure_ascii=False))
    elif command == "events":
        from .overview import events
        print(json.dumps(events(store, args.limit), indent=2, ensure_ascii=False))
    elif command == "open-item":
        from .triage import open_item
        job_id = open_item(store, args.account, args.item_id)
        print(json.dumps({"job_id": job_id, "queued": True}))
    elif command in ("pause", "resume"):
        store.set_meta("paused", command == "pause")
        print(command + "d")
    elif command == "backup":
        store.backup()
        print(home / "state.backup.sqlite3")
    elif command == "install-launchd":
        install_launchd(home)
    elif command in ("retry", "new-session"):
        job = store.job(args.job_id)
        if command == "new-session":
            store.new_generation(job["id"])
        else:
            if job["status"] != "needs_attention":
                raise ValueError("Only needs_attention jobs can be retried; use new-session for a fresh conversation")
            if not Hermes(config, home).exists(job) and not store.get_meta(f"create:{job['id']}"):
                store.update(job["id"], status="deleted", error="Session is missing; use new-session explicitly")
                raise ValueError("The session was deleted; it will not be recreated by retry")
            # A create the bridge refused (or interrupted) left `create:<job>`: the retry finishes that
            # create with the same creation key instead of calling the session deleted.
            store.update(job["id"], status="retry_pending", error="")
        print("Queued. Run 'hermes-auto run' or wait for the next scheduled check.")
    elif command == "retry-followup":
        retry_followup(store, Hermes(config, home), args.followup_id)
        print("Queued; it opens a new session. Run 'hermes-auto run' or wait for the next scheduled check.")
    elif command == "triage-preview":
        with tempfile.TemporaryDirectory(prefix="hermes-auto-preview-") as directory:
            preview = Store(Path(directory), initialize=True)
            try:
                store.db.backup(preview.db)
                print(triage_preview(config, preview, home, args.days))
            finally:
                preview.close()
    elif command == "scan" and args.dry_run:
        with tempfile.TemporaryDirectory(prefix="hermes-auto-preview-") as directory:
            temporary_home = Path(directory)
            preview = Store(temporary_home, initialize=True)
            try:
                store.db.backup(preview.db)
                before = len(preview.rows())
                errors = poll(config, preview)
                print(json.dumps({"new_jobs": len(preview.rows()) - before, "errors": errors}, indent=2))
                return int(bool(errors))
            finally:
                preview.close()
    elif command in ("run", "scan", "tick"):
        if store.get_meta("paused", False):
            print("Paused")
            return 0
        # `only`: sources to read (None = all); `local`: also run local upkeep (archive -> done, last_poll).
        only, local = None, True
        if command == "tick":
            full = eligible_tick(config, store)
            if config["desktop_only"] and not store.get_meta("desktop_seen"):
                return 0
            if config.get("poll_sources", True) is False:
                # Push mode: a source is read only when its listener, webhook or timer says so (`due`);
                # the periodic check keeps only local upkeep (sessions, archive, briefs).
                only = fast_sources(config, store)
                local = full or brief_due(config, store)
                from . import triage as _triage
                busy = _triage.read_marks_waiting(store) or any(
                    j["status"] in ("pending", "created", "retry_pending", "running", "creating") for j in store.rows())
                if not only and not local and not busy:
                    return 0
            elif not full:
                # Not time for a full check; a source with its own interval or a listener nudge may be due.
                only = fast_sources(config, store)
                if not only:
                    return 0
                local = False
        started = time.time()
        if command != "scan":
            # Under the run lock anything still "running" was interrupted: jobs and replies alike go to
            # needs_attention before any work starts, so nothing is replayed without an explicit retry.
            store.recover()
        errors = poll(config, store, only)
        source_errors = list(errors)  # reading a source: poll and the sorter's inbox step
        failures = 0
        blocked = [error.split(":", 1)[0] for error in errors]
        hermes = None

        def bridge():
            nonlocal hermes
            hermes = hermes or Hermes(config, home)
            return hermes

        if command != "scan":
            triage_errors, triage_failures = run_triage(config, store, bridge, sources(), blocked, only)
            errors = errors + triage_errors
            source_errors += triage_errors
            failures += triage_failures
        if command != "scan" and any(j["status"] in ("pending", "created", "retry_pending", "running", "creating")
                                     for j in store.rows()):
            failures += process_jobs(store, config, bridge(), blocked_sources=blocked)
        if command != "scan":
            from . import triage as _triage
            read_errors = _triage.flush_read_marks(store, config, sources(), blocked)
            errors = errors + read_errors
        if command != "scan" and local:
            # Archiving a prepared session in Hermes closes its source item (Todoist: completes the task).
            finish_errors = sync_finished(store, config, bridge, sources(), blocked)
            failures += len(finish_errors)
            errors = errors + finish_errors
        if local:
            store.set_meta("last_poll", started)
        checked = [name for name in (only if only is not None else config["sources"])
                   if config["sources"].get(name, {}).get("enabled")]
        settle_checked(store, checked, source_errors, started)
        store.backup()
        counts = {}
        for job in store.rows():
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        print(json.dumps({"jobs": counts, "source_errors": errors, "failed_this_run": failures}))
        return int(bool(errors or failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
