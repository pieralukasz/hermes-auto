"""Which bot (Hermes profile) a job's session lives in.

Bots are Hermes profiles served by the same multiplexed gateway (`gateway.multiplex_profiles`), each with its own
memory, skills and tool configuration. `config.json` → `bots`:

    "bots": {
      "default": "default",                      # briefs, plans, proposals and any task no rule names
      "inbox": "inbox",                          # third-party content no rule names
      "read_only": {"inbox": {"reads": ["google", "todoist", "github"]},
                    "tapin-inbox": {"reads": ["github", "linear", "slack"]}},
      "rules": [{"match": {"source": "slack", "dm": true}, "profile": "inbox"},
                {"match": {"source": "slack"}, "profile": "tapin-inbox"},
                {"match": {"source": "todoist", "projectId": "..."}, "profile": "tapin"}]
    }

Third-party content (mail, Slack, GitHub, Linear) only ever goes to a read-only bot: a rule naming another
profile for it is ignored. A full-tools job (a Todoist task, a mail the user gave the agent label) goes to the
first matching rule that names a working bot, else to `default`. Internal sources (brief, plans) go to `default`.
Without a `bots` section everything stays in the default profile, as before bots existed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT = "default"
INTERNAL_SOURCES = ("brief", "questions", "proposals", "unblocked")  # triage.INTERNAL_SOURCES
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def settings(config):
    return config.get("bots") or {}


def enabled(config):
    return bool(settings(config))


def read_only(config):
    """{profile: {"reads": [...]}} of the read-only bots."""
    return settings(config).get("read_only") or {}


def is_read_only(config, profile):
    return profile in read_only(config)


def validate(config):
    bots = config.get("bots")
    if bots in (None, {}):
        return
    if not isinstance(bots, dict):
        raise ValueError("bots must be an object")
    names = [bots.get("default", DEFAULT), bots.get("inbox", "")]
    ro = bots.get("read_only") or {}
    if not isinstance(ro, dict) or not ro:
        raise ValueError("bots.read_only must name at least one read-only bot profile")
    for name, spec in ro.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("reads"), list):
            raise ValueError(f"bots.read_only.{name} needs a reads list")
    if bots.get("inbox") not in ro:
        raise ValueError("bots.inbox must be one of bots.read_only")
    if bots.get("default", DEFAULT) in ro:
        raise ValueError("bots.default cannot be a read-only bot")
    rules = bots.get("rules") or []
    if not isinstance(rules, list) or not all(isinstance(rule, dict) and isinstance(rule.get("match"), dict)
                                              and rule["match"] and isinstance(rule.get("profile"), str)
                                              for rule in rules):
        raise ValueError("bots.rules must be a list of {match: {...}, profile}")
    names += list(ro) + [rule["profile"] for rule in rules]
    for name in names:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f"bots: invalid profile name {name!r}")


def profiles(config):
    """Every profile the bots section names (default first)."""
    bots = settings(config)
    names = [bots.get("default", DEFAULT), bots.get("inbox")] + list(read_only(config))
    names += [rule["profile"] for rule in bots.get("rules") or []]
    return [name for name in dict.fromkeys(names) if name]


def profile_home(config, profile):
    """The Hermes home of a profile: the root home for `default`, else `<root>/profiles/<name>`."""
    root = Path(config["hermes_home"]).expanduser()
    if not profile or profile == DEFAULT:
        return str(root)
    if not NAME.fullmatch(profile):
        raise ValueError(f"invalid bot profile {profile!r}")
    return str(root / "profiles" / profile)


def _payload(job):
    try:
        payload = json.loads(job.get("payload") or "{}")
    except ValueError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def full_tools(job):
    return job["source"] == "todoist" or job.get("mode") == "agent"  # runner.full_tools


def choose(job, config):
    """The bot for a job that has no session yet. Read-only bots only ever receive third-party content."""
    bots = settings(config)
    if not bots:
        return DEFAULT
    default = bots.get("default", DEFAULT)
    if job["source"] in INTERNAL_SOURCES:
        return default
    payload = _payload(job)
    third_party = not full_tools(job)
    if third_party and is_read_only(config, payload.get("bot")):
        return payload["bot"]  # a reply continues in the same bot as the conversation it answers
    fields = {**payload, "source": job["source"]}
    for rule in bots.get("rules") or []:
        if all(str(fields.get(key, "")).casefold() == str(value).casefold() for key, value in rule["match"].items()):
            if is_read_only(config, rule["profile"]) == third_party:
                return rule["profile"]
    return bots.get("inbox") if third_party else default


def of(job):
    """The profile a job's session lives in (jobs from before bots: default)."""
    return job.get("profile") or DEFAULT


__all__ = ["choose", "of", "profile_home", "profiles", "validate", "read_only", "is_read_only"]
