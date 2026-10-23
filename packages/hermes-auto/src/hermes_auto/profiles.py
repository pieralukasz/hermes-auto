"""Run a job in another Hermes profile, chosen by `profile_routes` rules in the config.

A rule routes jobs whose payload fields match, e.g. every Todoist task of one project:

    "profile_routes": [{"source": "todoist", "match": {"projectId": ["<id>"]},
                        "profile": "work", "since": "2026-10-23 18:30:00"}]

`since` (UTC, the store's `created` format) keeps jobs created before the rule in the profile whose
database already holds their sessions. The bridge still bootstraps from the root runtime home; only
`hermes_home` (sessions, config, credentials, skills) moves to the profile.
"""
from __future__ import annotations

import json
from pathlib import Path


def _payload(job):
    payload = job.get("payload") if job else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _matches(payload, match):
    for field, wanted in (match or {}).items():
        allowed = {str(item) for item in (wanted if isinstance(wanted, list) else [wanted])}
        if str(payload.get(field)) not in allowed:
            return False
    return True


def profile_for(job, config):
    """Profile name of the first matching rule, or None for the configured (default) home."""
    if not job or "source" not in job:
        return None
    payload = _payload(job)
    for rule in config.get("profile_routes") or ():
        if rule.get("source") not in (None, job["source"]):
            continue
        since = rule.get("since")
        if since and str(job.get("created") or "") < str(since):
            continue
        if _matches(payload, rule.get("match")):
            return rule.get("profile") or None
    return None


def routed_profiles(config):
    return sorted({rule["profile"] for rule in config.get("profile_routes") or () if rule.get("profile")})


def config_for(config, profile):
    """The bridge config for one profile: same runtime, the profile's Hermes home."""
    if not profile:
        return config
    home = Path(config["hermes_home"]).expanduser() / "profiles" / profile
    if not (home / "config.yaml").is_file():
        raise RuntimeError(f"Hermes profile {profile!r} from profile_routes does not exist ({home})")
    return {**config, "hermes_home": str(home),
            "hermes_runtime_home": config.get("hermes_runtime_home", config["hermes_home"])}
