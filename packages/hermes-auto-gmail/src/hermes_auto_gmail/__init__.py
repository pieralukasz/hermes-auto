from __future__ import annotations

import base64
import time
from pathlib import Path
from email.utils import getaddresses, parseaddr

from hermes_auto.streams import deactivate_missing, observe_stream

# Messages stamped this far before the cursor are re-read and de-duplicated by id.
OVERLAP_MS = 120_000


def text_parts(payload):
    text = []
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        text.append(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace"))
    for part in payload.get("parts", []):
        text.extend(text_parts(part))
    return text


class Gmail:
    def __init__(self, config):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.credentials import Credentials
        self.http = AuthorizedSession(Credentials.from_authorized_user_file(
            str(Path(config["token_file"]).expanduser())))

    def get(self, path, **params):
        response = self.http.get(f"https://gmail.googleapis.com/gmail/v1/users/me/{path}",
                                 params=params, timeout=45)
        if not response.ok:
            raise RuntimeError(f"Gmail read failed ({response.status_code}); check OAuth access")
        return response.json()

    def thread_ids(self, label):
        threads = []
        cursor = None
        while True:
            params = {"labelIds": label, "maxResults": 100}
            if cursor:
                params["pageToken"] = cursor
            page = self.get("threads", **params)
            threads.extend(item["id"] for item in page.get("threads", []))
            cursor = page.get("nextPageToken")
            if not cursor:
                return threads

    def inbox(self, config, store, limit=40, since_ms=None):
        """New inbox messages since the sorter's cursor: (candidates, cursor key, new cursor).

        The first call only records "now" as the baseline. Messages the user sent are left out. For
        each candidate the thread's earlier sent messages are listed, so a reply can go back to the
        Hermes session that wrote it. `since_ms` overrides the cursor for a read-only preview.
        """
        account = self.get("profile")["emailAddress"]
        own = {address.casefold() for address in [account, *config.get("own_addresses", [])]}
        key = f"triage_cursor:gmail:{account}"
        cursor = since_ms if since_ms is not None else store.get_meta(key)
        if cursor is None:
            return [], key, {"ms": int(time.time() * 1000), "seen": {}}
        if isinstance(cursor, dict):
            # The cursor keeps the ids handed over in the overlap window, so a message stamped at or
            # slightly before the last hand-over is still read once and only once.
            mark, seen = int(cursor["ms"]), {str(k): int(v) for k, v in cursor.get("seen", {}).items()}
            floor = mark - OVERLAP_MS
        else:
            # A plain timestamp (preview window or a cursor from an older version): read from it on.
            mark, seen = int(cursor), {}
            floor = mark
        # Read every page: the oldest messages come last and must not be cut off.
        ids = self.list_ids(f"in:inbox after:{floor // 1000 - 1}")
        fresh = []
        for message_id in ids:
            if message_id in seen:
                continue
            stamp = int(self.get(f"messages/{message_id}", format="minimal").get("internalDate") or 0)
            if stamp >= floor:
                fresh.append((stamp, message_id))
        fresh.sort()
        # Hand over at most `limit` messages, oldest first; the rest wait for the next run.
        fresh = fresh[:limit]
        candidates = []
        for _, message_id in fresh:
            item = self.candidate(account, self.get(f"messages/{message_id}", format="full"), own)
            if item:
                candidates.append(item)
        if not fresh:
            return candidates, key, cursor
        handed = {**seen, **{message_id: stamp for stamp, message_id in fresh}}
        mark = max(mark, fresh[-1][0])
        window = {message_id: stamp for message_id, stamp in handed.items() if stamp >= mark - OVERLAP_MS}
        return candidates, key, {"ms": mark, "seen": window}

    def list_ids(self, query):
        """All message ids matching `query`, across every page; a repeated page token is an error."""
        ids, page_token, tokens = {}, None, set()
        while True:
            params = {"q": query, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            page = self.get("messages", **params)
            ids.update(dict.fromkeys(item["id"] for item in page.get("messages", [])))
            page_token = page.get("nextPageToken")
            if not page_token:
                return list(ids)
            if page_token in tokens:
                raise RuntimeError("Gmail returned a repeated page token; cursor was not advanced")
            tokens.add(page_token)

    def candidate(self, account, message, own):
        labels = set(message.get("labelIds", []))
        if labels & {"SENT", "DRAFT", "SPAM", "TRASH"}:
            return None
        payload = message.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        sender = parseaddr(headers.get("from", ""))[1].casefold()
        if not sender or sender in own:
            return None
        thread_id = message.get("threadId", "")
        sent = []
        thread = self.get(f"threads/{thread_id}", format="metadata", metadataHeaders=["To", "Cc"])
        for item in thread.get("messages", []):
            if "SENT" not in item.get("labelIds", []):
                continue
            if int(item.get("internalDate") or 0) >= int(message.get("internalDate") or 0):
                continue
            item_headers = {h["name"].lower(): h["value"] for h in item.get("payload", {}).get("headers", [])}
            to = [address for _, address in getaddresses([item_headers.get("to", ""), item_headers.get("cc", "")])
                  if address]
            sent.append({"ts": int(item["internalDate"]) / 1000, "to": to})
        return {
            "id": message["id"], "thread_id": thread_id, "account": account, "read": "UNREAD" not in labels,
            "from": headers.get("from", ""), "to": headers.get("to", ""), "subject": headers.get("subject", ""),
            "date": headers.get("date", ""), "list_unsubscribe": bool(headers.get("list-unsubscribe")),
            "snippet": message.get("snippet", ""),
            "body": ("\n".join(text_parts(payload)) or message.get("snippet", ""))[:20000],
            "url": f"https://mail.google.com/mail/u/{account}/#all/{thread_id}", "sent": sent,
        }

    def poll(self, config, store):
        account = self.get("profile")["emailAddress"]
        own = [account, *config.get("own_addresses", [])]
        labels = {item["name"]: item["id"] for item in self.get("labels").get("labels", [])}
        if config["label"] not in labels:
            raise RuntimeError(f"Create Gmail label {config['label']!r} and apply it to threads to watch")
        # The agent label is optional; a thread carrying it gets full tools, otherwise the watch mode.
        modes = {}
        for name, mode in ((config["label"], config.get("mode", "draft")),
                           (config.get("agent_label", ""), "agent")):
            if name not in labels:
                continue
            for thread_id in self.thread_ids(labels[name]):
                modes[thread_id] = mode
        threads = list(modes)
        for thread_id in threads:
            thread = self.get(f"threads/{thread_id}", format="full")
            messages = []
            for position, message in enumerate(thread.get("messages", [])):
                payload = message.get("payload", {})
                headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
                messages.append({
                    "id": message["id"], "from": headers.get("from", ""),
                    "subject": headers.get("subject", ""), "date": headers.get("date", ""),
                    "outgoing": bool(set(message.get("labelIds", [])) & {"SENT", "DRAFT"}),
                    "is_reply": bool(headers.get("in-reply-to") or headers.get("references") or position),
                    "body": "\n".join(text_parts(payload))[:20000] or message.get("snippet", ""),
                    "url": f"https://mail.google.com/mail/u/{account}/#all/{thread_id}",
                })
            own_lower = {address.casefold() for address in own}
            events = []
            for message in messages:
                sender = parseaddr(message["from"])[1].casefold()
                events.append({"id": message["id"], "title": message["subject"] or "Email reply",
                               "payload": message, "eligible": bool(sender) and sender not in own_lower
                               and not message["outgoing"] and message["is_reply"]})
            observe_stream(store, source="gmail", account=account, stream_id=thread_id,
                           events=events, mode=modes[thread_id])
        deactivate_missing(store, f"gmail:{account}", set(threads))


class Source:
    @staticmethod
    def defaults():
        return {"enabled": True, "label": "Hermes/Watch", "agent_label": "Hermes/Agent", "mode": "draft",
                "token_file": str(Path.home() / ".hermes/google_token.json"), "own_addresses": []}

    def poll(self, config, store):
        settings = config["sources"]["gmail"]
        Gmail(settings).poll(settings, store)

    def inbox(self, config, store, limit=40, since_ms=None):
        settings = config["sources"]["gmail"]
        return Gmail(settings).inbox(settings, store, limit=limit, since_ms=since_ms)

    def setup(self, config):
        settings = config["sources"]["gmail"]
        gmail = Gmail(settings)
        existing = {label["name"] for label in gmail.get("labels").get("labels", [])}
        for name in (settings["label"], settings.get("agent_label", "")):
            if not name or name in existing:
                continue
            response = gmail.http.post("https://gmail.googleapis.com/gmail/v1/users/me/labels",
                                       json={"name": name, "labelListVisibility": "labelShow",
                                             "messageListVisibility": "show"}, timeout=45)
            if not response.ok:
                raise RuntimeError(f"Could not create Gmail label ({response.status_code}); create it in Gmail")
