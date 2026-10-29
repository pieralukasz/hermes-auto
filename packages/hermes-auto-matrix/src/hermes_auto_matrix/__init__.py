"""Matrix source for the sorter: chats bridged into the user's own homeserver (mautrix WhatsApp, Messenger,
Instagram, later LinkedIn).

Reads the homeserver with the user's access token, GET only: an incremental `/sync` from a stored `since` token finds
rooms with new messages, `/rooms/{id}/messages` reads the chat around them. Nothing is sent, joined or marked read
here: a Matrix read receipt would travel through the bridge to the sender ("read" in WhatsApp/Messenger), so the
source has no `mark_read`. What counts: a message from someone else in a bridged chat (a DM or a group). The user's
own messages (his Matrix account, which double puppeting uses, or a puppet listed in `own_puppets`), the bridge
bots, their management rooms, spaces, notices, edits and redactions never count. Messages of one room are handed
over as one burst once nobody has written for `quiet_seconds`; the room is the conversation, so later bursts follow
into the same session (as in Slack). The messenger comes from the room's `m.bridge` state, else the puppet's prefix.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "/_matrix/client/v3"
# True while bursts wait for quiet or were cut off by `limit`: the next check comes at once (cli.fast_sources).
BACKLOG = "source_backlog:matrix"
ROOM_ID = re.compile(r"![A-Za-z0-9._~=+/-]{1,255}(?::[A-Za-z0-9.-]{1,255}(?::[0-9]{1,5})?)?")
MESSAGE_TYPES = ("m.room.message", "m.sticker")
# Bridge protocol ids and puppet prefixes (mautrix defaults) → the messenger shown to the user.
CHANNELS = {"whatsapp": "WhatsApp", "facebook": "Messenger", "facebookgo": "Messenger", "messenger": "Messenger",
            "instagram": "Instagram", "instagramgo": "Instagram", "linkedin": "LinkedIn", "signal": "Signal",
            "telegram": "Telegram"}
PUPPET = re.compile(r"@([a-z]+)_[^:]+:")
MEDIA = {"m.image": "[zdjęcie]", "m.video": "[wideo]", "m.audio": "[nagranie]", "m.file": "[plik]",
         "m.location": "[lokalizacja]"}
ROOM_CACHE_SECONDS = 86400
SYNC_FILTER = {"presence": {"not_types": ["*"]}, "account_data": {"not_types": ["*"]},
               "room": {"timeline": {"limit": 50, "types": list(MESSAGE_TYPES)},
                        "state": {"not_types": ["*"]}, "ephemeral": {"not_types": ["*"]},
                        "account_data": {"not_types": ["*"]}, "include_leave": False}}
# The baseline only needs a position: no message, no state.
BASELINE_FILTER = {**SYNC_FILTER, "room": {**SYNC_FILTER["room"], "timeline": {"limit": 0}}}
# Without it every /sync marks the account online, a change other users and the bridges see.
PRESENCE = {"set_presence": "offline"}
ERRCODE = re.compile(r"M_[A-Z0-9_]{1,64}")


class MatrixError(RuntimeError):
    pass


def read_token(settings):
    """The access token from `token_keychain` (macOS Keychain service name) or `token_file` (mode 0600)."""
    service = settings.get("token_keychain")
    if service:
        done = subprocess.run(["security", "find-generic-password", "-s", str(service), "-w"],
                              capture_output=True, text=True, timeout=30)
        if done.returncode or not done.stdout.strip():
            raise MatrixError(f"No Matrix token in the Keychain item {service!r}")
        return done.stdout.strip()
    path = Path(os.path.expanduser(str(settings.get("token_file") or "")))
    try:
        mode = path.stat().st_mode
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MatrixError(f"Cannot read the Matrix token file {path} ({exc.__class__.__name__})") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise MatrixError(f"The Matrix token file {path} is readable by others; chmod 600 it")
    if not token:
        raise MatrixError(f"The Matrix token file {path} is empty")
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: urllib would carry the Authorization header to whatever origin it names."""

    def redirect_request(self, *args, **kwargs):
        return None


