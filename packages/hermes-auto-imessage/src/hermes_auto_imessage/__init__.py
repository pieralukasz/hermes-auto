"""iMessage/SMS source for the sorter: new messages from other people on this Mac.

Reads Messages' own database (`~/Library/Messages/chat.db`) directly, opened read-only (SQLite `mode=ro`), so
nothing here can send, mark read or change a chat. The process needs macOS Full Disk Access (the database is
TCC-protected). Every new message from someone else (direct chats and groups) is sorted; the user's own messages
only count as an answer. Messages of one chat are handed over as one burst once nobody has written for
`quiet_seconds`; the chat is the conversation, so later bursts follow into the same session (as in Slack).
Messages has no push, so the source is polled (`poll_seconds`); one local query per check.
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
import time

# Messages stores dates as nanoseconds (older macOS: seconds) since 2001-01-01.
APPLE_EPOCH = 978307200
GROUP_STYLE = 43
# True while bursts wait for quiet or were cut off by `limit`: the next check comes at once (cli.fast_sources).
BACKLOG = "source_backlog:imessage"
CURSOR = "triage_cursor:imessage:local"
ACCOUNT = "local"

MESSAGE_COLUMNS = ("m.ROWID AS rowid, m.text, m.attributedBody, m.handle_id, m.service, m.date, m.is_from_me, m.is_read, "
                   "m.associated_message_type, m.item_type, m.cache_has_attachments, h.id AS handle, "
                   "c.ROWID AS chat_id, c.guid AS chat_guid, c.style, c.display_name, c.chat_identifier")
MESSAGE_FROM = ("FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID JOIN chat c ON c.ROWID = j.chat_id "
                "LEFT JOIN handle h ON h.ROWID = m.handle_id")


class MessagesError(RuntimeError):
    pass


def unix(value):
    value = int(value or 0)
    return APPLE_EPOCH + (value / 1_000_000_000 if value > 10_000_000_000 else value)


def attributed_text(blob):
    """The plain text of an `attributedBody` typedstream (newer macOS leaves `text` NULL): the NSString's bytes
    after its `+` marker, with typedstream's length prefix (one byte, or 0x81/0x82 + 2/4 bytes little endian)."""
    data = bytes(blob or b"")
    start = data.find(b"NSString")
    marker = data.find(b"+", start + 8) if start >= 0 else -1
    if marker < 0 or marker + 1 >= len(data):
        return ""
    size, at = data[marker + 1], marker + 2
    if size in (0x81, 0x82):
        width = 2 if size == 0x81 else 4
        size, at = int.from_bytes(data[at:at + width], "little"), at + width
    return data[at:at + size].decode("utf-8", "replace")


def message_text(message):
    return str(message.get("text") or attributed_text(message.get("attributedBody")) or "").strip()


DIGITS = re.compile(r"\D")


def phone_key(value):
    """The last nine digits: enough to match +48 500 100 200 with 500100200 across formats."""
    digits = DIGITS.sub("", str(value or ""))
    return digits[-9:] if len(digits) >= 7 else None


def contact_names(directory):
    """{phone key or lowercased email: display name} from the Contacts databases; {} when unreadable."""
    names = {}
    directory = os.path.expanduser(str(directory))
    paths = glob.glob(os.path.join(directory, "Sources", "*", "AddressBook-v22.abcddb"))
    paths += glob.glob(os.path.join(directory, "AddressBook-v22.abcddb"))
    for path in paths:
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                people = {pk: " ".join(filter(None, [first, last])) or nick or org or ""
                          for pk, first, last, org, nick in db.execute(
                              "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME FROM ZABCDRECORD")}
                for owner, number in db.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                    if people.get(owner) and phone_key(number):
                        names.setdefault(phone_key(number), people[owner].strip())
                for owner, address in db.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                    if people.get(owner) and address:
                        names.setdefault(str(address).strip().lower(), people[owner].strip())
            finally:
                db.close()
        except sqlite3.Error:
            continue  # no permission or another schema: handles instead of names
    return names


def connect(path):
    """chat.db opened read-only; a clear error when macOS denies access (no Full Disk Access)."""
    path = os.path.expanduser(str(path))
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("SELECT 1 FROM message LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        raise MessagesError(f"Cannot read {path} ({exc}). Give the process running hermes-auto Full Disk Access "
                            "(System Settings > Privacy & Security > Full Disk Access).") from exc
    return db


