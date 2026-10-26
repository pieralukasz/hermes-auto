"""Slack source for the sorter: everything addressed to the user becomes a mail-like candidate.

What counts: direct messages and group DMs, @mentions of the user in channels, replies in
threads the user follows (Slack subscribes you to threads you started, wrote in or were mentioned
in), and every new top-level post in the channels listed in `watch_channels` (e.g. a bug-report
channel), each post its own candidate. Bots, Slackbot, the user's own messages, edits, joins and @here/@channel alone never count.

Messages of one conversation (a DM, or a thread) are handed over as one burst once nobody has
written for `quiet_seconds`; if the user read or answered it in Slack before that, the candidate is
marked read and the sorter opens nothing. The adapter reads Slack (Web API with the user's token, plus
Slack's MCP search for discovery) and never posts; its only write moves a DM's read marker over messages a
Hermes session or brief has shown (`Source.mark_read`; channels and threads are never marked).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

MCP_URL = "https://mcp.slack.com/mcp"
API_URL = "https://slack.com/api/"
# Re-read this far behind the cursor; handed-over messages are de-duplicated per conversation.
OVERLAP_SECONDS = 600
HUMAN_SUBTYPES = (None, "thread_broadcast", "file_share", "me_message")
MENTION = "<@{}>"
# True while handed-over bursts remain (cut off by `limit` or still being written): poll again soon.
BACKLOG = "source_backlog:slack"


class SlackError(RuntimeError):
    pass


def read_token(path):
    data = json.loads(Path(path).expanduser().read_text())
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise SlackError(f"No Slack user token in {path}; log in with 'hermes mcp login slack'")
    return token


class Http:
    """Tiny JSON client over requests; separate so tests can replace it."""

    def __init__(self, token):
        import requests
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def api(self, method, _post=False, **params):
        for attempt in range(3):
            if _post:
                response = self.session.post(API_URL + method, data=params, timeout=45)
            else:
                response = self.session.get(API_URL + method, params=params, timeout=45)
            if response.status_code == 429 and attempt < 2:
                time.sleep(min(int(response.headers.get("Retry-After") or 5), 30))
                continue
            data = response.json() if response.ok else {"ok": False, "error": f"http {response.status_code}"}
            if not data.get("ok"):
                raise SlackError(f"Slack {method} failed: {data.get('error')}")
            return data
        raise SlackError(f"Slack {method} rate limited")

    def mcp_search(self, arguments):
        """One call of the official MCP server's search tool; returns its text result."""
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-06-18"}

        def rpc(method, params=None, sid=None, rid: int | None = 1):
            body = {"jsonrpc": "2.0", "method": method}
            if rid is not None:
                body["id"] = rid
            if params is not None:
                body["params"] = params
            extra = {"Mcp-Session-Id": sid} if sid else {}
            response = self.session.post(MCP_URL, json=body, headers={**headers, **extra}, timeout=120)
            if not response.ok:
                raise SlackError(f"Slack MCP {method} failed ({response.status_code})")
            sid = response.headers.get("Mcp-Session-Id") or sid
            raw = response.text
            if "data:" in raw:
                datas = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
                raw = datas[-1] if datas else "{}"
            return (json.loads(raw) if raw.strip() else {}), sid

        _, sid = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "hermes-auto-slack", "version": "0.1"}})
        rpc("notifications/initialized", sid=sid, rid=None)
        result, _ = rpc("tools/call", {"name": "slack_search_public_and_private", "arguments": arguments}, sid, 2)
        if "error" in result:
            raise SlackError(f"Slack search failed: {result['error']}")
        parts = result.get("result", {}).get("content", [])
        text = "\n".join(part.get("text", "") for part in parts)
        if result.get("result", {}).get("isError"):
            raise SlackError(f"Slack search failed: {text[:200]}")
        return text


HIT = re.compile(r"Channel: [^\n]*\(ID: (?P<channel>[CDG][A-Z0-9]+)\)(?P<rest>.*?)(?=\nChannel: |\Z)", re.S)


