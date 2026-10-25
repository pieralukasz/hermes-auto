"""Client of the hermes-hooks gateway (Cloudflare Worker): webhook pushes for sources that need a
public address (Gmail Pub/Sub; Meta later).

The Mac connects out to `wss://<gateway>/listen` with a bearer secret and replays missed events by
sequence number. Each event names its source; this records a nudge for that source's listener state
(`<source>-listener.json`, times only) and runs `hermes-auto tick` after a short quiet period, so the
source's `due` turns it into a check of that source only. Events carry ids, never message content.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import listening
from .listening import LocalSink, log, sink_from_config

NAME = "hooks"
DEFAULTS = {"url": "wss://hooks.lucaspiera.com/listen",
            "secret_command": ["security", "find-generic-password", "-s", "hermes-auto-hooks-listen", "-w"],
            # Seconds to wait after an event before checking that source.
            "quiet_seconds": {"gmail": 10}}


def secret(settings):
    result = subprocess.run(list(settings["secret_command"]), capture_output=True, text=True, timeout=30)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("No gateway secret from hooks.secret_command")
    return result.stdout.strip()


class HooksClient:
    def __init__(self, home, settings, clock=time.time, runner=None, sink=None):
        self.home = Path(home)
        self.settings = {**DEFAULTS, **settings}
        self.clock = clock
        self.sink = sink or LocalSink(self.home, self.settings["quiet_seconds"], runner, clock)

    def since(self):
        return int(listening.read_state(self.home, NAME).get("seq") or 0)

    def handle(self, raw):
        """One gateway message; returns the source it nudged, if any."""
        if raw == "pong":
            return None
        message = json.loads(raw)
        now = self.clock()
        if message.get("type") == "hello":
            listening.update_state(self.home, NAME, lambda state: state.update(connected=True, heartbeat=now))
            return None
        source, seq = message.get("source"), int(message.get("seq") or 0)
        if seq <= self.since():
            return None  # already handled (replay overlap)
        listening.update_state(self.home, NAME, lambda state: state.update(seq=seq, heartbeat=now,
                                                                            last_event_at=now))
        if not source:
            return None
        self.sink.emit(source)
        return source

    def beat(self, connected):
        now = self.clock()
        listening.update_state(self.home, NAME, lambda state: state.update(connected=connected, heartbeat=now))


def serve(home, settings, config=None):
    import websocket

    client = HooksClient(home, settings, sink=sink_from_config(home, config or {},
                                                               {**DEFAULTS, **settings}["quiet_seconds"]))
    token = secret(client.settings)
    stop = threading.Event()

    def pumping():
        last = 0.0
        while not stop.is_set():
            client.sink.pump()
            if time.time() - last > 60 and listening.read_state(client.home, NAME).get("connected"):
                client.beat(True)
                last = time.time()
            stop.wait(1)

    threading.Thread(target=pumping, daemon=True).start()
    backoff = 5
    while True:
        try:
            url = f"{client.settings['url']}?since={client.since()}"
            log("connecting")
            app = websocket.WebSocketApp(url, header=[f"Authorization: Bearer {token}"],
                                         on_message=lambda socket, raw: client.handle(raw))
            app.run_forever(ping_interval=30, ping_timeout=10)
            log("disconnected")
            backoff = 5
        except Exception as exc:  # noqa: BLE001 - keep the service alive; regular checks cover the gap
            log(f"error: {type(exc).__name__}: {str(exc)[:200]}")
            backoff = min(backoff * 2, 300)
        client.beat(False)
        time.sleep(backoff)


def main(argv=None):
    parser = argparse.ArgumentParser(description="hermes-hooks gateway client for Hermes Auto")
    parser.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-auto")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "install-launchd"])
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    config = json.loads((home / "config.json").read_text())
    settings = config.get("hooks", {})
    if args.command == "install-launchd":
        print(listening.install_launchd(home, NAME, "hermes-auto-hooks-listen"))
        return 0
    serve(home, settings, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
