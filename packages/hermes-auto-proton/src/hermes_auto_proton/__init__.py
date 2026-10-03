"""Proton Bridge adapter. Polling uses read-only IMAP and BODY.PEEK."""
from __future__ import annotations

import email.policy
import imaplib
import re
import ssl
import subprocess
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from hermes_auto.streams import deactivate_missing, observe_stream


def message_ids(value):
    return re.findall(r"<[^<>\s]+>", str(value or ""))


def root_id(message):
    refs = message_ids(message.get("References"))
    parents = message_ids(message.get("In-Reply-To"))
    own = message_ids(message.get("Message-ID"))
    return (refs or parents or own or [None])[0]


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

    def poll(self, config, store):
        account = config["username"]
        own = [account, *config.get("own_addresses", [])]
        self.select(config["watch_mailbox"])
        roots = set()
        for uid in self.search("ALL"):
            message = BytesParser(policy=email.policy.default).parsebytes(self.fetch(uid, headers=True))
            root = root_id(message)
            if root:
                roots.add(root)
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
                           events=events, mode=config.get("mode", "draft"))
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
                "watch_mailbox": "Labels/Hermes Watch", "all_mailbox": "All Mail", "mode": "draft"}

    def poll(self, config, store):
        settings = config["sources"]["proton"]
        client = Proton(settings)
        try:
            client.poll(settings, store)
        finally:
            client.close()

    def setup(self, config):
        settings = config["sources"]["proton"]
        client = Proton(settings)
        try:
            try:
                client.select(settings["watch_mailbox"])
                return
            except RuntimeError:
                pass
            mailbox = '"' + settings["watch_mailbox"].replace('"', '\\"') + '"'
            kind, _ = client.client.create(mailbox)
            if kind != "OK":
                raise RuntimeError("Create the Hermes Watch label in Proton Mail")
        finally:
            client.close()
