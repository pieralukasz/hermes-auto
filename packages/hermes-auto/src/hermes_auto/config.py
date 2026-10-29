from __future__ import annotations

import json
import os
import shutil
from importlib.metadata import entry_points
from pathlib import Path


def default_home() -> Path:
    return Path(os.environ.get("HERMES_AUTO_HOME", "~/.config/hermes-auto")).expanduser()


def defaults() -> dict:
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return {
        "hermes_home": str(hermes_home),
        "hermes_runtime_home": str(hermes_home),
        "hermes_source": str(hermes_home / "hermes-agent"),
        "hermes_python": "",
        "hermes_command": shutil.which("hermes") or "hermes",
        "default_mode": "draft",
        "language": "English",
        "max_jobs_per_run": 5,
        "run_budget_seconds": 180,
        "max_turns": 8,
        # Full-tools runs (all Todoist tasks, mail with the agent label) use the normal Hermes toolsets.
        "agent_run_budget_seconds": 900,
        "agent_max_turns": 60,
        "poll_seconds": 300,
        "desktop_only": True,
        "desktop_process": "Hermes",
        "sources": {},
    }


def read_config(home: Path) -> dict:
    config = defaults()
    raw = json.loads((home / "config.json").read_text())
    config.update(raw)
    if config["default_mode"] not in ("open", "draft", "research", "agent"):
        raise ValueError("default_mode must be open, draft, research or agent")
    for key in ("max_jobs_per_run", "run_budget_seconds", "max_turns", "poll_seconds",
                "agent_run_budget_seconds", "agent_max_turns"):
        if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    routes = config.get("routes", [])
    if not isinstance(routes, list) or not all(
            isinstance(rule, dict) and isinstance(rule.get("match"), dict) and rule["match"]
            and isinstance(rule.get("provider", ""), str) and isinstance(rule.get("model", ""), str)
            and (rule.get("provider") or rule.get("model")) for rule in routes):
        raise ValueError("routes must be a list of {match: {...}, provider and/or model}")
    if "bots" in config:
        raise ValueError("bots is no longer supported: every session lives in the default profile. Remove `bots` "
                         "from config.json and put the per-account reads in `first_turn` (see docs/architecture.md)")
    first_turn_scope(config)
    return config


READ_FAMILIES = ("github", "google", "linear", "slack", "todoist")  # guard_plugin.policy.FAMILIES


def first_turn_scope(config: dict) -> dict:
    """What the first, automatic turn of a third-party session may read (`first_turn` in config.json), normalized:
    {"reads": [families], "providers": {provider: {"reads": [...], "session_search": bool}}}. A provider entry
    (an account chosen by `routes`, e.g. a work account) replaces the default reads for sessions on it and
    allows session_search only when it says so. Without the section every family is read, with no entries.
    `hermes-auto install-guard` writes this into the guard's scope.json; the prompts name only these reads."""
    section = config.get("first_turn")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("first_turn must be an object {reads, providers}")

    def families(value, where):
        if not isinstance(value, list) or not all(item in READ_FAMILIES for item in value):
            raise ValueError(f"{where} must be a list of {', '.join(READ_FAMILIES)}")
        return sorted(set(value))
    reads = families(section.get("reads", list(READ_FAMILIES)), "first_turn.reads")
    providers = section.get("providers") or {}
    if not isinstance(providers, dict):
        raise ValueError("first_turn.providers must be an object {provider: {reads, session_search}}")
    entries = {}
    for name, spec in providers.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise ValueError("first_turn.providers must be an object {provider: {reads, session_search}}")
        search = spec.get("session_search", False)
        if not isinstance(search, bool):
            raise ValueError(f"first_turn.providers.{name}.session_search must be true or false")
        entries[name] = {"reads": families(spec.get("reads"), f"first_turn.providers.{name}.reads"),
                         "session_search": search}
    return {"reads": reads, "providers": entries}


ROUTE_FIELDS = ("provider", "model")


def model_route(config: dict, source: str, data: dict | None = None) -> dict:
    """Provider/model for one event: the first `routes` rule whose `match` fits, else {} (Hermes default).

    A rule is {"match": {"source": "github", "owner": "acme"}, "provider": "...", "model": "..."}.
    `source` is the adapter (the job's source or the item's kind); other keys compare with the event
    payload, case-insensitively. This keeps one organisation's Slack and GitHub on its own account while
    everything else stays on the default model. The classifier and the session runs use the same rule.
    """
    fields = {**(data or {}), "source": source}
    for rule in config.get("routes") or []:
        match = rule.get("match") or {}
        if match and all(str(fields.get(key, "")).casefold() == str(value).casefold()
                         for key, value in match.items()):
            return {key: rule[key] for key in ROUTE_FIELDS if rule.get(key)}
    return {}


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def sources() -> dict:
    return {entry.name: entry.load() for entry in entry_points(group="hermes_auto.sources")}