class Http:
    """GET-only JSON client for the client-server API; the token never appears in an error and never follows a
    redirect."""

    def __init__(self, homeserver, token):
        self.base = str(homeserver).rstrip("/")
        self._token = token
        self._opener = urllib.request.build_opener(NoRedirect)

    def get(self, path, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"}, method="GET")
        try:
            with self._opener.open(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                code = str(json.load(exc).get("errcode", ""))
            except (ValueError, AttributeError):
                code = ""
            code = code if ERRCODE.fullmatch(code) else ""  # server text: shown only as a Matrix code
            raise MatrixError(f"Matrix GET {path.split('?')[0]} failed: HTTP {exc.code} {code}".strip()) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise MatrixError(f"Matrix GET {path.split('?')[0]} failed: {exc.__class__.__name__}") from None


def quote(room_id):
    return urllib.parse.quote(room_id, safe="")


def localpart(user_id):
    return str(user_id or "")[1:].split(":", 1)[0]


def message_text(event):
    content = event.get("content") or {}
    if event.get("type") == "m.sticker":
        return "[naklejka]"
    body = str(content.get("body") or "")
    if (content.get("m.relates_to") or {}).get("m.in_reply_to"):
        # Drop the quoted reply fallback ("> <@x> ...") the bridge or client prepends.
        lines = body.split("\n")
        while lines and lines[0].startswith(">"):
            lines.pop(0)
        body = "\n".join(lines).strip()
    media = MEDIA.get(content.get("msgtype"))
    if media:
        return f"{media} {body}".strip() if content.get("msgtype") == "m.file" else media
    return " ".join(body.split())


class Matrix:
    def __init__(self, settings, http=None, now=None):
        self.settings = settings
        if http is None and not settings.get("homeserver"):
            raise MatrixError("Set sources.matrix.homeserver (the homeserver URL) in config.json")
        self.http = http or Http(settings["homeserver"], read_token(settings))
        self.now = now or time.time
        self._me = None
        self._members = {}

    def me(self):
        if self._me is None:
            self._me = self.http.get(f"{API}/account/whoami")["user_id"]
        return self._me

    def own(self, sender):
        return sender == self.me() or sender in set(self.settings.get("own_puppets") or [])

    def bot(self, sender):
        return localpart(sender) in {localpart(bot) for bot in self.settings.get("bridge_bots") or []}

    # ── Rooms ───────────────────────────────────────────────────────────

    def room(self, store, room_id):
        """{"channel", "kind" (dm, group, management, space), "name"} of a room, cached for a day."""
        key = f"matrix_room:{room_id}"
        cached = store.get_meta(key)
        if cached and self.now() - float(cached.get("at", 0)) < ROOM_CACHE_SECONDS:
            return cached
        state = self.http.get(f"{API}/rooms/{quote(room_id)}/state")
        bridge = next((event.get("content") or {} for event in state
                       if event.get("type") in ("m.bridge", "uk.half-shot.bridge")), None)
        create = next((event.get("content") or {} for event in state if event.get("type") == "m.room.create"), {})
        name = next(((event.get("content") or {}).get("name") for event in state
                     if event.get("type") == "m.room.name"), "") or ""
        members = [event.get("state_key") for event in state if event.get("type") == "m.room.member"
                   and (event.get("content") or {}).get("membership") == "join"]
        protocol = ((bridge or {}).get("protocol") or {})
        channel = CHANNELS.get(str(protocol.get("id") or "").lower()) or protocol.get("displayname") or ""
        room_type = (bridge or {}).get("com.beeper.room_type.v2") or (bridge or {}).get("com.beeper.room_type")
        if create.get("type") == "m.space" or room_type == "personal_filtering_space":
            kind = "space"
        elif bridge is None and any(self.bot(member) for member in members):
            kind = "management"  # a chat with a bridge bot (login, status): the bridge's own room
        elif room_type == "dm" or (bridge is None and len(members) <= 2):
            kind = "dm"
        else:
            kind = "group"
        info = {"at": self.now(), "channel": channel, "kind": kind, "name": str(name).strip()[:120]}
        store.set_meta(key, info)
        return info

    def members(self, room_id):
        if room_id not in self._members:
            joined = self.http.get(f"{API}/rooms/{quote(room_id)}/joined_members").get("joined") or {}
            self._members[room_id] = {user: (data or {}).get("display_name") or localpart(user)
                                      for user, data in joined.items()}
        return self._members[room_id]

    def name(self, room_id, user_id):
        if self.own(user_id):
            return "Łukasz"
        return self.members(room_id).get(user_id) or localpart(user_id)

    def history(self, room_id, limit):
        """The room's latest `limit` messages, oldest first."""
        data = self.http.get(f"{API}/rooms/{quote(room_id)}/messages",
                             {"dir": "b", "limit": int(limit), "filter": json.dumps({"types": list(MESSAGE_TYPES)})})
        return list(reversed([event for event in data.get("chunk") or [] if event.get("type") in MESSAGE_TYPES]))

    # ── Candidates ──────────────────────────────────────────────────────

    def relevant(self, event, now):
        """A message from someone else with something in it: not the user, not a bridge bot, no notice, edit or
        redaction, nothing older than `max_age_hours` (a bridge backfills old history with new events)."""
        content = event.get("content") or {}
        sender = event.get("sender", "")
        if event.get("type") not in MESSAGE_TYPES or not content or self.own(sender) or self.bot(sender):
            return False
        if content.get("msgtype") == "m.notice" or (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
            return False
        if "fi.mau.double_puppet_source" in content:
            return False  # sent by the user from his phone, written through his own account
        if now - int(event.get("origin_server_ts") or 0) / 1000 > float(self.settings["max_age_hours"]) * 3600:
            return False
        return bool(message_text(event))

    def inbox(self, config, store, limit=40, since_ms=None):
        """Bursts from other people since the stored `since` token: (candidates, cursor key, new cursor)."""
        now = self.now()
        key = f"triage_cursor:matrix:{self.me()}"
        cursor = store.get_meta(key)
        if cursor is None:
            # First check: the baseline. Nothing older than this ever reaches the sorter.
            data = self.http.get(f"{API}/sync", {"filter": json.dumps(BASELINE_FILTER), "timeout": 0, **PRESENCE})
            store.set_meta(BACKLOG, False)
            return [], key, {"since": data["next_batch"], "start": now, "seen": {}, "waiting": {}}
        start = float(cursor.get("start", 0))
        # Handed-over event ids per room, with their times. Identity, not time, says what is new: a bridge
        # delivering a backlog keeps each message's own time, so a message can arrive after a later one.
        seen = {room: dict(ids) for room, ids in (cursor.get("seen") or {}).items()}
        waiting = {room: dict(span) for room, span in (cursor.get("waiting") or {}).items()}
        skip = set(self.settings.get("skip_rooms") or [])

        def new(room_id, event):
            return (self.relevant(event, now) and int(event["origin_server_ts"]) / 1000 > start
                    and event.get("event_id") not in seen.get(room_id, {}))
        data = self.http.get(f"{API}/sync", {"filter": json.dumps(SYNC_FILTER), "since": cursor["since"],
                                              "timeout": 0, **PRESENCE})
        for room_id, room in ((data.get("rooms") or {}).get("join") or {}).items():
            if room_id in skip:
                continue
            stamps = [int(event["origin_server_ts"]) / 1000 for event in (room.get("timeline") or {}).get("events", [])
                      if new(room_id, event)]
            if stamps or (room.get("timeline") or {}).get("limited"):
                span = waiting.setdefault(room_id, {"first": min(stamps or [now]), "last": max(stamps or [now])})
                span["last"] = max([span["last"], *stamps])
        quiet = float(self.settings["quiet_seconds"])
        ready = []
        for room_id, span in list(waiting.items()):
            if now - float(span["last"]) < quiet:
                continue  # still being written; read once it is quiet
            info = self.room(store, room_id)
            if info["kind"] in ("management", "space") or room_id in skip:
                waiting.pop(room_id)
                continue
            events = self.history(room_id, int(self.settings["context_messages"]) + int(self.settings["max_burst"]))
            fresh = [event for event in events if new(room_id, event)]
            if not fresh:
                waiting.pop(room_id)
                continue
            last_at = max(int(event["origin_server_ts"]) / 1000 for event in fresh)
            if now - last_at < quiet:
                span.update(last=last_at)  # a message the sync had not shown yet
                continue
            ready.append((last_at, room_id, info, events, fresh))
        ready.sort(key=lambda entry: entry[0])
        candidates = []
        for last_at, room_id, info, events, fresh in ready[:limit]:
            candidates.append(self.candidate(room_id, info, events, fresh))
            seen.setdefault(room_id, {}).update({event["event_id"]: int(event["origin_server_ts"]) / 1000
                                                 for event in fresh})
            waiting.pop(room_id, None)
        # A message older than max_age_hours is never new again, so its id need not be kept.
        horizon = now - float(self.settings["max_age_hours"]) * 3600 - 3600
        seen = {room: kept for room, ids in seen.items()
                if (kept := {event_id: at for event_id, at in ids.items() if at >= horizon})}
        store.set_meta(BACKLOG, bool(waiting))
        return candidates, key, {"since": data["next_batch"], "start": start, "seen": seen, "waiting": waiting}

    def candidate(self, room_id, info, events, fresh):
        last = fresh[-1]
        ids = {event["event_id"] for event in fresh}
        senders = []
        for event in fresh:
            shown = self.name(room_id, event["sender"])
            if shown not in senders:
                senders.append(shown)
        sender = ", ".join(senders)
        channel = info["channel"] or next((CHANNELS.get(match.group(1)) for match in
                                          (PUPPET.match(event["sender"]) for event in fresh) if match
                                          and match.group(1) in CHANNELS), None) or "Matrix"
        group = info["kind"] == "group"
        place = (f"grupa „{info['name']}”" if info["name"] else "grupa") if group else "DM"
        last_at = int(last["origin_server_ts"]) / 1000
        answered = any(self.own(event.get("sender")) and int(event["origin_server_ts"]) / 1000 > last_at
                       for event in events)
        return {
            "kind": "matrix", "id": f"{room_id}@{last['event_id']}", "thread_id": room_id, "account": self.me(),
            "read": answered, "from": sender, "to": "Łukasz", "subject": f"{channel}: {sender}",
            "why": "group" if group else "dm", "channel": channel, "place": place, "room_id": room_id,
            "dm": not group, "group": group, "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(last_at)),
            "snippet": message_text(last)[:300], "body": self.transcript(room_id, events, ids)[:20000],
            "url": "", "sent": [], "event_ids": [event["event_id"] for event in fresh],
        }

    def transcript(self, room_id, events, fresh_ids):
        first_new = next(index for index, event in enumerate(events) if event["event_id"] in fresh_ids)
        context = int(self.settings["context_messages"])
        lines = []
        for event in events[max(0, first_new - context):]:
            if (event.get("content") or {}).get("msgtype") == "m.notice" or not message_text(event):
                continue
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(event["origin_server_ts"]) / 1000))
            mark = "NOWE " if event["event_id"] in fresh_ids else ""
            lines.append(f"{mark}[{stamp}] {self.name(room_id, event['sender'])}: {message_text(event)}")
        return "\n".join(lines)


def history_lines(matrix, room_id, limit):
    """The chat as text lines, oldest first (`hermes-auto-matrix history`: the first turn's read of its own room)."""
    if not ROOM_ID.fullmatch(str(room_id)):
        raise MatrixError("Not a Matrix room id (!...)")
    lines = []
    for event in matrix.history(room_id, max(1, min(int(limit), 100))):
        if (event.get("content") or {}).get("msgtype") == "m.notice" or not message_text(event):
            continue
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(event["origin_server_ts"]) / 1000))
        lines.append(f"[{stamp}] {matrix.name(room_id, event['sender'])}: {message_text(event)}")
    return lines


