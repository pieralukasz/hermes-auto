"""Proton Bridge adapter. Polling uses read-only IMAP and BODY.PEEK."""
from __future__ import annotations

import datetime as dt
import email.policy
import imaplib
import re
import ssl
import subprocess
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path

from hermes_auto.streams import deactivate_missing, observe_stream


def message_ids(value):
    return re.findall(r"<[^<>\s]+>", str(value or ""))


def root_id(message):
    refs = message_ids(message.get("References"))
    parents = message_ids(message.get("In-Reply-To"))
    own = message_ids(message.get("Message-ID"))
    return (refs or parents or own or [None])[0]


def parse_candidate(raw, own_addresses):
    """An INBOX message as a sorter candidate, or None for the user's own or unidentifiable mail."""
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    ids = message_ids(message.get("Message-ID"))
    sender = parseaddr(str(message.get("From", "")))[1].casefold()
    if not ids or not sender or sender in own_addresses:
        return None
    try:
        body = message.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        if body is not None and body.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
    except (KeyError, LookupError, ValueError):
        text = ""
    refs = message_ids(message.get("References")) + message_ids(message.get("In-Reply-To"))
    return {"id": ids[0], "thread_id": root_id(message) or ids[0], "from": str(message.get("From", "")),
            "to": str(message.get("To", "")), "subject": str(message.get("Subject", "")),
            "date": str(message.get("Date", "")), "list_unsubscribe": bool(message.get("List-Unsubscribe")),
            "body": " ".join(str(text).split())[:20000], "refs": list(dict.fromkeys(refs)), "sent": []}


def parse_event(raw, own_addresses):
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    ids = message_ids(message.get("Message-ID"))
    if not ids:
        return None
    sender = parseaddr(str(message.get("From", "")))[1].casefold()
    body = message.get_body(preferencelist=("plain",)) if message.is_multipart() else message
    text = body.get_content() if body and body.get_content_type() == "text/plain" else ""
    reply = bool(message_ids(message.get("In-Reply-To")) or message_ids(message.get("References")))
    payload = {"id": ids[0], "from": str(message.get("From", "")),
               "subject": str(message.get("Subject", "Email reply")), "date": str(message.get("Date", "")),
               "body": str(text)[:20000]}
    return {"id": ids[0], "title": payload["subject"], "payload": payload,
            "eligible": bool(sender) and sender not in {a.casefold() for a in own_addresses} and reply}


