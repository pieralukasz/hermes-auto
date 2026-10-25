"""Slack Socket Mode listener: nudges the sorter the moment something addressed to the user arrives.

Runs as its own long-lived user service. It never reads or stores message text beyond what it needs
to decide relevance in memory, never posts, and never touches the automation database (the run lock
belongs to `hermes-auto tick`). It writes `slack-listener.json` in the automation home:

- `heartbeat`: last sign of a live connection (the Slack source relaxes its polling while it is fresh),
- `hints`: conversations with new activity ({channel, thread_ts, why, ts}; ids only),
- `nudge_at`: when the latest relevant message arrived,
- `pending_hints`, `catchup_owed`, `retry_at`: what the sink has not accepted yet (ids only), retried
  by the pump with backoff.

When a conversation has been quiet for `quiet_seconds` it runs `hermes-auto tick`, which then performs
a Slack-only check (the source's `due`). Socket Mode replays nothing missed while disconnected (sleep,
restarts), so every `hello` owes one hintless nudge, delivered at a bounded rate.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from hermes_auto import listening
from hermes_auto.listening import LocalSink, log, sink_from_config

from . import Http, Source, read_token

NAME = "slack"
# Socket Mode replays nothing missed while disconnected: every `hello` owes one hintless nudge (a
# Slack-only check). Delivered at most once per CATCHUP_SECONDS, so a reconnect storm cannot flood the
# sorter; failed publishes retry with backoff between RETRY_MIN and RETRY_MAX seconds.
CATCHUP_SECONDS = 60
RETRY_MIN, RETRY_MAX = 5, 300


def read_state(home):
    return listening.read_state(home, NAME)


def write_state(home, data):
    listening.write_state(home, NAME, data)


def app_token(settings):
    command = settings.get("app_token_command") or Source.defaults()["app_token_command"]
    result = subprocess.run(list(command), capture_output=True, text=True, timeout=30)
    token = result.stdout.strip()
    if result.returncode or not token.startswith("xapp-"):
        raise RuntimeError("No Slack app-level token (xapp-) from app_token_command")
    return token


class Listener:
    def __init__(self, home, settings, http=None, clock=time.time, runner=None, sink=None):
        self.home = Path(home)
        self.settings = {**Source.defaults(), **settings}
        self.http = http or Http(read_token(self.settings["token_file"]))
        self.clock = clock
        # Where events go: this machine's automation (LocalSink) or the relay queue (RelaySink).
        self.sink = sink or LocalSink(self.home, {NAME: float(self.settings["quiet_seconds"]) + 5}, runner, clock)
        self.me = self.http.api("auth.test")["user_id"]
        self.followed = {}  # (channel, thread_ts) -> (followed, checked_at)
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
        """Why this message concerns the user ("dm", "mention", "thread", "channel"), or None."""
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
        if channel in (self.settings.get("watch_channels") or []) and thread_ts in (None, event.get("ts")):
            return "channel"
        if thread_ts and thread_ts != event.get("ts") and self.thread_followed(channel, thread_ts):
            return "thread"
        return None

    # ── Events ──────────────────────────────────────────────────────────

    def handle(self, envelope):
        """Process one Socket Mode envelope; returns the ack to send (or None)."""
        kind = envelope.get("type")
        if kind == "hello":
            self.beat(connected=True, catchup=True)
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
            if why == "channel":
                thread_ts = event.get("ts")  # each watched post is its own conversation
            self.hint(channel, thread_ts, why, event.get("ts"))
        return ack

    def hint(self, channel, thread_ts, why, ts):
        # Ids only; a followed thread outranks a single mention in it (merged by the sink).
        hint = {"channel": channel, "thread_ts": thread_ts, "why": why, "ts": ts}
        before = self.clock()
        state = read_state(self.home)
        if state.get("pending_hints") or before < float(state.get("retry_at") or 0):
            # The sink is failing: queue behind the earlier hints instead of blocking this event.
            self.queue(hint)
            return
        try:
            self.sink.emit(NAME, hint=hint)
        except Exception as exc:  # noqa: BLE001 - durable before the ack; the pump retries it
            log(f"publish failed: {type(exc).__name__}: {str(exc)[:160]}")
            self.queue(hint, failed=True)
            return
        self.settled(before)

    def queue(self, hint, failed=False):
        """Keep a hint in the state file until the sink accepts it (one per conversation, newest wins)."""
        key = f"{hint['channel']}:{hint.get('thread_ts') or ''}"

        def change(state):
            pending = state.setdefault("pending_hints", {})
            old = pending.get(key) or {}
            newest = hint if float(hint.get("ts") or 0) >= float(old.get("ts") or 0) else old
            why = "thread" if "thread" in (old.get("why"), hint.get("why")) else newest.get("why")
            pending[key] = {**newest, "why": why}
            if failed and self.clock() >= float(state.get("retry_at") or 0):
                self.back_off(state)

        listening.update_state(self.home, NAME, change)

    def back_off(self, state):
        delay = min(max(float(state.get("retry_delay") or 0) * 2, RETRY_MIN), RETRY_MAX)
        state["retry_delay"], state["retry_at"] = delay, self.clock() + delay

    def settled(self, before):
        """A hint accepted after a `hello` was a nudge too: the catch-up owed up to `before` is paid
        (a later `hello` keeps its own)."""
        if read_state(self.home).get("catchup_owed") is None:
            return

        def change(state):
            if float(state.get("catchup_owed") or before) <= before:
                state.pop("catchup_owed", None)
            state["catchup_sent_at"] = before

        listening.update_state(self.home, NAME, change)

    def beat(self, connected=None, catchup=False):
        def change(state):
            state["heartbeat"] = self.clock()
            state["events_since_start"], state["last_event_at"] = self.events, self.last_event
            if connected is not None:
                state["connected"] = connected
            if catchup:
                state["catchup_owed"] = self.clock()

        state = listening.update_state(self.home, NAME, change)
        beat = getattr(self.sink, "beat", None)
        if beat:
            # The relay's liveness view: the Mac checks Slack itself once these stop.
            beat([NAME], bool(state.get("connected")))

    def pump(self):
        self.deliver()
        self.sink.pump()

    def deliver(self):
        """Publish what is owed (pending hints, else the catch-up nudge) outside the state lock; forget
        each piece only once the sink accepted it, and only if no newer version replaced it meanwhile."""
        now = self.clock()
        state = read_state(self.home)
        if now < float(state.get("retry_at") or 0):
            return
        pending = dict(state.get("pending_hints") or {})
        owed = state.get("catchup_owed")
        catchup = owed is not None and now - float(state.get("catchup_sent_at") or 0) >= CATCHUP_SECONDS
        if not pending and not catchup:
            return
        sent, failed = [], False
        try:
            for key, hint in pending.items():
                self.sink.emit(NAME, hint=hint)
                sent.append((key, hint))
            if catchup and not sent:
                self.sink.emit(NAME)
        except Exception as exc:  # noqa: BLE001 - stays in the state file; retried with backoff
            log(f"publish failed: {type(exc).__name__}: {str(exc)[:160]}")
            failed = True

        def change(state):
            hints = state.get("pending_hints") or {}
            for key, hint in sent:
                if hints.get(key) == hint:
                    del hints[key]
            if hints:
                state["pending_hints"] = hints
            else:
                state.pop("pending_hints", None)
            if failed:
                self.back_off(state)
            else:
                state.pop("retry_at", None)
                state.pop("retry_delay", None)
            if sent or (catchup and not failed):
                owed_now = state.get("catchup_owed")
                if owed_now is not None and (owed_now == owed or float(owed_now) <= now):
                    state.pop("catchup_owed", None)
                state["catchup_sent_at"] = now

        listening.update_state(self.home, NAME, change)


def on_message(listener, socket, raw):
    """Handle one frame. websocket-client swallows callback errors, so an event that could not be kept
    closes the connection unacked: Slack redelivers it, and the reconnect's `hello` owes a catch-up."""
    try:
        envelope = json.loads(raw)
        ack = listener.handle(envelope)
    except Exception as exc:  # noqa: BLE001
        log(f"event not kept, reconnecting: {type(exc).__name__}: {str(exc)[:160]}")
        socket.close()
        return
    if ack:
        socket.send(json.dumps(ack))
    if envelope.get("type") == "disconnect":
        socket.close()