class Source:
    @staticmethod
    def defaults():
        return {"enabled": False,
                # The homeserver URL (required, e.g. over Tailscale) and the user's access token.
                "homeserver": "",
                "token_file": "~/.config/hermes-auto/secrets/matrix.token",
                # A macOS Keychain service name instead of the file (security find-generic-password -s ... -w).
                "token_keychain": "",
                # Bridge bots: their messages and their management rooms never count.
                "bridge_bots": ["@whatsappbot", "@facebookbot", "@instagrambot", "@linkedinbot"],
                # The user's own puppets (without double puppeting his messages come from these).
                "own_puppets": [],
                "skip_rooms": [],
                # A room is handed over after nobody wrote for this long (seconds).
                "quiet_seconds": 90,
                # No push to this Mac: check this often (seconds; 0 = only full checks). One /sync per check.
                "poll_seconds": 60,
                # Messages older than this are never new (a bridge backfilling history).
                "max_age_hours": 24,
                "context_messages": 8, "max_burst": 40}

    def due(self, settings, store, last_poll, now):
        settings = {**self.defaults(), **settings}
        return bool(settings["poll_seconds"]) and now - last_poll >= float(settings["poll_seconds"])

    def poll(self, config, store):
        """Nothing to watch by label: messages go through the sorter (`inbox`)."""

    def inbox(self, config, store, limit=40, since_ms=None):
        return Matrix({**self.defaults(), **config["sources"]["matrix"]}).inbox(config, store, limit, since_ms)

    def setup(self, config):
        Matrix({**self.defaults(), **config["sources"]["matrix"]}).me()


