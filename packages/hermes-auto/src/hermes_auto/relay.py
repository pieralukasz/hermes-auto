"""Relay queue on an always-on machine (ZimaBlade), and its client on the Mac.

`hermes-auto-relay serve` keeps a durable SQLite queue of ids-only events published by the listeners
on that machine (Slack, GitHub, the Cloudflare webhook gateway). It listens on a private address
(Tailscale) and needs a bearer token for both publishing and reading.

`hermes-auto-relay client` runs on the Mac. It long-polls `/events?since=<seq>`, so whatever arrived
while the Mac slept comes in order once it wakes, and hands each event to the local sink (the source's
`<name>-listener.json` and a tick). If the relay stays unreachable, it nudges every relayed source every
`fallback_seconds`, so the Mac falls back to checking them itself.
"""
from __future__ import annotations

import argparse
import hmac
import json
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import listening
from .listening import LocalSink, log, read_secret

NAME = "relay"
KEEP_SECONDS = 7 * 86400
MAX_BATCH = 500


class Queue:
    """Durable, ordered, ids-only events, plus the last heartbeat of each listener's sources.

    `epoch` is random per database file: a client whose sequence number came from another file (the
    queue was recreated) sees a different epoch. `head` is the last number ever given out (it survives
    the expiry of every event), `first` the oldest event still kept.
    """

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "at REAL NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # alive_at: the last heartbeat that said connected, so a brief disconnect is not a dead listener.
        self.db.execute("CREATE TABLE IF NOT EXISTS listeners (source TEXT PRIMARY KEY, heartbeat REAL NOT NULL, "
                        "connected INTEGER NOT NULL, alive_at REAL)")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('epoch', ?)", (uuid.uuid4().hex,))
        self.epoch = self.db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def publish(self, source, data, now=None):
        now = time.time() if now is None else now
        with self.changed:
            cursor = self.db.execute("INSERT INTO events(at, source, data) VALUES (?,?,?)",
                                     (now, source, json.dumps(data)))
            self.db.execute("DELETE FROM events WHERE at < ?", (now - KEEP_SECONDS,))
            self.changed.notify_all()
            return cursor.lastrowid

    def since(self, seq, wait=0.0):
        deadline = time.time() + wait
        with self.changed:
            while True:
                rows = self.db.execute("SELECT seq, at, source, data FROM events WHERE seq > ? ORDER BY seq "
                                       "LIMIT ?", (int(seq), MAX_BATCH)).fetchall()
                left = deadline - time.time()
                if rows or left <= 0:
                    return [{"seq": s, "at": a, "source": src, **json.loads(d)} for s, a, src, d in rows]
                self.changed.wait(left)

    def last(self):
        row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return int(row[0]) if row else 0

    def page(self, seq, wait=0.0):
        """One answer to `/events`: the events after `seq` and the numbering they belong to."""
        events = self.since(seq, wait)
        with self.lock:
            head = self.last()
            first = self.db.execute("SELECT MIN(seq) FROM events").fetchone()[0]
        return {"events": events, "last": head, "head": head, "first": first or head + 1, "epoch": self.epoch}

    def beat(self, sources, connected, now=None):
        now = time.time() if now is None else now
        with self.lock:
            for source in sources:
                self.db.execute("INSERT INTO listeners VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                                "heartbeat=excluded.heartbeat, connected=excluded.connected, "
                                "alive_at=COALESCE(excluded.alive_at, alive_at)",
                                (str(source), now, int(connected), now if connected else None))

    def listeners(self):
        return {source: {"heartbeat": beat, "connected": bool(connected), "alive_at": alive_at or 0}
                for source, beat, connected, alive_at in
                self.db.execute("SELECT source, heartbeat, connected, alive_at FROM listeners")}


