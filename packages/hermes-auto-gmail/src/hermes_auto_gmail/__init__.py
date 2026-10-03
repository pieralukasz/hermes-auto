from __future__ import annotations

import base64
from pathlib import Path
from email.utils import parseaddr

from hermes_auto.streams import deactivate_missing, observe_stream


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
