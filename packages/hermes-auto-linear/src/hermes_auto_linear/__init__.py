"""Linear source for the sorter: the user's Linear inbox (assignments, comments, mentions, status changes ...).

Reads `notifications` through the `linear` CLI (`linear api`), so the API key stays in the CLI's keyring and
never passes through this process. Its only write marks a shown notification read once the session or brief
showing it is ready, or at once for skipped noise (`mark_read`); no comments, no issue changes.
Notifications about one issue (project, document) form one conversation; each notification is a candidate.
Linear has no push for a member's inbox (webhooks need a workspace admin), so the source is polled.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time

OVERLAP_SECONDS = 300
PAGE = 100
# True while notifications cut off by `limit` remain: the next check comes at once (see cli.fast_sources).
BACKLOG = "source_backlog:linear"

FIELDS = """id type createdAt readAt title subtitle url isLinearActor botActor { name } actor { displayName }
      ... on IssueNotification { issue { id identifier title url priorityLabel state { name type } team { key }
        assignee { displayName } project { name } description } comment { body user { displayName } }
        parentComment { body user { displayName } } }
      ... on ProjectNotification { project { id name } }
      ... on DocumentNotification { documentId }"""
INBOX = ("query($after: String, $since: DateTimeOrDuration) { viewer { displayName organization { urlKey } }\n"
         f"  notifications(first: {PAGE}, after: $after, orderBy: createdAt, filter: {{createdAt: {{gt: $since}}}}) {{\n"
         "    pageInfo { hasNextPage endCursor }\n"
         f"    nodes {{ {FIELDS} }} }} }}")
READ_STATE = "query($ids: [ID!]) { notifications(first: 100, filter: {id: {in: $ids}}) { nodes { id readAt } } }"
MARK_READ = ("mutation($id: String!, $at: DateTime!) { notificationUpdate(id: $id, input: {readAt: $at}) "
             "{ success } }")


def stamp(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Cli:
    """GraphQL through `linear api`; the CLI holds the key (keyring), this process never sees it."""

    def __init__(self, command):
        self.command = list(command)

    def __call__(self, document, variables=None):
        result = subprocess.run([*self.command, "api", "--variables-json", json.dumps(variables or {})],
                                input=document, capture_output=True, text=True, timeout=60)
        try:
            data = json.loads(result.stdout or "{}", strict=False)
        except json.JSONDecodeError:
            data = {}
        if result.returncode or data.get("errors") or "data" not in data:
            detail = (data.get("errors") or [{}])[0].get("message") or result.stderr.strip()[-300:]
            raise RuntimeError(f"Linear read failed (exit {result.returncode}): {detail or 'no data'}")
        return data["data"]


def text(value, limit):
    return str(value or "").strip()[:limit]


CLOSED = ("completed", "canceled")


def mine(me, node):
    return ((node.get("issue") or {}).get("assignee") or {}).get("displayName") == me


def mentions(me, node):
    body = str((node.get("comment") or {}).get("body") or "")
    return bool(me) and re.search(rf"@{re.escape(me)}\b", body, re.IGNORECASE) is not None


def noise(settings, me, node):
    """Left out before the sorter: Linear's own reminders, tickets already closed (the "Done" notice, an
    unblock of a done ticket) and, with `only_my_comments`, comments on other people's tickets that do not
    mention the user (a mention also arrives as its own notification)."""
    kind = node.get("type")
    if kind in settings.get("skip_types", []):
        return True
    state = ((node.get("issue") or {}).get("state") or {}).get("type")
    if settings.get("skip_closed") and kind in ("issueStatusChanged", "issueUnblocked") and state in CLOSED:
        return True
    return bool(settings.get("only_my_comments") and kind == "issueNewComment" and not mine(me, node)
                and not mentions(me, node))


class Linear:
    def __init__(self, settings, api=None, now=None):
        self.settings = settings
        self.api = api or Cli(settings["command"])
        self.now = now or time.time

    def notifications(self, since):
        nodes, after, viewer = [], None, {}
        while True:
            data = self.api(INBOX, {"since": iso(since), "after": after})
            viewer = data.get("viewer") or viewer
            page = data["notifications"]
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return viewer, nodes
            after = page["pageInfo"]["endCursor"]

    def inbox(self, config, store, limit=40, since_ms=None):
        now = self.now()
        cursor = None if since_ms is None else {"since": since_ms / 1000, "handed": {}}
        if cursor is None:
            # The account is only known after a read; one cursor per installation keeps the baseline cheap.
            cursor = store.get_meta("triage_cursor:linear")
            if cursor is None:
                store.set_meta(BACKLOG, False)
                return [], "triage_cursor:linear", {"since": now, "start": now, "handed": {}}
        since = float(cursor["since"])
        handed = dict(cursor.get("handed", {}))
        viewer, nodes = self.notifications(since - OVERLAP_SECONDS)
        me = viewer.get("displayName", "")
        fresh = []
        for node in nodes:
            created = stamp(node["createdAt"])
            if created <= float(cursor.get("start", 0)) or node["id"] in handed:
                continue
            if noise(self.settings, me, node):
                handed[node["id"]] = created
                continue
            fresh.append((created, node))
        fresh.sort(key=lambda pair: pair[0])
        if since_ms is None:
            store.set_meta(BACKLOG, len(fresh) > limit)
        # Notifications cut off by `limit` hold the cursor (the next read starts OVERLAP_SECONDS before it).
        new_since = min(now, fresh[limit][0]) if len(fresh) > limit else now
        fresh = fresh[:limit]
        org = (viewer.get("organization") or {}).get("urlKey", "")
        candidates = [self.candidate(me, org, node) for _, node in fresh]
        for created, node in fresh:
            handed[node["id"]] = created
        horizon = now - 86400 * 30
        handed = {k: v for k, v in handed.items() if v >= horizon}
        return candidates, "triage_cursor:linear", {"since": new_since, "start": float(cursor.get("start", 0)),
                                                   "handed": handed}

    def candidate(self, me, org, node):
        issue = node.get("issue") or {}
        comment = node.get("comment") or {}
        parent = node.get("parentComment") or {}
        project = node.get("project") or {}
        actor = (node.get("actor") or {}).get("displayName") or (node.get("botActor") or {}).get("name") or ""
        place = issue.get("identifier") or project.get("name") or node.get("title") or org
        title = issue.get("title") or node.get("title") or ""
        lines = [f"Typ: {node.get('type')}", f"Od: {actor or '?'}", f"Tytuł: {title}"]
        if node.get("subtitle"):
            lines.append(f"Powiadomienie: {text(node['subtitle'], 500)}")
        if issue:
            lines.append(f"Ticket: {issue.get('identifier')}; stan: {(issue.get('state') or {}).get('name')}; "
                         f"priorytet: {issue.get('priorityLabel')}; przypisany: "
                         f"{(issue.get('assignee') or {}).get('displayName') or '-'}; projekt: "
                         f"{(issue.get('project') or {}).get('name') or '-'}")
            if issue.get("description") and not comment:
                lines.append("Opis: " + " ".join(str(issue["description"]).split())[:1500])
        if parent:
            lines.append(f"W odpowiedzi na ({(parent.get('user') or {}).get('displayName')}): "
                         + text(parent.get("body"), 1500))
        if comment:
            lines.append(f"Komentarz ({(comment.get('user') or {}).get('displayName')}): "
                         + text(comment.get("body"), 4000))
        created = stamp(node["createdAt"])
        conversation = issue.get("id") or project.get("id") or node.get("documentId") or node["id"]
        return {
            "kind": "linear", "id": node["id"], "thread_id": conversation, "account": me,
            "read": bool(self.settings.get("skip_read") and node.get("readAt")),
            "from": actor or "Linear", "bot": bool(node.get("isLinearActor") or node.get("botActor")), "to": me,
            "subject": f"Linear: {place} {title}".strip()[:180], "place": place, "why": node.get("type", ""),
            "type": node.get("type", ""), "org": org, "mine": mine(me, node), "mentions_me": mentions(me, node), "team": (issue.get("team") or {}).get("key", ""),
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(created)),
            "body": "\n".join(lines)[:20000], "url": issue.get("url") or node.get("url") or "",
            "snippet": text(node.get("subtitle") or title, 300), "sent": [],
        }


class Source:
    @staticmethod
    def defaults():
        return {"enabled": True, "command": ["linear"],
                # Reminders and digests Linear sends to everyone: never a conversation.
                "skip_types": ["projectUpdatePrompt", "feedSummaryGenerated", "issueCommentReaction",
                               "issueReaction", "issueEmojiReaction"],
                # A ticket marked done or canceled (and an unblock of one) is not worth a word.
                "skip_closed": True,
                # Comments on other people's tickets only when they mention the user.
                "only_my_comments": True,
                # true: a notification already read in Linear opens nothing.
                "skip_read": False,
                # Linear has no push for a member's inbox: check this often (seconds; 0 = only full checks).
                "poll_seconds": 120}

    def due(self, settings, store, last_poll, now):
        settings = {**self.defaults(), **settings}
        return bool(settings["poll_seconds"]) and now - last_poll >= float(settings["poll_seconds"])

    def poll(self, config, store):
        """Nothing to watch by label: Linear candidates go through the sorter (`inbox`)."""

    def inbox(self, config, store, limit=40, since_ms=None):
        settings = {**self.defaults(), **config["sources"]["linear"]}
        return Linear(settings).inbox(config, store, limit=limit, since_ms=since_ms)

    def mark_read(self, config, items, api=None):
        """Mark each shown notification read; {item id: "done"}. Linear marks single notifications, so a
        newer one about the same issue stays unread."""
        settings = {**self.defaults(), **config["sources"]["linear"]}
        api = api or Cli(settings["command"])
        ids = [str(item["id"]) for item in items]
        nodes = api(READ_STATE, {"ids": ids})["notifications"]["nodes"] if ids else []
        unread = {node["id"] for node in nodes if not node.get("readAt")}
        at = iso(time.time())
        for notification_id in ids:
            if notification_id in unread:
                api(MARK_READ, {"id": notification_id, "at": at})
        return {item["id"]: "done" for item in items}

    def setup(self, config):
        Cli({**self.defaults(), **config["sources"]["linear"]}["command"])("query { viewer { id } }")