def parse_hits(text):
    """(channel, ts, thread_ts or None) per hit of the MCP search's detailed text result."""
    try:
        data = json.loads(text)
        text = data.get("results", "") + "\n" + data.get("pagination_info", "")
    except (TypeError, ValueError):
        pass
    hits = []
    for match in HIT.finditer(text):
        rest = match.group("rest")
        ts = re.search(r"Message_ts: (\d+\.\d+)", rest)
        if not ts:
            continue
        thread = re.search(r"thread_ts=(\d+\.\d+)", rest)
        hits.append((match.group("channel"), ts.group(1), thread.group(1) if thread else None))
    return hits


def next_cursor(text):
    match = re.search(r"cursor `([^`]+)`", text)
    return match.group(1) if match else None


def is_human(message, me):
    return (message.get("subtype") in HUMAN_SUBTYPES and not message.get("bot_id")
            and message.get("user") not in (None, "", "USLACKBOT", me))


class Slack:
    def __init__(self, settings, http=None, now=None):
        self.settings = settings
        self.http = http or Http(read_token(settings["token_file"]))
        self.now = now or time.time
        self.users, self.channels = {}, {}
        self._me = None

    # ── Identity and names ──────────────────────────────────────────────

    def me(self):
        if self._me is None:
            self._me = self.http.api("auth.test")
        return self._me

    def user_name(self, user_id):
        if user_id not in self.users:
            try:
                user = self.http.api("users.info", user=user_id)["user"]
                profile = user.get("profile", {})
                self.users[user_id] = (profile.get("real_name") or profile.get("display_name")
                                       or user.get("real_name") or user.get("name") or user_id)
            except SlackError:
                self.users[user_id] = user_id
        return self.users[user_id]

    def channel(self, channel_id):
        if channel_id not in self.channels:
            self.channels[channel_id] = self.http.api("conversations.info", channel=channel_id)["channel"]
        return self.channels[channel_id]

    def search(self, store, label, filters, channel_types, after):
        """Every hit after `after`, newest first, across pages (bounded)."""
        hits, cursor = [], None
        for _ in range(int(self.settings.get("max_search_pages", 5))):
            # The server rejects a search without filters; the day filter is coarse, `after` is exact.
            day = time.strftime("%Y-%m-%d", time.localtime(after - 86400))
            arguments = {"filters": f"{filters} after:{day}".strip(), "after": str(int(after)),
                         "channel_types": channel_types, "limit": 20, "response_format": "detailed",
                         "sort": "timestamp"}
            if cursor:
                arguments["cursor"] = cursor
            text = self.http.mcp_search(arguments)
            hits += parse_hits(text)
            cursor = next_cursor(text)
            if not cursor:
                break
        return hits

    # ── Reading conversations ───────────────────────────────────────────

    def history(self, channel_id, oldest, limit=200, pages=5):
        messages, cursor = [], None
        for _ in range(pages):
            params = {"channel": channel_id, "oldest": f"{oldest:.6f}", "limit": limit}
            if cursor:
                params["cursor"] = cursor
            page = self.http.api("conversations.history", **params)
            messages += page.get("messages", [])
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return messages

    def replies(self, channel_id, thread_ts):
        messages, cursor = [], None
        for _ in range(5):
            params = {"channel": channel_id, "ts": thread_ts, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = self.http.api("conversations.replies", **params)
            messages += page.get("messages", [])
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return messages

    def my_channels(self, store):
        """Channels (not DMs) where the user wrote in the last `thread_days`; cached for 30 minutes."""
        key = "slack:my_channels"
        cached = store.get_meta(key)
        if cached and self.now() - cached.get("at", 0) < 1800:
            return cached["ids"]
        me = self.me()["user_id"]
        hits = self.search(store, "mine", f"from:{MENTION.format(me)}", "public_channel,private_channel",
                           self.now() - 86400 * int(self.settings["thread_days"]))
        ids = sorted({channel for channel, _, _ in hits})
        store.set_meta(key, {"at": self.now(), "ids": ids})
        return ids

    # ── Candidates ──────────────────────────────────────────────────────

    def conversations(self, store, since):
        """{conversation key: (channel_id, thread_ts or None, [messages], why)} with activity after `since`."""
        me = self.me()["user_id"]
        skip = set(self.settings.get("skip_channels", []))
        found = {}

        def add(channel_id, thread_ts, why):
            if channel_id in skip:
                return
            key = f"{channel_id}:{thread_ts or ''}"
            # A followed thread beats a single mention in it: then every reply counts, not only the mention.
            if key not in found or why == "thread":
                found[key] = (channel_id, thread_ts, why)

        # Conversations the Socket Mode listener saw: found even before search indexes them.
        for hint in listener_hints(store, since):
            add(hint["channel"], hint.get("thread_ts"), hint.get("why") or "dm")
        for channel_id, ts, thread_ts in self.search(store, "dm", "", "im,mpim", since):
            add(channel_id, thread_ts, "dm")
        for channel_id, ts, thread_ts in self.search(store, "mention", MENTION.format(me),
                                                     "public_channel,private_channel", since):
            add(channel_id, thread_ts, "mention")
        # Watched channels: every new top-level post is its own conversation (keyed by its ts).
        for channel_id in self.settings.get("watch_channels") or []:
            for message in self.history(channel_id, since, pages=1):
                if message.get("thread_ts") in (None, message.get("ts")) and is_human(message, me):
                    add(channel_id, message["ts"], "channel")
        horizon = self.now() - 86400 * int(self.settings["thread_days"])
        for channel_id in self.my_channels(store):
            if channel_id in skip:
                continue
            for parent in self.history(channel_id, horizon):
                if not parent.get("reply_count") or float(parent.get("latest_reply") or 0) <= since:
                    continue
                if parent.get("subscribed") or parent.get("user") == me or me in parent.get("reply_users", []):
                    add(channel_id, parent["ts"], "thread")
        result = {}
        for key, (channel_id, thread_ts, why) in found.items():
            if thread_ts:
                messages = self.replies(channel_id, thread_ts)
            else:
                messages = list(reversed(self.history(channel_id, since - 86400 * 2, pages=1)))
            result[key] = (channel_id, thread_ts, messages, why)
        return result

    def relevant(self, message, why, me, channel_info, thread_ts):
        if not is_human(message, me):
            return False
        if channel_info.get("is_im") or channel_info.get("is_mpim"):
            return True
        if why == "channel":
            # The watched post itself; a post mentioning him comes as a "mention" instead (no duplicate).
            return message.get("ts") == thread_ts and MENTION.format(me) not in (message.get("text") or "")
        if MENTION.format(me) in (message.get("text") or ""):
            return True
        # Channel thread the user follows: every human reply counts (the parent itself only if it mentions him).
        return bool(thread_ts) and why == "thread" and message.get("ts") != thread_ts

    def inbox(self, config, store, limit=40, since_ms=None):
        """Bursts addressed to the user since the cursor: (candidates, cursor key, new cursor)."""
        identity = self.me()
        me, team = identity["user_id"], identity.get("team_id", "")
        key = f"triage_cursor:slack:{team}"
        cursor = store.get_meta(key) if since_ms is None else {"since": since_ms / 1000, "convs": {}}
        now = self.now()
        if cursor is None:
            store.set_meta(BACKLOG, False)
            return [], key, {"since": now, "start": now, "convs": {}}
        handed = {k: float(v) for k, v in cursor.get("convs", {}).items()}
        since = float(cursor["since"])
        quiet = float(self.settings["quiet_seconds"])
        candidates, waiting = [], []
        for conv, (channel_id, thread_ts, messages, why) in self.conversations(store, since - OVERLAP_SECONDS).items():
            info = self.channel(channel_id)
            # Late search indexing: re-read the overlap, but never before the baseline or a hand-over.
            floor = max(handed.get(conv, since - OVERLAP_SECONDS), float(cursor.get("start", 0)))
            fresh = [m for m in messages if float(m["ts"]) > floor and self.relevant(m, why, me, info, thread_ts)]
            if not fresh:
                continue
            last = max(float(m["ts"]) for m in fresh)
            if now - last < quiet:
                waiting.append(min(float(m["ts"]) for m in fresh))
                continue
            candidates.append((last, conv, self.candidate(identity, info, channel_id, thread_ts, messages,
                                                          fresh, why), min(float(m["ts"]) for m in fresh)))
        candidates.sort(key=lambda item: item[0])
        # Bursts cut off by `limit` hold the cursor like bursts still being written: they come next time.
        waiting += [first for _, _, _, first in candidates[limit:]]
        candidates = [(last, conv, item) for last, conv, item, _ in candidates[:limit]]
        for last, conv, _ in candidates:
            handed[conv] = last
        horizon = now - 86400 * int(self.settings["thread_days"])
        handed = {k: v for k, v in handed.items() if v >= horizon}
        # Never move past a burst that is still being written (or one cut off by `limit`).
        mark = min(waiting) - 1 if waiting else now - quiet
        new_cursor = {"since": max(since, min(mark, now)), "start": float(cursor.get("start", 0)), "convs": handed}
        if since_ms is None:
            # Push mode polls again soon while bursts wait for quiet or were cut off (see cli.fast_sources).
            store.set_meta(BACKLOG, bool(waiting))
        return [item for _, _, item in candidates], key, new_cursor

    def candidate(self, identity, info, channel_id, thread_ts, messages, fresh, why):
        me, team_url = identity["user_id"], identity.get("url", "https://slack.com/").rstrip("/")
        last = fresh[-1]
        last_ts = float(last["ts"])
        dm = bool(info.get("is_im") or info.get("is_mpim"))
        private = bool(info.get("is_private")) and not dm
        redacted = private and self.settings.get("private_channels", "link_only") == "link_only"
        senders = []
        for message in fresh:
            name = self.user_name(message["user"])
            if name not in senders:
                senders.append(name)
        if info.get("is_im"):
            place = "DM"
        elif info.get("is_mpim"):
            place = "grupa DM"
        else:
            place = "#" + (info.get("name") or channel_id) + (" (prywatny)" if private else "")
        where = place + (" · wątek" if thread_ts and not info.get("is_im") and why != "channel" else "")
        # Read in Slack or answered there after the burst: he has seen it.
        if thread_ts and why != "channel":
            # A thread has its own read marker on the parent (followed threads only).
            parent = next((m for m in messages if m.get("ts") == thread_ts), {})
            last_read = float(parent.get("last_read") or 0)
        else:
            last_read = float(info.get("last_read") or 0)
        answered = any(m.get("user") == me and float(m["ts"]) > last_ts for m in messages)
        # Reading in Slack (e.g. a phone notification) does not settle it: the session still opens
        # unless `skip_read` is on. Answering there does.
        seen = bool(self.settings.get("skip_read")) and last_read >= last_ts
        stamp = last["ts"].replace(".", "")
        url = f"{team_url}/archives/{channel_id}/p{stamp}"
        if thread_ts and thread_ts != last["ts"]:
            url += f"?thread_ts={thread_ts}&cid={channel_id}"
        if redacted:
            body = ("Wiadomość w prywatnym kanale. Treść nie jest przekazywana (zasada Łukasza: treści z "
                    "prywatnych kanałów się nie powtarza). Tylko autor, miejsce i link.")
        else:
            body = self.transcript(messages, fresh, thread_ts, me)
        return {
            "kind": "slack", "id": f"{channel_id}:{thread_ts or ''}@{last['ts']}",
            "thread_id": f"{channel_id}:{thread_ts or ''}" if not info.get("is_im") or thread_ts else channel_id,
            "account": identity.get("team_id", ""), "read": bool(seen or answered),
            "from": ", ".join(senders), "to": "Łukasz", "subject": f"Slack: {', '.join(senders)} · {where}",
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(last_ts)), "why": why,
            "channel": channel_id, "thread_ts": thread_ts or "", "ts": last["ts"], "place": where,
            "private": private, "redacted": redacted, "dm": dm,
            "snippet": "" if redacted else " ".join((last.get("text") or "").split())[:300],
            "body": body[:20000], "url": url, "sent": [],
            # Messages this item shows: marking read may pass these and no others (Source.mark_read).
            "fresh_ts": [m["ts"] for m in fresh],
        }

    def transcript(self, messages, fresh, thread_ts, me):
        """Recent context plus the new messages, oldest first, marked so the reader sees what is new."""
        fresh_ts = {m["ts"] for m in fresh}
        ordered = sorted(messages, key=lambda m: float(m["ts"]))
        first_new = min(float(ts) for ts in fresh_ts)
        context = [m for m in ordered if float(m["ts"]) < first_new][-int(self.settings.get("context_messages", 8)):]
        if thread_ts:
            parent = next((m for m in ordered if m.get("ts") == thread_ts), None)
            if parent and parent not in context:
                context.insert(0, parent)
        lines = []
        for message in context + [m for m in ordered if m["ts"] in fresh_ts]:
            who = "Łukasz" if message.get("user") == me else self.user_name(message.get("user") or "?")
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(message["ts"])))
            mark = "NOWE " if message["ts"] in fresh_ts else ""
            text = (message.get("text") or "").strip()
            files = [f.get("name") or f.get("title") or "plik" for f in message.get("files", [])]
            if files:
                text += f" [załączniki: {', '.join(files)}]"
            lines.append(f"{mark}[{stamp}] {who}: {text}")
        return "\n".join(lines)


