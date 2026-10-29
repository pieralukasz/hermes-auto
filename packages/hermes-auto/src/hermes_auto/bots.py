"""Which bot (Hermes profile) a job's session lives in, and whom that bot delegates to.

Bots are Hermes profiles served by the same multiplexed gateway (`gateway.multiplex_profiles`), each with its own
memory, skills and tool configuration. `config.json` → `bots`:

    "bots": {
      "default": "default",                      # Dolores: every automatic session lives here
      "inbox": "inbox",                          # the read-only profile that prepares third-party matters
      "read_only": {"inbox": {"reads": ["google", "todoist", "github", "linear", "slack"]}},
      "rules": [{"match": {"source": "todoist", "projectId": "..."}, "profile": "tapin"}]
    }

With bots every job (mail, Slack, GitHub, Linear, briefs, plans, Todoist tasks, "↩" replies, also replies to
conversations of the old inbox sessions) opens its session in `bots.default`. Third-party content (mail, Slack,
GitHub, Linear) is prepared there by a subagent: Dolores' first, automatic turn may only `delegate_task` with
`profile: <bots.inbox>` (the guard plugin's `first_turn` mode, Hermes' `profile_routes` delegation backend), and
that subagent runs under the read-only profile's own guard. Dolores gets her full tools once the user writes in
the session. `bots.inbox` must be one of `bots.read_only` (its guard and read families).

`rules` no longer move a session into another profile: a full-tools job (a Todoist task) matching a rule gets a
note that the work belongs to that profile, which Dolores can hand over with `delegate_task` and `profile`. Rules
never name a read-only profile. The model account is a separate choice: `routes` is frozen into the session at
creation. Without a `bots` section everything stays in the default profile with the old bounded launches.
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


def subagent(config):
    """The read-only profile Dolores hands third-party matters to (`bots.inbox`), or None without bots."""
    if not enabled(config):
        return None
    return settings(config).get("inbox") or None


def delegates(config):
    """Whether third-party jobs run in Dolores and are prepared by her read-only subagent."""
    return subagent(config) is not None


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
        raise ValueError("bots.inbox (the subagent that prepares third-party matters) must be one of bots.read_only")
    if bots.get("default", DEFAULT) in ro:
        raise ValueError("bots.default cannot be a read-only bot")
    rules = bots.get("rules") or []
    if not isinstance(rules, list) or not all(isinstance(rule, dict) and isinstance(rule.get("match"), dict)
                                              and rule["match"] and isinstance(rule.get("profile"), str)
                                              for rule in rules):
        raise ValueError("bots.rules must be a list of {match: {...}, profile}")
    if any(rule["profile"] in ro for rule in rules):
        raise ValueError("bots.rules name working profiles for delegated work, never a read-only bot")
    names += list(ro) + [rule["profile"] for rule in rules]
    for name in names:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f"bots: invalid profile name {name!r}")


def profiles(config):
    """The profiles hermes-auto opens or prepares sessions in (Dolores first, then her subagent and any other
    read-only bot whose older sessions may still be retried)."""
    bots = settings(config)
    names = [bots.get("default", DEFAULT), bots.get("inbox")] + list(read_only(config))
    return [name for name in dict.fromkeys(names) if name]


def working_profiles(config):
    """Profiles named by `rules`: where Dolores may delegate the work of a domain (e.g. TapIn, eCMR)."""
    names = [rule["profile"] for rule in settings(config).get("rules") or []]
    default = settings(config).get("default", DEFAULT)
    return [name for name in dict.fromkeys(names) if name != default and not is_read_only(config, name)]


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
    """With bots, only the user's own Todoist tasks run with full tools. A mail thread with the agent label still
    carries other people's replies, so its subagent prepares it like any third-party matter."""
    return job["source"] == "todoist"


def choose(job, config):
    """The bot for a job that has no session yet: Dolores (`bots.default`) for every job once bots are on. A reply
    to a conversation of a read-only bot (`payload.bot`) opens there too; its context is still read from the
    conversation's own profile (triage.redirect_followup)."""
    if not enabled(config):
        return DEFAULT
    return settings(config).get("default", DEFAULT)


def delegation_hint(job, config):
    """The working profile a full-tools job belongs to (first matching rule), for its prompt; None otherwise.
    Third-party content is never pointed at a working profile: only the read-only subagent prepares it."""
    if not enabled(config) or not full_tools(job):
        return None
    fields = {**_payload(job), "source": job["source"]}
    for rule in settings(config).get("rules") or []:
        if all(str(fields.get(key, "")).casefold() == str(value).casefold() for key, value in rule["match"].items()):
            return rule["profile"] if rule["profile"] in working_profiles(config) else None
    return None


def of(job):
    """The profile a job's session lives in (jobs from before bots: default)."""
    return job.get("profile") or DEFAULT


__all__ = ["choose", "of", "profile_home", "profiles", "validate", "read_only", "is_read_only", "subagent",
           "delegates", "working_profiles", "delegation_hint"]
