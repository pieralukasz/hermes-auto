"""Proton Bridge IMAP IDLE listener: nudges the sorter the moment new mail lands in INBOX.

Runs as its own user service (`hermes-auto-proton-listen`). It logs in to the local Bridge, selects INBOX
read-only and waits in IDLE. On `* N EXISTS` it records only the time (`proton-listener.json`) and runs
`hermes-auto tick` after `listen_quiet_seconds`; the Proton source's `due` then makes that a check of
Proton only. It reads no message, changes no flags and sends nothing. Missed events (sleep, Bridge
restarts) are covered by the regular checks.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

from hermes_auto import listening
from hermes_auto.listening import TickPump, log

from . import Proton, Source

NAME = "proton"
# RFC 2177: re-issue IDLE before the server's 30-minute inactivity timeout.
IDLE_SECONDS = 25 * 60
EXISTS = re.compile(rb"^\* \d+ EXISTS", re.I)


class RawLines:
    """Line reader on the TLS socket itself, with a timeout per read, outside imaplib's buffered file
    (a timed-out read would make that file unusable)."""

    def __init__(self, sock):
        self.sock, self.buffer = sock, b""

    def send(self, data):
        self.sock.sendall(data)

    def readline(self, timeout):
        """One line (with CRLF), or None when nothing arrived within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while b"\r\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                raise ConnectionError("Bridge closed the connection")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\r\n", 1)
        return line + b"\r\n"


class ProtonListener:
    def __init__(self, home, settings, clock=time.time, runner=None):
        self.home = Path(home)
        self.settings = {**Source.defaults(), **settings}
        self.clock = clock
        self.ticks = TickPump(runner or (lambda: listening.run_tick(self.home)), clock)
        self.events, self.last_event = 0, None
        self.counter = 0

    def state(self, change):
        listening.update_state(self.home, NAME, change)

    def beat(self, connected=None):
        def change(state):
            state["heartbeat"] = self.clock()
            state["events_since_start"], state["last_event_at"] = self.events, self.last_event
            if connected is not None:
                state["connected"] = connected

        self.state(change)

    def arrived(self):
        now = self.clock()
        self.events += 1
        self.last_event = now

        def change(state):
            state.update(nudge_at=now, heartbeat=now, events_since_start=self.events, last_event_at=now)

        self.state(change)
        self.ticks.schedule(now + float(self.settings["listen_quiet_seconds"]))

    def tag(self):
        self.counter += 1
        return f"HAI{self.counter}".encode()

    def idle_round(self, lines, stop=None, wait=60):
        """One IDLE command: wait for new mail until IDLE_SECONDS, then DONE and its tagged reply."""
        tag = self.tag()
        lines.send(tag + b" IDLE\r\n")
        while True:
            line = lines.readline(30)
            if line is None:
                raise ConnectionError("Bridge did not accept IDLE")
            if line.startswith(b"+"):
                break
            if line.startswith(tag):
                raise ConnectionError("Bridge refused IDLE")
        started = time.monotonic()
        while time.monotonic() - started < IDLE_SECONDS and not (stop and stop.is_set()):
            line = lines.readline(wait)
            if line is None:
                self.beat()
                continue
            if EXISTS.match(line):
                self.arrived()
            elif line.upper().startswith(b"* BYE"):
                raise ConnectionError("Bridge ended the session")
        lines.send(b"DONE\r\n")
        while True:
            line = lines.readline(30)
            if line is None:
                raise ConnectionError("No reply to DONE")
            if EXISTS.match(line):
                self.arrived()
            if line.startswith(tag):
                if b" OK" not in line.upper():
                    raise ConnectionError("IDLE ended with an error")
                return

    def connect(self):
        client = Proton(self.settings)
        client.select("INBOX")
        return client, RawLines(client.client.sock)


def serve(home, settings):
    listener = ProtonListener(home, settings)
    stop = threading.Event()

    def pumping():
        while not stop.is_set():
            listener.ticks.pump()
            stop.wait(1)

    threading.Thread(target=pumping, daemon=True).start()
    backoff = 5
    while True:
        client = None
        try:
            client, lines = listener.connect()
            log("idle")
            listener.beat(connected=True)
            backoff = 5
            while True:
                listener.idle_round(lines)
        except Exception as exc:  # noqa: BLE001 - keep the service alive; regular checks cover the gap
            log(f"error: {type(exc).__name__}: {str(exc)[:200]}")
            backoff = min(backoff * 2, 300)
        finally:
            if client is not None:
                try:
                    client.client.shutdown()
                except Exception:  # noqa: BLE001
                    pass
        listener.beat(connected=False)
        time.sleep(backoff)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Proton Bridge IMAP IDLE listener for Hermes Auto")
    parser.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-auto")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "install-launchd", "check"])
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    settings = json.loads((home / "config.json").read_text()).get("sources", {}).get(NAME, {})
    if args.command == "install-launchd":
        print(listening.install_launchd(home, NAME, "hermes-auto-proton-listen"))
        return 0
    if args.command == "check":
        listener = ProtonListener(home, settings)
        client, _ = listener.connect()
        print(json.dumps({"bridge": "ok", "idle": b"IDLE" in b" ".join(
            c.encode() if isinstance(c, str) else c for c in client.client.capabilities)}))
        client.close()
        return 0
    serve(home, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
