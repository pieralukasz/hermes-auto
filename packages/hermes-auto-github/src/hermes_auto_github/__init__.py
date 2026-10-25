"""GitHub source for the sorter: the user's notifications (review requests, mentions, comments, CI ...).

Reads `GET /notifications` with the user's `gh` token and never writes: no comments, no reviews, and no
marking read (GitHub can only mark a whole thread read, which could hide an update nobody has shown yet).
Each notification thread is one conversation; a new update in it is a new candidate.
Notifications the user already read on GitHub are recorded as read, so the sorter opens nothing. Watched
repositories (`reason: subscribed`) are left out unless configured otherwise.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import time

API = "https://api.github.com"
OVERLAP_SECONDS = 300
# True while notifications cut off by `limit` remain: push mode polls again soon (see cli.fast_sources).
BACKLOG = "source_backlog:github"


def gh_token(command):
    result = subprocess.run(list(command), capture_output=True, text=True, timeout=30)
    token = result.stdout.strip()
    if result.returncode or not token:
        raise RuntimeError("No GitHub token; log in with 'gh auth login'")
    return token


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(value):
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def html_url(api_url):
    """https://api.github.com/repos/o/r/pulls/1 -> https://github.com/o/r/pull/1 (issues, commits alike)."""
    match = re.match(r"https://api\.github\.com/repos/([^/]+/[^/]+)/(pulls|issues|commits)/([^/?#]+)$",
                     str(api_url or ""))
    if not match:
        return ""
    kind = {"pulls": "pull", "issues": "issues", "commits": "commit"}[match.group(2)]
    return f"https://github.com/{match.group(1)}/{kind}/{match.group(3)}"


class Http:
    def __init__(self, token):
        import requests
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})

    def get(self, url, **params):
        response = self.session.get(url if url.startswith("http") else API + url, params=params, timeout=45)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise RuntimeError(f"GitHub read failed ({response.status_code}) for {url.split('?')[0]}")
        return response.json()