def make_handler(queue, token):
    class Handler(BaseHTTPRequestHandler):
        def authorized(self):
            return hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode())

        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/health":
                return self.reply(200, {"ok": True, "seq": queue.last()})
            if not self.authorized():
                return self.reply(403, {"error": "forbidden"})
            if url.path != "/events":
                return self.reply(404, {"error": "not found"})
            query = parse_qs(url.query)
            since = int((query.get("since") or ["0"])[0])
            wait = min(float((query.get("wait") or ["0"])[0]), 60.0)
            page = queue.page(since, wait)
            self.reply(200, {**page, "now": time.time(), "sources": queue.listeners()})

        def do_POST(self):
            if not self.authorized():
                return self.reply(403, {"error": "forbidden"})
            path = urlparse(self.path).path
            if path not in ("/publish", "/heartbeat"):
                return self.reply(404, {"error": "not found"})
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if path == "/heartbeat":
                    queue.beat([str(name) for name in payload["sources"]], bool(payload.get("connected")))
                    return self.reply(200, {"ok": True})
                source = str(payload["source"])
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "bad request"})
            hint = payload.get("hint") if isinstance(payload.get("hint"), dict) else None
            self.reply(200, {"seq": queue.publish(source, {"hint": hint} if hint else {})})

        def log_message(self, *args):
            pass

    return Handler


def serve(home, settings):
    queue = Queue(Path(home) / "relay-queue.sqlite3")
    token = read_secret(settings["token_command"])
    host, port = settings.get("host", "127.0.0.1"), int(settings.get("port", 8787))
    server = ThreadingHTTPServer((host, port), make_handler(queue, token))
    server.daemon_threads = True
    log(f"relay listening on {host}:{port}, seq {queue.last()}")
    server.serve_forever()


class Client:
    """Mac side: long-poll the relay, hand events to the local sink, remember the sequence number.

    Recovery is bounded: each relayed source gets at most one fallback check per `fallback_seconds`
    (times kept in `relay-listener.json`, so restarts do not repeat it), and only when its events may
    be lost: the relay is unreachable, its numbering was reset or expired past our position, or the
    listener feeding that source stopped reporting (`stale_seconds`, measured on the relay's clock).
    A healthy, idle relay costs no source checks.
    """

    def __init__(self, home, settings, fetch=None, sink=None, clock=time.time):
        self.home = Path(home)
        self.settings = {"fallback_seconds": 1800, "stale_seconds": 600, "again_after": {}, "quiet_seconds": {},
                         **settings}
        self.fetch = fetch
        self.sink = sink or LocalSink(self.home, self.settings["quiet_seconds"])
        self.clock = clock
        self.down_since = None

    def state(self):
        return listening.read_state(self.home, NAME)

    def seq(self):
        return int(self.state().get("seq") or 0)

    def handle(self, events):
        for event in events:
            if int(event["seq"]) <= self.seq():
                continue
            source = event["source"]
            # The sink first: an event it refuses keeps the old seq and comes again with the next poll.
            self.sink.emit(source, hint=event.get("hint"), wake_after=self.settings["again_after"].get(source))
            seq = int(event["seq"])
            listening.update_state(self.home, NAME, lambda state: state.update(seq=seq, last_event_at=self.clock()))

    def catch_up(self, why):
        log(f"relay {why}: checking every relayed source")
        for source in self.settings.get("sources", []):
            self.sink.emit(source)

    def numbering(self, page):
        """Reset or retention gap: catch up once and move past it. True when the page's events are void."""
        state = self.state()
        since, known = int(state.get("seq") or 0), state.get("epoch")
        head, epoch = page.get("head", page.get("last")), page.get("epoch")
        events = page.get("events", [])
        if (epoch and known and epoch != known) or (head is not None and int(head) < since):
            self.catch_up(f"reset (seq {since} -> {head})")
            listening.update_state(self.home, NAME, lambda s: s.update(seq=int(head or 0), epoch=epoch or known))
            return True
        if epoch and not known:
            listening.update_state(self.home, NAME, lambda s: s.update(epoch=epoch))
        oldest = int(events[0]["seq"]) if events else page.get("first")
        if oldest is not None and head is not None and int(head) > since and int(oldest) > since + 1:
            self.catch_up(f"events {since + 1}..{int(oldest) - 1} expired")
            listening.update_state(self.home, NAME, lambda s: s.update(seq=int(oldest) - 1))
        return False

    def fallback(self, source, now):
        """Nudge one source unless it already had a fallback check within `fallback_seconds`."""
        last = float(self.state().get("fallback_at", {}).get(source, 0))
        if 0 <= now - last < self.settings["fallback_seconds"]:
            return False
        self.sink.emit(source)
        listening.update_state(self.home, NAME, lambda state: state.setdefault("fallback_at", {}).update(
            {source: now}))
        return True

    def liveness(self, page):
        """Fallback for each relayed source whose listener is dead (relays without heartbeats: skipped)."""
        beats = page.get("sources")
        if beats is None:
            return
        server_now = float(page.get("now") or self.clock())
        for source in self.settings.get("sources", []):
            beat = beats.get(source) or {}
            up_at = beat.get("heartbeat") if beat.get("connected") else beat.get("alive_at")
            alive = server_now - float(up_at or 0) < self.settings["stale_seconds"]
            if not alive and self.fallback(source, self.clock()):
                log(f"no live listener for {source} behind the relay: fallback check")

    def connected(self):
        now = self.clock()
        self.down_since = None
        listening.update_state(self.home, NAME, lambda state: state.update(connected=True, heartbeat=now))

    def failed(self):
        """Relay unreachable: after `fallback_seconds`, nudge every relayed source so the Mac checks them."""
        now = self.clock()
        self.down_since = self.down_since or now
        listening.update_state(self.home, NAME, lambda state: state.update(connected=False, heartbeat=now))
        if now - self.down_since < self.settings["fallback_seconds"]:
            return False
        fired = [self.fallback(source, now) for source in self.settings.get("sources", [])]
        return any(fired)

    def step(self):
        """One long poll; returns the number of events handled."""
        page = self.fetch(self.seq())
        if isinstance(page, list):
            page = {"events": page}  # a fetch that returns bare events
        self.connected()
        events = [] if self.numbering(page) else page.get("events", [])
        self.handle(events)
        self.liveness(page)
        return len(events)


