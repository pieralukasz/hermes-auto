"""Matrix listener: push for the bridged chats. Runs next to the homeserver (ZimaBlade) and publishes to the relay.

It long-polls `/sync` (GET only, presence offline, its own position) and, when a message from someone else arrives
in a joined room, publishes one ids-free event `matrix` (`relay.publish_url`) or, without a relay, records it in
`matrix-listener.json` and runs `hermes-auto tick` after `quiet_seconds`. It never reads or forwards message text
beyond deciding "someone else wrote": the Mac's Matrix source does the reading, from its own `/sync` cursor.

After a (re)start the first answer is a baseline, followed by one event, so whatever came while the listener was
down is checked at once. A refused publish keeps the old position: the same messages trigger it again.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from hermes_auto import listening
from hermes_auto.listening import LocalSink, log, sink_from_config

from . import API, BASELINE_FILTER, MESSAGE_TYPES, PRESENCE, SYNC_FILTER, Matrix, MatrixError, Source

NAME = "matrix"
# Enough to see whether anyone else wrote; a `limited` timeline counts as "maybe" and publishes.
LISTEN_FILTER = {**SYNC_FILTER, "room": {**SYNC_FILTER["room"], "timeline": {"limit": 10, "types": list(MESSAGE_TYPES)}}}
BEAT_SECONDS = 60


class MatrixListener:
    def __init__(self, home, settings, http=None, sink=None, clock=time.time, runner=None):
        self.home = Path(home)
        self.settings = {**Source.defaults(), **settings}
        self.clock = clock
        self.matrix = Matrix(self.settings, http=http, now=clock)
        self.sink = sink or LocalSink(self.home, {NAME: float(self.settings["quiet_seconds"])}, runner, clock)
        self.since = None
        self.events, self.last_event, self.beat_at = 0, None, 0.0

    def wait_ms(self):
        return int(max(0.0, min(float(self.settings["listen_timeout_seconds"]), 50.0)) * 1000)

    def check(self):
        """One /sync. Returns True when it published (someone else wrote, or the baseline after a start)."""
        skip = set(self.settings.get("skip_rooms") or [])
        if self.since is None:
            data = self.matrix.http.get(f"{API}/sync", {"filter": json.dumps(BASELINE_FILTER), "timeout": 0,
                                                       **PRESENCE})
            self.arrived()  # raises when the sink refused it: no position yet, the next check is a baseline again
            self.since = data["next_batch"]
            self.beat(True)
            return True
        data = self.matrix.http.get(f"{API}/sync", {"filter": json.dumps(LISTEN_FILTER), "since": self.since,
                                                   "timeout": self.wait_ms(), **PRESENCE})
        now = self.clock()
        new = False
        for room_id, room in ((data.get("rooms") or {}).get("join") or {}).items():
            if room_id in skip:
                continue
            timeline = room.get("timeline") or {}
            if timeline.get("limited") or any(self.matrix.relevant(event, now) for event in timeline.get("events", [])):
                new = True
                break
        if new:
            self.arrived()  # raises when refused: `since` stays, so these messages publish again
        self.since = data.get("next_batch") or self.since
        self.beat(True)
        return new

    def arrived(self):
        self.events += 1
        self.last_event = self.clock()
        self.sink.emit(NAME)

    def beat(self, connected, force=False):
        now = self.clock()
        if not force and connected and now - self.beat_at < BEAT_SECONDS:
            return
        self.beat_at = now
        listening.update_state(self.home, NAME, lambda state: state.update(
            heartbeat=now, connected=connected, events_since_start=self.events,
            last_event_at=self.last_event or now))
        beat = getattr(self.sink, "beat", None)
        if beat:
            beat([NAME], connected)


def serve(home, settings, config=None):
    full = {**Source.defaults(), **settings}
    listener = MatrixListener(home, settings, sink=sink_from_config(
        home, config or {}, {NAME: float(full["quiet_seconds"])}))
    stop = threading.Event()
    threading.Thread(target=listening.keep_pumping, args=(stop, listener.sink.pump), daemon=True).start()
    log(f"listening to the homeserver as {listener.matrix.me()}")
    backoff = 5
    while True:
        try:
            listener.check()
            backoff = 5
        except Exception as exc:  # noqa: BLE001 - keep the service alive; the Mac's polling covers the gap
            log(f"error: {type(exc).__name__}: {str(exc)[:200]}")
            listener.beat(False, force=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Matrix listener for Hermes Auto (push for bridged chats)")
    parser.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-auto")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "check"])
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    config = json.loads((home / "config.json").read_text())
    settings = config.get("sources", {}).get(NAME, {})
    try:
        if args.command == "check":
            matrix = Matrix({**Source.defaults(), **settings})
            print(json.dumps({"matrix": "ok", "user": matrix.me(),
                              "publish": "relay" if config.get("relay", {}).get("publish_url") else "local"}))
            return 0
        serve(home, settings, config)
    except MatrixError as exc:
        print(f"hermes-auto-matrix-listen: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
