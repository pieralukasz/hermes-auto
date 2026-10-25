"""Slack Socket Mode listener: nudges the sorter the moment something addressed to the user arrives.

Runs as its own long-lived user service. It never reads or stores message text beyond what it needs
to decide relevance in memory, never posts, and never touches the automation database (the run lock
belongs to `hermes-auto tick`). It writes `slack-listener.json` in the automation home:

- `heartbeat`: last sign of a live connection (the Slack source relaxes its polling while it is fresh),
- `hints`: conversations with new activity ({channel, thread_ts, why, ts}; ids only),
- `nudge_at`: when the latest relevant message arrived.

When a conversation has been quiet for `quiet_seconds` it runs `hermes-auto tick`, which then performs
a Slack-only check (the source's `due`). Missed events (sleep, restarts) are covered by that polling.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import Http, Source, read_token

STATE = "slack-listener.json"
KEEP_HINTS_SECONDS = 86400 * 3


def state_path(home):
    return Path(home) / STATE


def read_state(home):
    try:
        return json.loads(state_path(home).read_text())
    except (OSError, ValueError):
        return {}


def write_state(home, data):
    path = state_path(home)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".slack-listener-")
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def app_token(settings):
    command = settings.get("app_token_command") or Source.defaults()["app_token_command"]
    result = subprocess.run(list(command), capture_output=True, text=True, timeout=30)
    token = result.stdout.strip()
    if result.returncode or not token.startswith("xapp-"):
        raise RuntimeError("No Slack app-level token (xapp-) from app_token_command")
    return token


class Listener:
    def __init__(self, home, settings, http=None, clock=time.time, runner=None):
        self.home = Path(home)
        self.settings = {**Source.defaults(), **settings}
        self.http = http or Http(read_token(self.settings["token_file"]))
        self.clock = clock
        self.runner = runner or self.run_tick
        self.me = self.http.api("auth.test")["user_id"]
        self.followed = {}  # (channel, thread_ts) -> (followed, checked_at)
        self.lock = threading.Lock()
        self.due_at = None
        # Health without content: how many events arrived since start, and when the last one did.
        self.events, self.last_event = 0, None

    # ── Relevance ───────────────────────────────────────────────────────

    def thread_followed(self, channel, thread_ts):
        key = (channel, thread_ts)
        cached = self.followed.get(key)
        if cached and self.clock() - cached[1] < 600:
            return cached[0]
        try:
            parent = self.http.api("conversations.replies", channel=channel, ts=thread_ts, limit=1)["messages"][0]
            followed = bool(parent.get("subscribed") or parent.get("user") == self.me)
        except Exception:  # noqa: BLE001 - unknown: let the sorter decide
            followed = True
        self.followed[key] = (followed, self.clock())
        return followed

    def classify(self, event):
        """Why this message concerns the user ("dm", "mention", "thread"), or None."""
        if event.get("type") != "message" or event.get("bot_id"):
            return None
        if event.get("subtype") not in (None, "thread_broadcast", "file_share", "me_message"):
            return None
        user = event.get("user")
        channel = event.get("channel") or ""
        thread_ts = event.get("thread_ts")
        if user == self.me and thread_ts:
            # He wrote in a thread: Slack follows it for him from now on.
            self.followed[(channel, thread_ts)] = (True, self.clock())
            return None
        if not user or user in ("USLACKBOT", self.me) or channel in self.settings.get("skip_channels", []):
            return None
        if event.get("channel_type") in ("im", "mpim"):
            return "dm"
        if f"<@{self.me}>" in (event.get("text") or ""):
            return "mention"
        if thread_ts and thread_ts != event.get("ts") and self.thread_followed(channel, thread_ts):
            return "thread"
        return None

    # ── Events ──────────────────────────────────────────────────────────

    def handle(self, envelope):
        """Process one Socket Mode envelope; returns the ack to send (or None)."""
        kind = envelope.get("type")
        if kind == "hello":
            self.beat(connected=True)
            return None
        ack = {"envelope_id": envelope["envelope_id"]} if envelope.get("envelope_id") else None
        if kind != "events_api":
            return ack
        self.events += 1
        self.last_event = self.clock()
        event = envelope.get("payload", {}).get("event", {})
        why = self.classify(event)
        if why:
            channel = event["channel"]
            thread_ts = event.get("thread_ts") if event.get("thread_ts") != event.get("ts") else None
            if why == "dm" and event.get("thread_ts"):
                thread_ts = event["thread_ts"]
            self.hint(channel, thread_ts, why, event.get("ts"))
        return ack

    def hint(self, channel, thread_ts, why, ts):
        now = self.clock()
        with self.lock:
            state = read_state(self.home)
            hints = {k: v for k, v in state.get("hints", {}).items() if now - v.get("seen", 0) < KEEP_HINTS_SECONDS}
            key = f"{channel}:{thread_ts or ''}"
            old = hints.get(key, {})
            # A followed thread outranks a single mention in it (every reply then counts).
            hints[key] = {"channel": channel, "thread_ts": thread_ts, "seen": now, "ts": ts,
                          "why": "thread" if "thread" in (old.get("why"), why) else why}
            state.update(hints=hints, nudge_at=now, heartbeat=now)
            write_state(self.home, state)
            self.due_at = now + float(self.settings["quiet_seconds"]) + 5

    def beat(self, connected=None):
        with self.lock:
            state = read_state(self.home)
            state["heartbeat"] = self.clock()
            state["events_since_start"], state["last_event_at"] = self.events, self.last_event
            if connected is not None:
                state["connected"] = connected
            write_state(self.home, state)

    # ── Ticks ───────────────────────────────────────────────────────────

    def run_tick(self):
        entry = Path(sys.executable).parent / "hermes-auto"
        command = [str(entry) if entry.exists() else "hermes-auto", "--home", str(self.home), "tick"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        return result.returncode, result.stderr[-300:]

    def pump(self):
        """Run a tick once the latest conversation went quiet; a busy run lock retries in 30 s."""
        with self.lock:
            due = self.due_at
        if due is None or self.clock() < due:
            return
        with self.lock:
            self.due_at = None
        code, error = self.runner()
        if code and "Another hermes-auto operation" in (error or ""):
            with self.lock:
                self.due_at = self.due_at or self.clock() + 30
        log(f"tick exit={code}")


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def serve(home, settings):
    import websocket

    listener = Listener(home, settings)
    token = app_token(listener.settings)
    stop = threading.Event()

    def pumping():
        last_beat = 0.0
        while not stop.is_set():
            listener.pump()
            if time.time() - last_beat > 60:
                if read_state(home).get("connected"):
                    listener.beat()
                last_beat = time.time()
            stop.wait(1)

    threading.Thread(target=pumping, daemon=True).start()
    backoff = 5
    while True:
        try:
            opened = Http(token).api("apps.connections.open", _post=True)
            url = opened["url"]
            log("connecting")

            def on_message(socket, raw):
                envelope = json.loads(raw)
                ack = listener.handle(envelope)
                if ack:
                    socket.send(json.dumps(ack))
                if envelope.get("type") == "disconnect":
                    socket.close()

            app = websocket.WebSocketApp(url, on_message=on_message)
            app.run_forever(ping_interval=30, ping_timeout=10)
            log("disconnected")
            backoff = 5
        except Exception as exc:  # noqa: BLE001 - keep the service alive; polling covers the gap
            log(f"error: {type(exc).__name__}: {str(exc)[:200]}")
            backoff = min(backoff * 2, 300)
        listener.beat(connected=False)
        time.sleep(backoff)


def install_launchd(home):
    import plistlib
    entry = Path(sys.executable).parent / "hermes-auto-slack-listen"
    agent = Path.home() / "Library/LaunchAgents/io.github.hermes-auto.slack-listener.plist"
    payload = {
        "Label": "io.github.hermes-auto.slack-listener",
        "ProgramArguments": [str(entry), "--home", str(home)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())},
        "StandardOutPath": str(Path(home) / "slack-listener.log"),
        "StandardErrorPath": str(Path(home) / "slack-listener.log"),
    }
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(plistlib.dumps(payload))
    os.chmod(agent, 0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/io.github.hermes-auto.slack-listener"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(agent)], check=True)
    print(agent)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Slack Socket Mode listener for Hermes Auto")
    parser.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-auto")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "install-launchd", "check"])
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    config = json.loads((home / "config.json").read_text())
    settings = config.get("sources", {}).get("slack", {})
    if args.command == "install-launchd":
        install_launchd(home)
        return 0
    if args.command == "check":
        token = app_token({**Source.defaults(), **settings})
        print(json.dumps({"app_token": "ok", "socket": bool(Http(token).api("apps.connections.open",
                                                                         _post=True).get("url"))}))
        return 0
    serve(home, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