def http_fetch(url, token, wait=55):
    import urllib.request

    def fetch(since):
        request = urllib.request.Request(f"{url.rstrip('/')}/events?since={since}&wait={wait}",
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=wait + 15) as response:
            return json.load(response)

    return fetch


def run_client(home, settings):
    token = read_secret(settings["token_command"])
    client = Client(home, settings, fetch=http_fetch(settings["url"], token))
    stop = threading.Event()

    threading.Thread(target=listening.keep_pumping, args=(stop, client.sink.pump), daemon=True).start()
    log(f"relay client for {settings['url']}, seq {client.seq()}")
    backoff = 5
    while True:
        try:
            client.step()
            backoff = 5
        except Exception as exc:  # noqa: BLE001 - keep going; the fallback nudges keep sources checked
            if client.failed():
                log("relay unreachable: fallback check of relayed sources")
            log(f"relay error: {type(exc).__name__}: {str(exc)[:160]}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hermes Auto relay queue (server) and its Mac client")
    parser.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-auto")
    parser.add_argument("command", choices=["serve", "client", "install-launchd"])
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    settings = json.loads((home / "config.json").read_text()).get("relay", {})
    if args.command == "install-launchd":
        # The Mac client as a user service.
        import plistlib
        import os
        import subprocess
        entry = Path(sys.executable).parent / "hermes-auto-relay"
        label = "io.github.hermes-auto.relay-client"
        agent = Path.home() / f"Library/LaunchAgents/{label}.plist"
        agent.write_bytes(plistlib.dumps({
            "Label": label, "ProgramArguments": [str(entry), "--home", str(home), "client"],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())},
            "StandardOutPath": str(home / "relay-client.log"), "StandardErrorPath": str(home / "relay-client.log")}))
        os.chmod(agent, 0o600)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(agent)], check=True)
        print(agent)
        return 0
    if args.command == "serve":
        serve(home, settings)
    else:
        run_client(home, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
