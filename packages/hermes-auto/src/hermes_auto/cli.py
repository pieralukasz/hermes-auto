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


def poll(config, store):
    installed = sources()
    errors = []
    for name, settings in config["sources"].items():
        if not settings.get("enabled"):
            continue
        try:
            if name not in installed:
                raise RuntimeError(f"Install hermes-auto-{name}")
            if settings.get("mode", config["default_mode"]) not in ("open", "draft", "research", "agent"):
                raise ValueError("Unknown source mode")
            installed[name]().poll(config, store)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    store.set_meta("source_errors", errors)
    return errors


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
        "RunAtLoad": True, "StartInterval": 60,
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
    commands.add_parser("status", help="Show jobs, source errors and pause state")
    commands.add_parser("pause", help="Pause discovery and preparation")
    commands.add_parser("resume", help="Resume discovery and preparation")
    commands.add_parser("tick", help="Scheduled run, respecting desktop presence and polling interval")
    commands.add_parser("run", help="Discover events and prepare pending jobs now")
    scan = commands.add_parser("scan", help="Discover events without starting Hermes or spending LLM tokens")
    scan.add_argument("--dry-run", action="store_true", help="Use a temporary state copy; do not advance baselines")
    source = commands.add_parser("source", help="Enable or disable an installed adapter")
    source.add_argument("action", choices=["list", "enable", "disable", "setup"])
    source.add_argument("name", nargs="?")
    for name in ("retry", "new-session"):
        item = commands.add_parser(name, help="Explicitly continue a failed job" if name == "retry" else
                                   "Explicitly create a fresh conversation for an existing event")
        item.add_argument("job_id", type=int)
    commands.add_parser("install-launchd", help="Install a macOS user service")
    commands.add_parser("backup", help="Write a consistent backup of automation state")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    home = args.home.expanduser().resolve()
    try:
        if args.command == "status":
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
    elif command == "doctor":
        print(json.dumps(Hermes(config, home).invoke("doctor"), indent=2))
        print("Installed adapters:", ", ".join(sorted(sources())) or "none")
    elif command == "status":
        jobs = [{key: job[key] for key in ("id", "source", "title", "mode", "status", "session_id", "error")}
                for job in store.rows()]
        print(json.dumps({"paused": store.get_meta("paused", False),
                          "source_errors": store.get_meta("source_errors", []), "jobs": jobs}, indent=2,
                         ensure_ascii=False))
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
            if not Hermes(config, home).exists(job):
                store.update(job["id"], status="deleted", error="Session is missing; use new-session explicitly")
                raise ValueError("The session was deleted; it will not be recreated by retry")
            store.update(job["id"], status="retry_pending", error="")
        print("Queued. Run 'hermes-auto run' or wait for the next scheduled check.")
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
        if command == "tick" and not eligible_tick(config, store):
            return 0
        errors = poll(config, store)
        failures = 0
        blocked = [error.split(":", 1)[0] for error in errors]
        hermes = None

        def bridge():
            nonlocal hermes
            hermes = hermes or Hermes(config, home)
            return hermes

        if command != "scan" and any(j["status"] in ("pending", "created", "retry_pending", "running", "creating")
                                     for j in store.rows()):
            failures = process_jobs(store, config, bridge(), blocked_sources=blocked)
        if command != "scan":
            # Archiving a prepared session in Hermes closes its source item (Todoist: completes the task).
            finish_errors = sync_finished(store, config, bridge, sources(), blocked)
            failures += len(finish_errors)
            errors = errors + finish_errors
        store.set_meta("last_poll", time.time())
        store.backup()
        counts = {}
        for job in store.rows():
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        print(json.dumps({"jobs": counts, "source_errors": errors, "failed_this_run": failures}))
        return int(bool(errors or failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