class Messages:
    def __init__(self, settings, now=None):
        self.settings = settings
        self.now = now or time.time
        self._db = None
        self._names = None

    @property
    def db(self):
        if self._db is None:
            self._db = connect(self.settings["db"])
        return self._db

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def last_rowid(self):
        return int(self.db.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])

    def after(self, rowid, limit):
        return [dict(row) for row in self.db.execute(
            f"SELECT {MESSAGE_COLUMNS} {MESSAGE_FROM} WHERE m.ROWID > ? ORDER BY m.ROWID LIMIT ?",
            (rowid, limit))]

    def chat_messages(self, chat_id, first, last, context):
        before = [dict(row) for row in self.db.execute(
            f"SELECT {MESSAGE_COLUMNS} {MESSAGE_FROM} WHERE j.chat_id = ? AND m.ROWID < ? "
            "ORDER BY m.ROWID DESC LIMIT ?", (chat_id, first, context))]
        span = [dict(row) for row in self.db.execute(
            f"SELECT {MESSAGE_COLUMNS} {MESSAGE_FROM} WHERE j.chat_id = ? AND m.ROWID BETWEEN ? AND ? "
            "ORDER BY m.ROWID", (chat_id, first, last))]
        return list(reversed(before)) + span

    def answered_after(self, chat_id, rowid):
        return bool(self.db.execute(
            "SELECT 1 FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID WHERE j.chat_id = ? "
            "AND m.ROWID > ? AND m.is_from_me = 1 LIMIT 1", (chat_id, rowid)).fetchone())

    # ── Candidates ──────────────────────────────────────────────────────

    def participants(self, chat_id):
        return [row[0] for row in self.db.execute(
            "SELECT h.id FROM chat_handle_join x JOIN handle h ON h.ROWID = x.handle_id WHERE x.chat_id = ? "
            "ORDER BY h.ROWID", (chat_id,))]

    def relevant(self, message, now):
        """A message from someone else with something in it: no reaction (tapback), no group event (rename,
        member added), nothing older than `max_age_hours` (iCloud may sync old messages in with new rowids)."""
        if message["is_from_me"] or message.get("associated_message_type") or message.get("item_type"):
            return False
        if now - unix(message["date"]) > float(self.settings["max_age_hours"]) * 3600:
            return False
        return bool(message_text(message) or message.get("cache_has_attachments"))

    def inbox(self, config, store, limit=40, since_ms=None):
        """Bursts from other people since the cursor: (candidates, cursor key, new cursor)."""
        now = self.now()
        cursor = store.get_meta(CURSOR) if since_ms is None else {"rowid": 0, "start": 0, "convs": {}}
        if cursor is None:
            # First check: the baseline. Nothing older than this ever reaches the sorter.
            store.set_meta(BACKLOG, False)
            last = self.last_rowid()
            return [], CURSOR, {"rowid": last, "start": last, "convs": {}}
        rowid = int(cursor["rowid"])
        handed = {key: int(value) for key, value in cursor.get("convs", {}).items()}
        scan = int(self.settings["max_scan"])
        rows = self.after(rowid, scan)
        end = rows[-1]["rowid"] if rows else rowid
        quiet = float(self.settings["quiet_seconds"])
        skip = set(self.settings.get("skip_chats") or [])
        by_chat = {}
        for message in rows:
            if message["rowid"] <= handed.get(message["chat_guid"], 0) or not self.relevant(message, now) \
                    or message["chat_guid"] in skip:
                continue
            by_chat.setdefault(message["chat_guid"], []).append(message)
        candidates, waiting = [], []
        for guid, fresh in by_chat.items():
            last_at = unix(fresh[-1]["date"])
            if now - last_at < quiet:
                waiting.append(fresh[0]["rowid"])
                continue
            candidates.append((last_at, guid, fresh))
        candidates.sort(key=lambda entry: entry[0])
        waiting += [fresh[0]["rowid"] for _, _, fresh in candidates[limit:]]
        candidates = candidates[:limit]
        items = []
        for _, guid, fresh in candidates:
            items.append(self.candidate(fresh))
            handed[guid] = fresh[-1]["rowid"]
        mark = min(waiting) - 1 if waiting else end
        handed = {key: value for key, value in handed.items() if value > mark}
        if since_ms is None:
            store.set_meta(BACKLOG, bool(waiting) or len(rows) >= scan)
        return items, CURSOR, {"rowid": mark, "start": int(cursor.get("start", 0)), "convs": handed}

    def candidate(self, fresh):
        first, last = fresh[0], fresh[-1]
        chat_id = last["chat_id"]
        context = self.chat_messages(chat_id, first["rowid"], last["rowid"], int(self.settings["context_messages"]))
        senders = []
        for message in fresh:
            name = self.sender(message)
            if name not in senders:
                senders.append(name)
        sender = ", ".join(senders)
        last_at = unix(last["date"])
        fresh_ids = {message["rowid"] for message in fresh}
        group = last.get("style") == GROUP_STYLE
        if group:
            name = str(last.get("display_name") or "").strip()
            place = f"grupa „{name}”" if name else "grupa: " + ", ".join(
                self.name(handle) for handle in self.participants(chat_id))
        else:
            place = "DM"
        channel = "SMS" if str(last.get("service") or "").upper() in ("SMS", "RCS") else "iMessage"
        # Read on the phone: only with `skip_read` (as in Slack, reading a notification does not settle it).
        seen = bool(self.settings.get("skip_read")) and all(message.get("is_read") for message in fresh)
        return {
            "kind": "imessage", "id": f"{last['chat_guid']}@{last['rowid']}", "thread_id": last["chat_guid"],
            "account": ACCOUNT, "read": seen or self.answered_after(chat_id, last["rowid"]),
            "from": sender, "to": "Łukasz", "subject": f"iMessage: {sender}", "why": "group" if group else "dm",
            "channel": channel, "place": place, "chat_id": chat_id, "dm": not group, "group": group,
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(last_at)),
            "snippet": " ".join(message_text(last).split())[:300],
            "body": self.transcript(context, fresh_ids)[:20000], "url": "", "sent": [],
            "rowids": sorted(fresh_ids),
        }

    def name(self, handle):
        if not handle:
            return "?"
        if self._names is None:
            self._names = contact_names(self.settings["contacts_dir"]) if self.settings.get("contacts") else {}
        key = str(handle).strip().lower() if "@" in str(handle) else phone_key(handle)
        return self._names.get(key) or handle

    def sender(self, message):
        return self.name(message.get("handle"))

    def transcript(self, messages, fresh_ids):
        lines = []
        for message in messages:
            who = "Łukasz" if message["is_from_me"] else self.sender(message)
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(unix(message["date"])))
            mark = "NOWE " if message["rowid"] in fresh_ids else ""
            text = " ".join(filter(None, [message_text(message),
                                          "[załącznik]" if message.get("cache_has_attachments") else ""]))
            lines.append(f"{mark}[{stamp}] {who}: {text}")
        return "\n".join(lines)