class GitHub:
    def __init__(self, settings, http=None, now=None):
        self.settings = settings
        self.http = http or Http(gh_token(settings["token_command"]))
        self.now = now or time.time

    def notifications(self, since):
        items, page = [], 1
        while True:
            batch = self.http.get("/notifications", all="true", since=iso(since), per_page=50, page=page) or []
            items += batch
            if len(batch) < 50:
                break
            page += 1
        return items

    def inbox(self, config, store, limit=40, since_ms=None):
        me = (self.http.get("/user") or {}).get("login", "")
        key = f"triage_cursor:github:{me}"
        cursor = store.get_meta(key) if since_ms is None else {"since": since_ms / 1000, "handed": {}}
        now = self.now()
        if cursor is None:
            if since_ms is None:
                store.set_meta(BACKLOG, False)
            return [], key, {"since": now, "start": now, "handed": {}}
        since = float(cursor["since"])
        handed = dict(cursor.get("handed", {}))
        skip = set(self.settings.get("skip_reasons", []))
        skip_repos = set(self.settings.get("skip_repos", []))
        fresh = []
        for item in self.notifications(since - OVERLAP_SECONDS):
            updated = stamp(item["updated_at"])
            if updated <= max(float(cursor.get("start", 0)), float(handed.get(item["id"], 0))):
                continue
            if item.get("reason") in skip or item["repository"]["full_name"] in skip_repos:
                handed[item["id"]] = updated
                continue
            fresh.append((updated, item))
        fresh.sort(key=lambda pair: pair[0])
        if since_ms is None:
            store.set_meta(BACKLOG, len(fresh) > limit)
        # Notifications cut off by `limit` hold the cursor (the next read starts OVERLAP_SECONDS before it).
        new_since = min(now, fresh[limit][0]) if len(fresh) > limit else now
        fresh = fresh[:limit]
        candidates = [self.candidate(me, item) for _, item in fresh]
        for updated, item in fresh:
            handed[item["id"]] = updated
        horizon = now - 86400 * 30
        handed = {k: v for k, v in handed.items() if v >= horizon}
        return candidates, key, {"since": new_since, "start": float(cursor.get("start", 0)), "handed": handed}

    def candidate(self, me, item):
        subject = item.get("subject", {})
        repo = item["repository"]["full_name"]
        details = self.http.get(subject["url"]) if subject.get("url") else None
        comment = self.http.get(subject["latest_comment_url"]) if subject.get("latest_comment_url") and \
            subject.get("latest_comment_url") != subject.get("url") else None
        url = (comment or {}).get("html_url") or (details or {}).get("html_url") or html_url(subject.get("url")) \
            or f"https://github.com/{repo}"
        actor = ((comment or details or {}).get("user") or {})
        lines = [f"Repo: {repo}", f"Typ: {subject.get('type')}", f"Powód: {item.get('reason')}",
                 f"Tytuł: {subject.get('title')}"]
        if details:
            state = details.get("state")
            if details.get("merged"):
                state = "merged"
            lines.append(f"Stan: {state}; autor: {(details.get('user') or {}).get('login')}"
                         + ("; draft" if details.get("draft") else ""))
            if details.get("body") and not comment:
                lines.append("Opis: " + " ".join(str(details["body"]).split())[:1500])
        if comment:
            lines.append(f"Ostatni komentarz ({(comment.get('user') or {}).get('login')}): "
                         + str(comment.get("body") or "").strip()[:4000])
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp(item["updated_at"])))
        return {
            "kind": "github", "id": f"{item['id']}@{item['updated_at']}", "thread_id": item["id"],
            # Read on GitHub, or the latest comment is his own: nothing waits for him.
            "account": me, "read": not item.get("unread", True) or bool(comment and actor.get("login") == me),
            "from": actor.get("login") or repo, "bot": actor.get("type") == "Bot", "to": me,
            "subject": f"GitHub: {subject.get('title') or repo} · {repo}"[:180],
            "place": repo, "why": item.get("reason", ""), "date": when, "type": subject.get("type", ""),
            "owner": item["repository"]["owner"]["login"], "body": "\n".join(lines)[:20000], "url": url,
            "snippet": str(subject.get("title") or "")[:300], "sent": [],
        }


class Source:
    @staticmethod
    def defaults():
        return {"enabled": True, "token_command": ["gh", "auth", "token"],
                # Repositories the user only watches are noise unless he asks for them.
                "skip_reasons": ["subscribed"], "skip_repos": [],
                # Without a live listener, check this often between the regular checks (seconds).
                "poll_seconds": 300, "listen_quiet_seconds": 20}

    def due(self, settings, store, last_poll, now):
        from pathlib import Path

        from hermes_auto.listening import live, nudged, read_state
        settings = {**self.defaults(), **settings}
        try:
            home = Path(store.path).parent
        except (AttributeError, TypeError):
            return now - last_poll >= settings["poll_seconds"]
        if nudged(home, "github", last_poll, now, float(settings["listen_quiet_seconds"])):
            return True
        if live(read_state(home, "github"), now):
            return False  # the listener nudges; the regular checks are the fallback
        return bool(settings["poll_seconds"]) and now - last_poll >= settings["poll_seconds"]

    def poll(self, config, store):
        """Nothing to watch by label: GitHub candidates go through the sorter (`inbox`)."""

    def inbox(self, config, store, limit=40, since_ms=None):
        settings = {**self.defaults(), **config["sources"]["github"]}
        return GitHub(settings).inbox(config, store, limit=limit, since_ms=since_ms)

    def mark_read(self, config, items):
        """GitHub notifications stay unread: every item is reported "unsupported".

        GitHub only marks a whole notification thread read (PATCH /notifications/threads/{id}); an update that
        lands after the shown one would be read with it, and checking `updated_at` first cannot close that race.
        """
        return {item["id"]: "unsupported" for item in items}

    def setup(self, config):
        GitHub({**self.defaults(), **config["sources"]["github"]}).http.get("/user")
