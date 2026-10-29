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
    """True when the listener saw something after the source's last check and it has been quiet since,
    or a requested later wake-up (`wakes`, e.g. the Todoist Inbox settle time) passed since that check."""
    state = read_state(home, name)
    nudge_at = float(state.get("nudge_at") or 0)
    if nudge_at > last_poll and now - nudge_at >= quiet:
        return True
    return any(last_poll < float(at) <= now for at in state.get("wakes", []))


KEEP_HINTS_SECONDS = 86400 * 3


class LocalSink:
    """Events land on this machine: record them for the source's `due` and run a tick after `quiet`.

    `hint` (Slack: {channel, thread_ts, why, ts}) is kept so the source finds the conversation even before
    search indexes it; `wake_after` asks for one more check that many seconds later.
    """

    def __init__(self, home, quiet=None, runner=None, clock=time.time):
        self.home = Path(home)
        self.quiet = quiet or {}
        self.clock = clock
        self.ticks = TickPump(runner or (lambda: run_tick(self.home)), clock)

    def emit(self, source, hint=None, wake_after=None):
        now = self.clock()

        def change(state):
            state.update(nudge_at=now, heartbeat=now, last_event_at=now,
                         events_since_start=int(state.get("events_since_start") or 0) + 1)
            if hint and hint.get("channel"):
                hints = {k: v for k, v in state.get("hints", {}).items()
                         if now - float(v.get("seen", 0)) < KEEP_HINTS_SECONDS}
                key = f"{hint['channel']}:{hint.get('thread_ts') or ''}"
                old = hints.get(key, {})
                why = "thread" if "thread" in (old.get("why"), hint.get("why")) else hint.get("why")
                hints[key] = {**hint, "why": why, "seen": now}
                state["hints"] = hints
            if wake_after:
                state["wakes"] = [at for at in state.get("wakes", []) if now - float(at) < 86400] \
                    + [now + float(wake_after)]

        update_state(self.home, source, change)
        self.ticks.schedule(now + float(self.quiet.get(source, 10)))
        if wake_after:
            self.ticks.schedule(now + float(wake_after))

    def beat(self, sources, connected):
        """Liveness is the listener's own state file here; nothing to forward."""

    def pump(self):
        self.ticks.pump()


class RelaySink:
    """Events go to the relay queue on another machine (`hermes-auto-relay serve`); nothing runs here."""

    def __init__(self, url, token, session=None):
        self.url, self.token = url.rstrip("/"), token
        self.session = session

    def emit(self, source, hint=None, wake_after=None):
        import json as _json
        import urllib.request
        body = _json.dumps({"source": source, "hint": hint}).encode()
        request = urllib.request.Request(self.url + "/publish", data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    if response.status == 200:
                        return
            except OSError as exc:
                log(f"relay publish failed ({type(exc).__name__}), retrying")
            time.sleep(2 * (attempt + 1))
        raise RuntimeError("Relay did not accept the event")

    def beat(self, sources, connected):
        """Tell the relay the listener for `sources` is alive; the Mac checks a source itself when its
        heartbeats stop. Best effort: a relay from before heartbeats answers 404, which is fine."""
        import urllib.error
        import urllib.request
        body = json.dumps({"sources": list(sources), "connected": bool(connected)}).encode()
        request = urllib.request.Request(self.url + "/heartbeat", data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                log(f"relay heartbeat refused ({exc.code})")
        except OSError as exc:
            log(f"relay heartbeat failed ({type(exc).__name__})")

    def pump(self):
        pass


def read_secret(command):
    result = subprocess.run([os.path.expanduser(part) for part in command], capture_output=True, text=True,
                            timeout=30)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"No secret from {command[0]}")
    return result.stdout.strip()


def sink_from_config(home, config, quiet=None):
    """RelaySink when `relay.publish_url` is set (a listener on the always-on server), else LocalSink."""
    relay = config.get("relay", {})
    if relay.get("publish_url"):
        return RelaySink(relay["publish_url"], read_secret(relay["token_command"]))
    return LocalSink(home, quiet)


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
    """Runs a tick at every requested time: each event's quiet period end, a source's later wake-up.
    Times that passed together run one tick. A busy run lock retries in 30 seconds, a tick that crashed
    or timed out in `RETRY_SECONDS`.

    Every time is kept, not only the latest: one later wake-up (a Todoist settle time, `again_after`)
    must not swallow the earlier end of another source's quiet period."""

    RETRY_SECONDS = 60

    def __init__(self, runner, clock=time.time, logger=None):
        self.runner, self.clock, self.logger = runner, clock, logger or log
        self.lock = threading.Lock()
        self.times = set()

    @property
    def due_at(self):
        """The next tick (None: nothing waits)."""
        with self.lock:
            return min(self.times) if self.times else None

    def schedule(self, at):
        with self.lock:
            self.times.add(float(at))

    def pump(self):
        with self.lock:
            now = self.clock()
            passed = {at for at in self.times if at <= now}
            if not passed:
                return
            self.times -= passed
        try:
            code, error = self.runner()
        except Exception as exc:  # noqa: BLE001 - the pumping thread must outlive a hung or failed tick
            self.schedule(self.clock() + self.RETRY_SECONDS)
            self.logger(f"tick failed: {type(exc).__name__}: {str(exc)[:160]}")
            return
        if code and "Another hermes-auto operation" in (error or ""):
            self.schedule(self.clock() + 30)
        self.logger(f"tick exit={code}")


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def keep_pumping(stop, step, seconds=1, logger=None):
    """Call `step` every `seconds` until `stop` is set. A failing step is logged, never fatal: this
    thread runs the ticks and heartbeats of a listener whose main loop would not notice it died."""
    while not stop.is_set():
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            (logger or log)(f"pump error: {type(exc).__name__}: {str(exc)[:160]}")
        stop.wait(seconds)


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