class Source:
    @staticmethod
    def defaults():
        return {"enabled": False, "db": "~/Library/Messages/chat.db",
                # A chat is handed over after nobody wrote for this long (seconds).
                "quiet_seconds": 90,
                # Messages has no push: check this often (seconds; 0 = only full checks). One local query.
                "poll_seconds": 60,
                # Messages older than this are never new (iCloud sync, a restored backup).
                "max_age_hours": 24,
                # Sender names from Contacts (read-only, same Full Disk Access); handles when unreadable.
                "contacts": True,
                "contacts_dir": "~/Library/Application Support/AddressBook",
                # Chat guids (e.g. "SMS;-;+48500100200") that never reach the sorter.
                "skip_chats": [],
                # true: a burst already read on the phone (every message `is_read`) opens nothing.
                "skip_read": False,
                "context_messages": 8, "max_scan": 2000}

    def due(self, settings, store, last_poll, now):
        settings = {**self.defaults(), **settings}
        return bool(settings["poll_seconds"]) and now - last_poll >= float(settings["poll_seconds"])

    def poll(self, config, store):
        """Nothing to watch by label: messages go through the sorter (`inbox`)."""

    def inbox(self, config, store, limit=40, since_ms=None):
        source = Messages({**self.defaults(), **config["sources"]["imessage"]})
        try:
            return source.inbox(config, store, limit=limit, since_ms=since_ms)
        finally:
            source.close()

    def setup(self, config):
        source = Messages({**self.defaults(), **config["sources"]["imessage"]})
        try:
            source.last_rowid()
        finally:
            source.close()
