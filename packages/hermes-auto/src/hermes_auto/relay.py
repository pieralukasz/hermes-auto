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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import listening
from .listening import LocalSink, log, read_secret

KEEP_SECONDS = 7 * 86400
MAX_BATCH = 500


class Queue:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "at REAL NOT NULL, source TEXT NOT NULL, data TEXT NOT NULL)")
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
        return self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]


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
            self.reply(200, {"events": queue.since(since, wait), "last": queue.last()})

        def do_POST(self):
            if not self.authorized():
                return self.reply(403, {"error": "forbidden"})
            if urlparse(self.path).path != "/publish":
                return self.reply(404, {"error": "not found"})
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
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
    """Mac side: long-poll the relay, hand events to the local sink, remember the sequence number."""

    def __init__(self, home, settings, fetch=None, sink=None, clock=time.time):
        self.home = Path(home)
        self.settings = {"fallback_seconds": 1800, "again_after": {}, "quiet_seconds": {}, **settings}
        self.fetch = fetch
        self.sink = sink or LocalSink(self.home, self.settings["quiet_seconds"])
        self.clock = clock
        self.down_since = None
        self.fallback_at = 0.0

    def seq(self):
        return int(listening.read_state(self.home, "relay").get("seq") or 0)

    def handle(self, events):
        for event in events:
            if int(event["seq"]) <= self.seq():
                continue
            source = event["source"]
            self.sink.emit(source, hint=event.get("hint"), wake_after=self.settings["again_after"].get(source))
            seq = int(event["seq"])
            listening.update_state(self.home, "relay", lambda state: state.update(seq=seq, last_event_at=self.clock()))

    def connected(self):
        now = self.clock()
        self.down_since = None
        listening.update_state(self.home, "relay", lambda state: state.update(connected=True, heartbeat=now))

    def failed(self):
        """Relay unreachable: after `fallback_seconds`, nudge every relayed source so the Mac checks them."""
        now = self.clock()
        self.down_since = self.down_since or now
        listening.update_state(self.home, "relay", lambda state: state.update(connected=False, heartbeat=now))
        if now - self.down_since >= self.settings["fallback_seconds"] and \
                now - self.fallback_at >= self.settings["fallback_seconds"]:
            self.fallback_at = now
            for source in self.settings.get("sources", []):
                self.sink.emit(source)
            return True
        return False

    def step(self):
        """One long poll; returns the number of events handled."""
        events = self.fetch(self.seq())
        self.connected()
        self.handle(events)
        return len(events)


def http_fetch(url, token, wait=55):
    import urllib.request

    def fetch(since):
        request = urllib.request.Request(f"{url.rstrip('/')}/events?since={since}&wait={wait}",
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=wait + 15) as response:
            return json.load(response)["events"]

    return fetch


def run_client(home, settings):
    token = read_secret(settings["token_command"])
    client = Client(home, settings, fetch=http_fetch(settings["url"], token))
    stop = threading.Event()

    def pumping():
        while not stop.is_set():
            client.sink.pump()
            stop.wait(1)

    threading.Thread(target=pumping, daemon=True).start()
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