class Proton:
    def __init__(self, settings):
        if settings["host"] not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Proton Bridge must be local")
        if not settings.get("username") or not settings.get("password_command"):
            raise ValueError("Configure Proton username and password_command (Bridge credentials)")
        context = ssl.create_default_context(cafile=str(Path(settings["certificate"]).expanduser()))
        # Trust the explicit Bridge certificate, whose CN may not be the loopback address.
        context.check_hostname = False
        result = subprocess.run(settings["password_command"], capture_output=True, text=True,
                                timeout=15, check=True)
        self.client = imaplib.IMAP4(settings["host"], settings["port"], timeout=30)
        self.client.starttls(ssl_context=context)
        self.client.login(settings["username"], result.stdout.strip())

    def select(self, mailbox):
        kind, _ = self.client.select('"' + mailbox.replace('"', '\\"') + '"', readonly=True)
        if kind != "OK":
            raise RuntimeError(f"Proton mailbox {mailbox!r} is unavailable; check Bridge and labels")

    def search(self, *criteria):
        kind, data = self.client.uid("search", None, *criteria)
        if kind != "OK":
            raise RuntimeError("Proton search failed; baseline was not advanced")
        return data[0].split() if data and data[0] else []

    def fetch(self, uid, headers=False):
        part = "BODY.PEEK[HEADER]" if headers else "BODY.PEEK[]<0.262144>"
        kind, data = self.client.uid("fetch", uid, f"({part})")
        if kind != "OK":
            raise RuntimeError("Proton fetch failed; baseline was not advanced")
        for item in data:
            if isinstance(item, tuple):
                return item[1]
        raise RuntimeError("Proton message vanished while reading; retry the poll")

    def inbox_status(self):
        kind, data = self.client.status("INBOX", "(UIDNEXT UIDVALIDITY)")
        if kind != "OK" or not data:
            raise RuntimeError("Proton INBOX status failed; cursor was not advanced")
        text = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
        values = dict(re.findall(r"(UIDNEXT|UIDVALIDITY) (\d+)", text))
        return int(values["UIDVALIDITY"]), int(values["UIDNEXT"])

    def inbox(self, config, store, limit=40, since_ms=None):
        """New INBOX messages since the sorter's UID cursor: (candidates, cursor key, new cursor).

        The first call (or a changed UIDVALIDITY) only records the baseline. For each candidate the
        Sent folder is checked for the messages it replies to, so the reply can return to the Hermes
        session that wrote them. `since_ms` reads a date window instead, for a read-only preview.
        """
        account = config["username"]
        own = {address.casefold() for address in [account, *config.get("own_addresses", [])]}
        key = f"triage_cursor:proton:{account}"
        validity, uid_next = self.inbox_status()
        cursor = store.get_meta(key)
        self.select("INBOX")
        if since_ms is not None:
            since = dt.datetime.fromtimestamp(since_ms / 1000).strftime("%d-%b-%Y")
            uids = [int(uid) for uid in self.search("SINCE", since)]
        elif cursor is None or cursor.get("validity") != validity:
            return [], key, {"validity": validity, "uid": uid_next - 1}
        else:
            uids = [int(uid) for uid in self.search("UID", f"{cursor['uid'] + 1}:*") if int(uid) > cursor["uid"]]
        uids = sorted(uids)[:limit]
        candidates = []
        for uid in uids:
            item = parse_candidate(self.fetch(str(uid).encode()), own)
            if item:
                item["account"] = account
                candidates.append(item)
        if any(item["refs"] for item in candidates):
            self.select(config.get("sent_mailbox", "Sent"))
            for item in candidates:
                for ref in item["refs"][-3:]:
                    quoted = '"' + ref.replace('\\', '\\\\').replace('"', '\\"') + '"'
                    for uid in self.search("HEADER", "Message-ID", quoted):
                        sent = BytesParser(policy=email.policy.default).parsebytes(self.fetch(uid, headers=True))
                        try:
                            stamp = parsedate_to_datetime(str(sent.get("Date"))).timestamp()
                        except (TypeError, ValueError):
                            continue
                        to = [address for _, address in getaddresses([str(sent.get("To", "")),
                                                                      str(sent.get("Cc", ""))]) if address]
                        item["sent"].append({"ts": stamp, "to": to})
        new_cursor = {"validity": validity, "uid": uids[-1] if uids else (cursor or {}).get("uid", uid_next - 1)}
        return candidates, key, new_cursor

    def poll(self, config, store):
        account = config["username"]
        own = [account, *config.get("own_addresses", [])]
        # The agent mailbox is optional; a thread found there gets full tools, otherwise the watch mode.
        modes = {}
        for mailbox, mode, required in ((config["watch_mailbox"], config.get("mode", "draft"), True),
                                        (config.get("agent_mailbox", ""), "agent", False)):
            if not mailbox:
                continue
            try:
                self.select(mailbox)
            except RuntimeError:
                if required:
                    raise
                continue
            for uid in self.search("ALL"):
                message = BytesParser(policy=email.policy.default).parsebytes(self.fetch(uid, headers=True))
                root = root_id(message)
                if root:
                    modes[root] = mode
        roots = set(modes)
        self.select(config["all_mailbox"])
        for root in sorted(roots):
            # RFC IDs are data, escaped as an IMAP quoted string, never commands.
            quoted = '"' + root.replace('\\', '\\\\').replace('"', '\\"') + '"'
            uids = self.search("OR", "HEADER", "References", quoted, "HEADER", "In-Reply-To", quoted)
            events = []
            for uid in uids:
                event = parse_event(self.fetch(uid), own)
                if event:
                    events.append(event)
            observe_stream(store, source="proton", account=account, stream_id=root,
                           events=events, mode=modes[root])
        deactivate_missing(store, f"proton:{account}", roots)

    def close(self):
        try:
            self.client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


class Source:
    @staticmethod
    def defaults():
        return {"enabled": True, "host": "127.0.0.1", "port": 1143, "username": "",
                "certificate": "", "password_command": [], "own_addresses": [],
                "watch_mailbox": "Labels/Hermes Watch", "agent_mailbox": "Labels/Hermes Agent",
                "all_mailbox": "All Mail", "mode": "draft"}

    def poll(self, config, store):
        settings = config["sources"]["proton"]
        client = Proton(settings)
        try:
            client.poll(settings, store)
        finally:
            client.close()

    def inbox(self, config, store, limit=40, since_ms=None):
        settings = config["sources"]["proton"]
        client = Proton(settings)
        try:
            return client.inbox(settings, store, limit=limit, since_ms=since_ms)
        finally:
            client.close()

    def setup(self, config):
        settings = config["sources"]["proton"]
        client = Proton(settings)
        try:
            for name in (settings["watch_mailbox"], settings.get("agent_mailbox", "")):
                if not name:
                    continue
                try:
                    client.select(name)
                    continue
                except RuntimeError:
                    pass
                mailbox = '"' + name.replace('"', '\\"') + '"'
                kind, _ = client.client.create(mailbox)
                if kind != "OK":
                    raise RuntimeError(f"Create the {name.split('/')[-1]} label in Proton Mail")
        finally:
            client.close()
