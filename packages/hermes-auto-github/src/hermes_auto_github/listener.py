"""GitHub notifications listener: the nearest thing to push GitHub offers for a personal account.

Account notifications have no webhook; GitHub's own clients poll `GET /notifications` with
`If-Modified-Since` at the `X-Poll-Interval` it returns (60 s). A 304 answer is free (no rate limit).
On a change this records only the time (`github-listener.json`) and runs `hermes-auto tick` after
`listen_quiet_seconds`; the GitHub source's `due` makes that a GitHub-only check. Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from hermes_auto import listening
from hermes_auto.listening import TickPump, log

from . import API, Source, gh_token

NAME = "github"


class GitHubListener:
    def __init__(self, home, settings, session=None, clock=time.time, runner=None):
        self.home = Path(home)
        self.settings = {**Source.defaults(), **settings}
        if session is None:
            import requests
            session = requests.Session()
            session.headers.update({"Authorization": f"Bearer {gh_token(self.settings['token_command'])}",
                                    "Accept": "application/vnd.github+json"})
        self.session = session
        self.clock = clock
        self.ticks = TickPump(runner or (lambda: listening.run_tick(self.home)), clock)
        self.modified = None
        self.events, self.last_event = 0, None
        self.seen = None  # newest updated_at seen; the first answer is the baseline

    def check(self):
        """One conditional request; returns the poll interval GitHub asks for."""
        headers = {"If-Modified-Since": self.modified} if self.modified else {}
        response = self.session.get(API + "/notifications", params={"per_page": 50}, headers=headers, timeout=45)
        interval = int(response.headers.get("X-Poll-Interval") or 60)
        if response.status_code == 304:
            self.beat(True)
            return interval
        if not response.ok:
            raise RuntimeError(f"GitHub notifications failed ({response.status_code})")
        self.modified = response.headers.get("Last-Modified") or self.modified
        skip = set(self.settings.get("skip_reasons", []))
        newest = max([item["updated_at"] for item in response.json() if item.get("reason") not in skip],
                     default=self.seen or "")
        if self.seen is not None and newest > self.seen:
            self.arrived()
        self.seen = max(newest, self.seen or "")
        self.beat(True)
        return interval

    def arrived(self):
        now = self.clock()
        self.events += 1
        self.last_event = now
        listening.update_state(self.home, NAME, lambda state: state.update(
            nudge_at=now, heartbeat=now, events_since_start=self.events, last_event_at=now))
        self.ticks.schedule(now + float(self.settings["listen_quiet_seconds"]))

    def beat(self, connected):
        now = self.clock()
        # A working poller counts as live: GitHub answers every minute, changes or not.
        listening.update_state(self.home, NAME, lambda state: state.update(
            heartbeat=now, connected=connected, events_since_start=self.events,
            last_event_at=self.last_event or now))


def serve(home, settings):
    listener = GitHubListener(home, settings)
    stop = threading.Event()

    def pumping():
        while not stop.is_set():
            listener.ticks.pump()
            stop.wait(1)

    threading.Thread(target=pumping, daemon=True).start()
    log("polling notifications")
    backoff = 30
    while True:
        try:
            interval = listener.check()
            backoff = 30
        except Exception as exc:  # noqa: BLE001 - keep the service alive; regular checks cover the gap
            log(f"error: {type(exc).__name__}: {str(exc)[:200]}")
            listener.beat(False)
            interval = backoff
            backoff = min(backoff * 2, 600)
        time.sleep(max(interval, 30))


def main(argv=None):
    parser = argparse.ArgumentParser(description="GitHub notifications listener for Hermes Auto")
    parser.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-auto")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "install-launchd", "check"])
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    settings = json.loads((home / "config.json").read_text()).get("sources", {}).get(NAME, {})
    if args.command == "install-launchd":
        print(listening.install_launchd(home, NAME, "hermes-auto-github-listen"))
        return 0
    if args.command == "check":
        listener = GitHubListener(home, settings)
        print(json.dumps({"github": "ok", "poll_interval": listener.check()}))
        return 0
    serve(home, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