def settings_from_config():
    """sources.matrix of the hermes-auto config (HERMES_AUTO_HOME or ~/.config/hermes-auto) over the defaults."""
    home = Path(os.environ.get("HERMES_AUTO_HOME", "~/.config/hermes-auto")).expanduser()
    try:
        raw = json.loads((home / "config.json").read_text()).get("sources", {}).get("matrix", {})
    except (OSError, ValueError):
        raw = {}
    return {**Source.defaults(), **(raw if isinstance(raw, dict) else {})}


def main(argv=None):
    """`hermes-auto-matrix history ROOM [--limit N]`: the chat's latest messages (read-only; the guard lets a
    chat session's first turn run it for its own room). `hermes-auto-matrix check`: token and room counts."""
    parser = argparse.ArgumentParser(prog="hermes-auto-matrix", description=main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    history = commands.add_parser("history", help="The latest messages of one room, oldest first")
    history.add_argument("room")
    history.add_argument("--limit", type=int, default=30)
    commands.add_parser("check", help="Check the token; print the user and the number of joined rooms")
    args = parser.parse_args(argv)
    try:
        matrix = Matrix(settings_from_config())
        if args.command == "history":
            print("\n".join(history_lines(matrix, args.room, args.limit)))
        else:
            rooms = matrix.http.get(f"{API}/joined_rooms").get("joined_rooms") or []
            print(json.dumps({"user": matrix.me(), "joined_rooms": len(rooms)}))
    except MatrixError as exc:
        print(f"hermes-auto-matrix: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