def automation_home(store):
    return Path(store.path).parent


def listener_state(store):
    from hermes_auto.listening import read_state
    try:
        return read_state(automation_home(store), "slack")
    except (AttributeError, TypeError):
        return {}


def listener_hints(store, since):
    hints = listener_state(store).get("hints", {})
    return [hint for hint in hints.values() if float(hint.get("ts") or 0) > since and hint.get("channel")]


class Source:
    def mark_read(self, config, items, http=None):
        """Move a DM's read marker over messages a Hermes session or brief has shown; {item id: status}.

        "done": the marker is at or past the item. "unsupported": left unread, because it cannot be marked
        safely: channel messages (the channel's marker would also read every unrelated message before the
        mention), thread replies (no public API for thread read state), and DM items with a message between the
        marker and the item that no item in this call shows (unread before the baseline, missed by discovery,
        not a human message, or more than one page to check). "retry: <error>": this conversation failed this
        time; other conversations are unaffected.

        The marker moves only over a contiguous run of shown messages and never past the newest shown one, so
        messages arriving meanwhile stay unread. Slack's API has no compare-and-set: if the user moves the
        marker further in Slack between `conversations.info` and `conversations.mark`, this call can move it
        back to the shown message (later messages then look unread again; nothing is hidden). The caller passes
        only items whose earlier items in the same conversation are shown (triage.flush_read_marks).
        """
        import subprocess
        result = {}
        by_channel = {}
        for item in items:
            if not item.get("channel") or not item.get("ts") or item.get("thread_ts"):
                result[item.get("id")] = "unsupported"
            else:
                by_channel.setdefault(item["channel"], []).append(item)
        if not by_channel:
            return result
        settings = {**self.defaults(), **config["sources"].get("slack", {})}
        if http is None:
            token = subprocess.run(list(settings["mark_token_command"]), capture_output=True, text=True, timeout=30)
            if token.returncode or not token.stdout.strip():
                raise SlackError("No Slack mark-read token (mark_token_command)")
            http = Http(token.stdout.strip())
        for channel, group in by_channel.items():
            group.sort(key=lambda item: float(item["ts"]))
            try:
                result.update(self._mark_channel(http, channel, group))
            except Exception as exc:  # noqa: BLE001 - one conversation failing must not hold the others
                result.update({item.get("id"): f"retry: {str(exc)[:200]}" for item in group})
        return result

    @staticmethod
    def _mark_channel(http, channel, group):
        info = http.api("conversations.info", channel=channel)["channel"]
        if not (info.get("is_im") or info.get("is_mpim")):
            return {item.get("id"): "unsupported" for item in group}
        last_read = float(info.get("last_read") or 0)
        shown = {float(ts) for item in group for ts in (item.get("fresh_ts") or [item["ts"]])}
        target = None
        if float(group[-1]["ts"]) > last_read:
            page = http.api("conversations.history", channel=channel, oldest=f"{last_read:.6f}",
                            latest=group[-1]["ts"], limit=200)
            if page.get("has_more"):
                unshown = [0.0]  # too much unread to check: mark nothing
            else:
                unshown = [float(m["ts"]) for m in page.get("messages", []) if float(m["ts"]) not in shown]
            limit = min(unshown, default=float("inf"))
            # Furthest item with no unshown message between the marker and it.
            target = next((item for item in reversed(group) if last_read < float(item["ts"]) <= limit), None)
        if target is not None:
            http.api("conversations.mark", _post=True, channel=channel, ts=target["ts"])
            last_read = float(target["ts"])
        return {item.get("id"): "done" if float(item["ts"]) <= last_read else "unsupported" for item in group}

    @staticmethod
    def defaults():
        return {"enabled": True, "token_file": str(Path.home() / ".hermes/mcp-tokens/slack.json"),
                # Slack-only checks between the regular ones (seconds) without a live listener ...
                "poll_seconds": 120,
                # ... and while the Socket Mode listener is connected (it nudges on new messages).
                "listener_poll_seconds": 900,
                "app_token_command": ["security", "find-generic-password", "-s", "hermes-auto-slack-app", "-w"],
                # User token of the "Lucas Hermes Agent" app with im:write/mpim:write (and im/mpim:history), used
                # only for conversations.mark on DMs (the MCP token cannot mark read).
                "mark_token_command": ["security", "find-generic-password", "-s", "hermes-auto-slack-user", "-w"],
                # A conversation is handed over after nobody wrote for this long.
                "quiet_seconds": 90,
                # true: a conversation already read in Slack opens no session (answered ones never do).
                "skip_read": False,
                # Threads the user wrote in within this many days are followed.
                "thread_days": 14,
                # link_only: private channels give author, place and link only; "full" passes the text.
                "private_channels": "link_only",
                "skip_channels": [],
                # Channel ids whose every new top-level post goes to the sorter, mention or not.
                "watch_channels": [],
                "context_messages": 8, "max_search_pages": 5}

    def due(self, settings, store, last_poll, now):
        """A Slack-only check is due: the listener saw something new, or the polling interval elapsed."""
        settings = {**self.defaults(), **settings}
        from hermes_auto.listening import live
        state = listener_state(store)
        if float(state.get("nudge_at") or 0) > last_poll and now - float(state["nudge_at"]) >= settings["quiet_seconds"]:
            return True
        # Relax polling only once the listener has proven it receives events (subscriptions set up).
        interval = settings["listener_poll_seconds"] if live(state, now) else settings["poll_seconds"]
        # 0 = no Slack-only polling at all: the listener nudges and the regular checks are the fallback.
        return bool(interval) and now - last_poll >= interval

    def poll(self, config, store):
        """Nothing to watch by label: Slack candidates go through the sorter (`inbox`)."""

    def inbox(self, config, store, limit=40, since_ms=None):
        settings = {**self.defaults(), **config["sources"]["slack"]}
        return Slack(settings).inbox(config, store, limit=limit, since_ms=since_ms)

    def setup(self, config):
        settings = {**self.defaults(), **config["sources"]["slack"]}
        Slack(settings).me()
