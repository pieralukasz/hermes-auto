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
        # Agent mode: the user's own Todoist task runs with the normal Hermes tools and skills.
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
    if config["default_mode"] not in ("open", "draft", "research"):
        raise ValueError("default_mode must be open, draft or research")
    for key in ("max_jobs_per_run", "run_budget_seconds", "max_turns", "poll_seconds",
                "agent_run_budget_seconds", "agent_max_turns"):
        if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    return config


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