def serve(home, settings, config=None):
    import websocket

    full = {**Source.defaults(), **settings}
    listener = Listener(home, settings, sink=sink_from_config(home, config or {},
                                                              {NAME: float(full["quiet_seconds"]) + 5}))
    token = app_token(listener.settings)
    stop = threading.Event()

    beat_at = [0.0]

    def pumping():
        listener.pump()
        if time.time() - beat_at[0] > 60:
            beat_at[0] = time.time()
            listener.beat()  # also while disconnected, so the relay sees the listener is down, not dead

    threading.Thread(target=listening.keep_pumping, args=(stop, pumping), daemon=True).start()
    backoff = 5
    while True:
        try:
            opened = Http(token).api("apps.connections.open", _post=True)
            url = opened["url"]
            log("connecting")

            app = websocket.WebSocketApp(url, on_message=lambda socket, raw: on_message(listener, socket, raw))
            app.run_forever(ping_interval=30, ping_timeout=10)
            log("disconnected")
            backoff = 5
        except Exception as exc:  # noqa: BLE001 - keep the service alive; polling covers the gap
            log(f"error: {type(exc).__name__}: {str(exc)[:200]}")
            backoff = min(backoff * 2, 300)
        listener.beat(connected=False)
        time.sleep(backoff)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Slack Socket Mode listener for Hermes Auto")
    parser.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-auto")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "install-launchd", "check"])
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    config = json.loads((home / "config.json").read_text())
    settings = config.get("sources", {}).get("slack", {})
    if args.command == "install-launchd":
        print(listening.install_launchd(home, NAME, "hermes-auto-slack-listen"))
        return 0
    if args.command == "check":
        token = app_token({**Source.defaults(), **settings})
        print(json.dumps({"app_token": "ok", "socket": bool(Http(token).api("apps.connections.open",
                                                                         _post=True).get("url"))}))
        return 0
    serve(home, settings, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
