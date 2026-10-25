"""Shared pieces of the push listeners (Slack Socket Mode, Proton IMAP IDLE, ...).

A listener is a long-lived user service next to the scheduler. It never touches the automation database
(the run lock belongs to `hermes-auto tick`); it records what it saw in `<name>-listener.json` (ids and
times only, no message text) and runs `hermes-auto tick` once things went quiet. The adapter's `due`
reads that file and turns the tick into a check of that source only.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_locks: dict[str, threading.Lock] = {}


def state_path(home, name):
    return Path(home) / f"{name}-listener.json"


def read_state(home, name):
    try:
        return json.loads(state_path(home, name).read_text())
    except (OSError, ValueError):
        return {}


def write_state(home, name, data):
    path = state_path(home, name)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{name}-listener-")
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def update_state(home, name, change):
    """Read, change (a function that edits the dict in place) and write the state under one lock."""
    lock = _locks.setdefault(f"{home}:{name}", threading.Lock())
    with lock:
        state = read_state(home, name)
        change(state)
        write_state(home, name, state)
        return state


def nudged(home, name, last_poll, now, quiet):
    """True when the listener saw something after the source's last check and it has been quiet since."""
    nudge_at = float(read_state(home, name).get("nudge_at") or 0)
    return nudge_at > last_poll and now - nudge_at >= quiet


def live(state, now, stale=300):
    """Connected, heartbeat fresh, and at least one event received (so subscriptions demonstrably work)."""
    return bool(state.get("connected") and now - float(state.get("heartbeat") or 0) < stale
                and state.get("last_event_at"))


def run_tick(home):
    entry = Path(sys.executable).parent / "hermes-auto"
    command = [str(entry) if entry.exists() else "hermes-auto", "--home", str(home), "tick"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    return result.returncode, result.stderr[-300:]


class TickPump:
    """Runs a tick once the latest event's quiet period ends; a busy run lock retries in 30 seconds."""

    def __init__(self, runner, clock=time.time, logger=None):
        self.runner, self.clock, self.logger = runner, clock, logger or log
        self.lock = threading.Lock()
        self.due_at = None

    def schedule(self, at):
        with self.lock:
            self.due_at = max(self.due_at or 0, at)

    def pump(self):
        with self.lock:
            due = self.due_at
            if due is None or self.clock() < due:
                return
            self.due_at = None
        code, error = self.runner()
        if code and "Another hermes-auto operation" in (error or ""):
            self.schedule(self.clock() + 30)
        self.logger(f"tick exit={code}")


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def install_launchd(home, name, entry_point):
    """User service `io.github.hermes-auto.<name>-listener`, kept alive, logging to `<name>-listener.log`."""
    entry = Path(sys.executable).parent / entry_point
    label = f"io.github.hermes-auto.{name}-listener"
    agent = Path.home() / f"Library/LaunchAgents/{label}.plist"
    payload = {
        "Label": label, "ProgramArguments": [str(entry), "--home", str(home)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())},
        "StandardOutPath": str(Path(home) / f"{name}-listener.log"),
        "StandardErrorPath": str(Path(home) / f"{name}-listener.log"),
    }
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(plistlib.dumps(payload))
    os.chmod(agent, 0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(agent)], check=True)
    return agent
